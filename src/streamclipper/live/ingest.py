"""
Live ingest: streamlink (or ffmpeg directly) -> ffmpeg segment muxer -> rolling 10 s MPEG-TS segments.
Every subprocess gets an argument list (never a shell); the URL is always one argument.
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from streamclipper.util import TOOL_HINTS, ToolError, own_group_flags, ytdlp_cmd

KINDS = ("twitch", "youtube", "kick", "hls", "file")
SEGMENT_S = 10
BACKOFF_S = (2, 5, 10, 30)
MAX_TRIES, TRIES_WINDOW_S = 10, 600.0
STALL_S = 60.0


def resolve_source(url: str) -> str:
    """twitch | youtube | kick | hls | file (a local video, used as a fake-live source)."""
    u = str(url or "").strip()
    if not u:
        raise ToolError("Pass --url <live stream link>.", "example: streamclipper live --url https://twitch.tv/NAME")
    if os.path.isfile(u):
        return "file"
    p = urlparse(u if "://" in u else "https://" + u)
    host = (p.netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host.endswith("twitch.tv"):
        return "twitch"
    if host.endswith("youtube.com") or host == "youtu.be":
        return "youtube"
    if host.endswith("kick.com"):
        return "kick"
    if p.path.lower().endswith(".m3u8"):
        return "hls"
    if "://" not in u and not host.count("."):
        raise ToolError(f"File not found: {u}", "check the path; put quotes around paths with spaces")
    raise ToolError(
        f"Live mode does not know this site: {host or u}.",
        "use a Twitch, YouTube Live or Kick link, a direct .m3u8 URL, or a local video file",
    )


def channel_name(url: str) -> str:
    """Short folder-safe name of the channel or video (twitch.tv/NAME -> NAME, a file -> its stem)."""
    u = str(url or "").strip()
    if os.path.isfile(u):
        name = Path(u).stem
    else:
        p = urlparse(u if "://" in u else "https://" + u)
        parts = [x for x in p.path.split("/") if x]
        q = dict(kv.split("=", 1) for kv in p.query.split("&") if "=" in kv)
        name = q.get("v") or (parts[-1] if parts else p.netloc)
        if parts and parts[0].startswith("@"):
            name = parts[0][1:]
    return re.sub(r"[^\w-]+", "_", name or "stream").strip("_")[:40] or "stream"


def twitch_channel(url: str) -> str:
    p = urlparse(url if "://" in url else "https://" + url)
    parts = [x for x in p.path.split("/") if x]
    return parts[0].lower() if parts else ""


def streamlink_cmd() -> list:
    if importlib.util.find_spec("streamlink") is not None:
        return [sys.executable, "-m", "streamlink"]
    exe = shutil.which("streamlink")
    if exe:
        return [exe]
    raise ToolError("streamlink is not installed (needed for live mode).", TOOL_HINTS["streamlink"])


def _proc_flags() -> dict:
    """Own process group: a console Ctrl+C reaches only streamclipper, which then stops the ingest cleanly."""
    return own_group_flags()


def segment_args(csv_path: Path, buffer_dir: Path, start_number: int) -> list:
    return [
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        str(SEGMENT_S),
        "-segment_format",
        "mpegts",
        "-segment_list",
        str(csv_path),
        "-segment_list_type",
        "csv",
        "-segment_start_number",
        str(start_number),
        "-reset_timestamps",
        "1",
        str(Path(buffer_dir) / "seg_%06d.ts"),
    ]


def pipeline_cmds(kind: str, url: str, csv_path: Path, buffer_dir: Path, start_number: int, hls_url: str = None):
    """[argv, ...]: one ffmpeg, or streamlink piped into ffmpeg."""
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
    seg = segment_args(csv_path, buffer_dir, start_number)
    if kind == "file":
        return [ff + ["-re", "-i", str(url)] + seg]
    if kind == "hls" or hls_url:
        return [ff + ["-i", hls_url or url] + seg]
    sl = streamlink_cmd() + ["--stdout", "--loglevel", "warning", "--stream-sorting-excludes", ">1080p", url, "best"]
    return [sl, ff + ["-i", "pipe:0"] + seg]


def is_offline(kind: str, url: str) -> bool:
    """True when streamlink finds no playable stream (the broadcast has ended)."""
    if kind in ("file", "hls"):
        return kind == "file"
    try:
        res = subprocess.run(
            streamlink_cmd() + ["--json", url],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError, ToolError):
        return False
    out = (res.stdout or "").lower()
    return '"error"' in out and ("no playable streams" in out or "offline" in out or "no streams" in out)


def ytdlp_hls(url: str) -> str:
    """Direct HLS URL of a live stream via yt-dlp -g (fallback when streamlink fails). Never printed."""
    res = subprocess.run(
        ytdlp_cmd() + ["-g", "-f", "best[height<=1080]", url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip().startswith("http")]
    return lines[0] if lines else ""


class Ingest:
    """
    Runs the ingest pipeline in a thread. On a drop it restarts with backoff 2/5/10/30 s (keeps segment
    numbering, at most 10 tries per 10 min). The stream has ended when the pipeline exits and the source
    is offline, a local file has been played to the end, or no new segment came for 60 s after 3 reconnects.
    """

    def __init__(self, url: str, kind: str, ring, log_dir: Path, backoff=BACKOFF_S, stall_s: float = STALL_S):
        self.url, self.kind, self.ring = url, kind, ring
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.backoff, self.stall_s = tuple(backoff), stall_s
        self.reconnects = 0
        self.end_reason = ""
        self.finished = threading.Event()
        self._stop = threading.Event()
        self._procs = []
        self._lock = threading.Lock()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="live-ingest", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self, timeout: float = None):
        if self._thread:
            self._thread.join(timeout)

    def _spawn(self, attempt: int, hls_url: str = None) -> Path:
        csv_path = self.ring.dir / ("live.csv" if attempt == 0 else f"live_{attempt:02d}.csv")
        cmds = pipeline_cmds(self.kind, self.url, csv_path, self.ring.dir, self.ring.next_number(), hls_url)
        self.ring.watch(csv_path)
        procs = []
        with self._lock:
            for i, argv in enumerate(cmds):
                log = open(self.log_dir / f"ingest_{attempt:02d}_{i}.log", "w", encoding="utf-8")
                last = i == len(cmds) - 1
                stdin = procs[-1].stdout if procs else (subprocess.PIPE if last else subprocess.DEVNULL)
                p = subprocess.Popen(
                    argv,
                    stdin=stdin,
                    stdout=subprocess.DEVNULL if last else subprocess.PIPE,
                    stderr=log,
                    **_proc_flags(),
                )
                p._sc_log = log
                if procs:
                    procs[-1].stdout.close()  # ffmpeg owns the pipe now; streamlink sees EOF if ffmpeg exits
                procs.append(p)
            self._procs = procs
        return csv_path

    def _alive(self) -> bool:
        return any(p.poll() is None for p in self._procs)

    def _stop_procs(self):
        """Graceful: 'q' to a stand-alone ffmpeg, terminate streamlink (ffmpeg then sees EOF); kill after 15 s."""
        with self._lock:
            procs = list(self._procs)
        if not procs:
            return
        if len(procs) == 1:
            ff = procs[0]
            try:
                ff.stdin.write(b"q")
                ff.stdin.flush()
                ff.stdin.close()
            except (OSError, ValueError, AttributeError):
                pass
        else:
            if procs[0].poll() is None:
                procs[0].terminate()
        deadline = time.time() + 15.0
        for p in procs:
            try:
                p.wait(max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)
        self._close_logs(procs)

    def _close_logs(self, procs):
        for p in procs:
            log = getattr(p, "_sc_log", None)
            if log and not log.closed:
                log.close()

    def _wait_attempt(self, t_start: float) -> str:
        """Wait for the running pipeline: 'exit', 'stop' or 'stall'."""
        while self._alive():
            if self._stop.wait(1.0):
                return "stop"
            last = self.ring.last_wall() or t_start
            if time.time() - max(last, t_start) > self.stall_s:
                return "stall"
        return "exit"

    def _run(self):
        attempt, fails = 0, 0
        tries = deque()
        hls_url = None
        try:
            while not self._stop.is_set():
                n_before = len(self.ring.segments)
                t_start = time.time()
                csv_path = self._spawn(attempt, hls_url)
                why = self._wait_attempt(t_start)
                if why == "stop":
                    self._stop_procs()
                    self.end_reason = "stopped"
                    break
                if why == "stall":
                    print(f"Live: no new segment for {self.stall_s:.0f} s; restarting the ingest.")
                    self._stop_procs()
                else:
                    with self._lock:
                        self._close_logs(self._procs)
                time.sleep(1.5)  # the segment muxer writes the last list line on exit
                produced = len(self.ring.segments) > n_before or _csv_lines(csv_path) > 0
                if self.kind == "file":
                    self.end_reason = "the source file has been played to the end"
                    break
                if self.kind in ("youtube", "kick") and not produced and hls_url is None:
                    hls_url = ytdlp_hls(self.url)
                    if hls_url:
                        print("Live: streamlink gave no video; using yt-dlp's HLS link instead.")
                        attempt += 1
                        continue
                    hls_url = None
                if is_offline(self.kind, self.url):
                    self.end_reason = "the stream is offline"
                    break
                now = time.time()
                tries.append(now)
                while tries and now - tries[0] > TRIES_WINDOW_S:
                    tries.popleft()
                last = self.ring.last_wall() or t_start
                if len(tries) > MAX_TRIES:
                    self.end_reason = f"{MAX_TRIES} reconnects in 10 minutes"
                    break
                if self.reconnects >= 3 and now - last >= self.stall_s:
                    self.end_reason = f"no new segment for {self.stall_s:.0f} s after {self.reconnects} reconnects"
                    break
                fails = 0 if produced else fails + 1
                wait = self.backoff[min(fails, len(self.backoff) - 1)]
                self.reconnects += 1
                print(f"Live: the stream dropped; reconnect {self.reconnects} in {wait} s ...")
                if self._stop.wait(wait):
                    self.end_reason = "stopped"
                    break
                attempt += 1
        except Exception as e:  # the main loop must always learn that the ingest ended
            self.end_reason = f"ingest error: {e}"
        finally:
            if self._procs and self._alive():
                self._stop_procs()
            self.finished.set()


def _csv_lines(path: Path) -> int:
    try:
        return sum(1 for ln in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip())
    except OSError:
        return 0
