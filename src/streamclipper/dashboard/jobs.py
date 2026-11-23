"""
Dashboard jobs: every job is a child process of the normal CLI (`python -m streamclipper ...`), run one at a
time from a FIFO queue. Jobs are saved in <workspace>/jobs.json and <workspace>/jobs/<id>/job.json, so a
restart lists the past jobs; a job that was running or queued becomes 'interrupted' (Resume runs it again).
"""

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from streamclipper.captions.styles import AUTO_STYLE, HOOK_STYLES, STYLES
from streamclipper.colab import _clip_argv
from streamclipper.effects import ENHANCE_MIX, ZOOM_LEVELS
from streamclipper.hud.analyze import HUD_MODES
from streamclipper.llm import PROVIDERS
from streamclipper.paths import PRESET_DIR
from streamclipper.state import write_json_atomic
from streamclipper.util import ToolError, own_group_flags

CLI_PREFIX = [sys.executable, "-m", "streamclipper"]  # tests replace it with a fake CLI script
JOB_ID_RE = re.compile(r"^[0-9A-Za-z-]{1,40}$")
RATIOS = ("9:16", "1:1")
MODES = ("vod", "podcast", "live")
RESUMABLE = ("failed", "cancelled", "interrupted", "stopped")
FINISHED = ("done", "failed", "cancelled", "stopped", "interrupted")
CANCEL_GRACE_S = 15.0  # Ctrl+Break first; the child tree is killed when it is still running after this
STOP_GRACE_S = 180.0  # live Stop: the queued clips are finished first
POLL_S = 0.5


def new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def check_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise ToolError("Unknown job.", "pick a job from the Jobs page")
    return job_id


def presets() -> list:
    return sorted(p.stem for p in PRESET_DIR.glob("*.json"))


def _choice(value, allowed, what: str, default=""):
    value = default if value in (None, "") else str(value)
    if value and value not in allowed:
        raise ToolError(f"Unknown {what}: {value}", f"pick one of: {', '.join(allowed)}")
    return value


TRUE_WORDS, FALSE_WORDS = ("true", "1", "yes"), ("false", "0", "no")


def parse_bool(value, what: str, default: bool = False) -> bool:
    """Strict boolean of an API option: true/false, 1/0 or the strings true/false, 1/0, yes/no (any case);
    missing or null = default. Anything else is a ToolError (HTTP 400), so "false" never counts as true."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in TRUE_WORDS + FALSE_WORDS:
        return value.strip().lower() in TRUE_WORDS
    raise ToolError(f"{what} must be true or false, not {str(value)[:40]!r}.", "send true/false, 1/0 or yes/no")


def _num(value, lo, hi, default, cast=float):
    try:
        v = cast(value)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def validate_options(raw: dict) -> dict:
    """Job options from the New job / Live form, checked against the real choice lists."""
    raw = raw or {}
    mode = _choice(raw.get("mode"), MODES, "mode", "vod")
    o = {
        "mode": mode,
        "preset": _choice(raw.get("preset"), presets(), "preset"),
        "caption_style": _choice(raw.get("caption_style"), [AUTO_STYLE, *STYLES], "caption style", AUTO_STYLE),
        "hook_style": _choice(raw.get("hook_style"), list(HOOK_STYLES), "hook style"),
        "ratio": _choice(raw.get("ratio"), RATIOS, "ratio", "9:16"),
        "llm": _choice(raw.get("llm"), PROVIDERS, "LLM", "none"),
        "min_score": _num(raw.get("min_score"), 0.0, 100.0, 50.0),
        "max_clips": _num(raw.get("max_clips"), 0, 50, 3, int),
        "render": parse_bool(raw.get("render"), "render", True),
        "drafts": parse_bool(raw.get("drafts"), "drafts", False),
        "stop_after": _num(raw.get("stop_after"), 0.0, 1440.0, 60.0),
        "max_clips_per_hour": _num(raw.get("max_clips_per_hour"), 1, 60, 12, int),
        # 5.3 effects (off by default) and the game HUD signal
        "zoom": _choice(raw.get("zoom"), list(ZOOM_LEVELS), "zoom", "off"),
        "remove_fillers": parse_bool(raw.get("remove_fillers"), "remove_fillers", False),
        "enhance_speech": _choice(raw.get("enhance_speech"), list(ENHANCE_MIX), "speech enhancement", "off"),
        "brand_kit": parse_bool(raw.get("brand_kit"), "brand_kit", False),
        "hud": _choice(raw.get("hud"), list(HUD_MODES), "HUD mode", "auto"),
        "game": str(raw.get("game") or "").strip()[:60],
        "creator_id": str(raw.get("creator_id") or "default").strip()[:60] or "default",
        "static_analysis": parse_bool(raw.get("static_analysis"), "static_analysis", True),
        "static_bucket": _num(raw.get("static_bucket"), 0.5, 10.0, 2.0),
        "static_min": _num(raw.get("static_min"), 1.0, 120.0, 6.0),
        "static_threshold": _num(raw.get("static_threshold"), 0.0, 255.0, 0.4),
    }
    if mode == "podcast":
        o["preset"] = "podcast-9x16"
    return o


def build_vod_argv(source: str, opts: dict, job_dir: Path, brand_kit: Path = None) -> list:
    """clip-stream argv (the Colab mapping); drafts go to <job>/capcut_drafts as portable drafts.
    brand_kit: the workspace's brand kit JSON, used when opts['brand_kit'] is on."""
    job_dir = Path(job_dir)
    droot = job_dir / "capcut_drafts"
    if opts["drafts"]:
        droot.mkdir(parents=True, exist_ok=True)
    o = {
        "prefix": "clip",
        "max_clips": opts["max_clips"],
        "min_score": opts["min_score"],
        "llm": opts["llm"],
        "whisper_model": "auto",
        "device": "auto",
        "language": "auto",
        "ratio": opts["ratio"],
        "preset": opts["preset"],
        "caption_style": opts["caption_style"],
        "hook_style": opts["hook_style"],
        "llm_model": "",
        "chat": "",
        "render": opts["render"],
        "drafts": opts["drafts"],
        "draft_root": droot,
        "resume": True,
        "zoom": opts.get("zoom") or "off",
        "remove_fillers": bool(opts.get("remove_fillers")),
        "enhance_speech": opts.get("enhance_speech") or "off",
        "brand_kit": str(brand_kit) if opts.get("brand_kit") and brand_kit else "",
        "hud": opts.get("hud") or "auto",
        "static_analysis": opts.get("static_analysis", True),
        "static_bucket": opts.get("static_bucket", 2.0),
        "static_min": opts.get("static_min", 6.0),
        "static_threshold": opts.get("static_threshold", 0.4),
        "game": opts.get("game") or "",
    }
    return _clip_argv(source, job_dir / "out", o)


def build_live_argv(source: str, opts: dict, job_dir: Path) -> list:
    job_dir = Path(job_dir)
    argv = ["live", "--url", source, "--out", str(job_dir / "out"), "--prefix", "LIVE"]
    argv += ["--min-score", str(float(opts["min_score"])), "--max-clips-per-hour", str(int(opts["max_clips_per_hour"]))]
    argv += ["--stop-after", f"{float(opts['stop_after']):g}", "--ratio", opts["ratio"]]
    for flag, key in (("--caption-style", "caption_style"), ("--hook-style", "hook_style"), ("--preset", "preset")):
        if opts.get(key):
            argv += [flag, str(opts[key])]
    if opts["render"]:
        argv.append("--render")
    if opts["drafts"]:
        droot = job_dir / "capcut_drafts"
        droot.mkdir(parents=True, exist_ok=True)
        argv += ["--draft-root", str(droot), "--portable-drafts"]
    else:
        argv.append("--no-draft")
    argv += ["--llm", opts["llm"]]
    return argv


def build_render_argv(plan_path: Path, caption_style: str = "", hook_style: str = "") -> list:
    argv = ["render", "--spec", str(plan_path)]
    if caption_style:
        argv += ["--caption-style", caption_style]
    if hook_style:
        argv += ["--hook-style", hook_style]
    return argv


_CHUNKS_RE = re.compile(r"Transcribing .* in (\d+) chunk")
_CHUNK_RE = re.compile(r"ASR chunk (\d+)/(\d+)")
_RANK_ROW_RE = re.compile(r"^\d+\s+\d")


def parse_progress(lines, prev: dict = None, max_clips: int = 0) -> dict:
    """
    Stage of a running job from its new log lines: {stage, detail, k, n, rank_rows, clips, error}.
    prev is the result for the earlier lines (the log is read incrementally).
    """
    p = dict(prev or {"stage": "starting", "detail": "", "k": None, "n": None, "rank_rows": 0, "clips": 0})
    in_table = p.pop("_in_table", False)
    for raw in lines:
        line = raw.rstrip("\r\n")
        s = line.strip()
        if in_table:
            if _RANK_ROW_RE.match(s):
                p["rank_rows"] += 1
                continue
            in_table = False
        if s.startswith(("Audio for analysis", "Source:", "Slice download")):
            p.update(stage="download", detail=s, k=None, n=None)
        elif s.startswith("Analysing audio"):
            p.update(stage="analysis", detail=s, k=None, n=None)
        elif m := _CHUNKS_RE.search(s):
            p.update(stage="transcribe", detail=s, k=0, n=int(m.group(1)))
        elif m := _CHUNK_RE.search(s):
            p.update(stage="transcribe", detail=f"chunk {m.group(1)}/{m.group(2)}", k=int(m.group(1)))
            p["n"] = int(m.group(2))
        elif s.startswith("Transcribing"):
            p.update(stage="transcribe", detail=s, k=None, n=None)
        elif s.startswith("RANK"):
            p.update(stage="scoring", detail="ranking clips", k=None, n=None, rank_rows=0)
            in_table = True
        elif s.startswith("Analysis time:"):
            p.update(stage="scoring", detail=s, k=None, n=None)
        elif s.startswith("Rendering "):
            if p["stage"] == "live":
                p["detail"] = "rendering a clip"
                continue
            k = (p["k"] or 0) + 1 if p["stage"] == "render" else 1
            rows = p["rank_rows"]
            n = min(max_clips, rows) if max_clips and rows else (rows or max_clips or None)
            p.update(stage="render", k=k, n=max(n, k) if n else None)
            p["detail"] = f"clip {k}/{p['n']}" if p["n"] else f"clip {k}"
        elif s.startswith("RENDER SUMMARY"):
            p.update(stage="finishing", detail="writing the report", k=None, n=None)
        elif s.startswith("Live: buffered") or s.startswith("Live: "):
            p.update(stage="live", detail=s[6:].strip(), k=None, n=None)
        elif s.startswith("CLIP "):
            p["clips"] += 1
            p.update(stage="live", detail=f"{p['clips']} clip(s) ready")
        elif s.startswith("LIVE SUMMARY"):
            p.update(stage="finishing", detail="live run finished")
        elif s.startswith("ERROR:"):
            p["error"] = s[6:].strip()[:300]
    p["_in_table"] = in_table
    return p


def public_progress(p: dict) -> dict:
    return {k: v for k, v in (p or {}).items() if not k.startswith("_")}


class JobStore:
    """Jobs on disk. One lock for every read-modify-write."""

    def __init__(self, ws: Path):
        self.ws = Path(ws).resolve()
        self.dir = self.ws / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self._load()

    def _load(self):
        for jp in sorted(self.dir.glob("*/job.json")):
            try:
                job = json.loads(jp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(job, dict) or not JOB_ID_RE.match(str(job.get("id", ""))):
                continue
            if not str(job.get("creator_id") or "").strip():
                job["creator_id"] = "default"
                write_json_atomic(jp, job)
            if job.get("status") in ("running", "queued"):
                job["status"] = "interrupted"
                job["stage"] = "interrupted"
                write_json_atomic(jp, job)
            self.jobs[job["id"]] = job
        self._save_index()

    def _save_index(self):
        index = [
            {k: j.get(k) for k in ("id", "kind", "label", "created", "status", "creator_id")}
            for j in sorted(self.jobs.values(), key=lambda j: j.get("created") or 0)
        ]
        write_json_atomic(self.ws / "jobs.json", index)

    def job_dir(self, job_id: str) -> Path:
        return self.dir / check_job_id(job_id)

    def create(self, kind: str, label: str, argv: list, options: dict, parent: str = None, job_id: str = None) -> dict:
        with self.lock:
            jid = job_id or new_job_id()
            while jid in self.jobs:
                jid = new_job_id()
            job = {
                "id": jid,
                "kind": kind,
                "label": label,
                "argv": [str(a) for a in argv],
                "options": options,
                "status": "queued",
                "stage": "queued",
                "progress": {},
                "created": time.time(),
                "started": None,
                "ended": None,
                "returncode": None,
                "parent": parent,
                "creator_id": str(options.get("creator_id") or "default"),
                "marks": {},
                "variants": {},
                "deleted": [],
                "imported": {},
            }
            self.job_dir(jid).mkdir(parents=True, exist_ok=True)
            self.jobs[jid] = job
            self._write(job)
            return dict(job)

    def _write(self, job: dict):
        write_json_atomic(self.job_dir(job["id"]) / "job.json", job)
        self._save_index()

    def get(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(check_job_id(job_id))
            if job is None:
                raise KeyError(job_id)
            return job

    def update(self, job_id: str, **fields) -> dict:
        with self.lock:
            job = self.get(job_id)
            job.update(fields)
            self._write(job)
            return job

    def mutate(self, job_id: str, fn):
        """fn(job) changes the job in place under the lock; the job is saved afterwards."""
        with self.lock:
            job = self.get(job_id)
            res = fn(job)
            self._write(job)
            return res

    def list(self) -> list:
        with self.lock:
            return sorted((dict(j) for j in self.jobs.values()), key=lambda j: j.get("created") or 0, reverse=True)


def _signal_child(proc: subprocess.Popen) -> bool:
    """Ctrl+Break (Windows) / SIGINT to the child's own process group. False when it could not be sent."""
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        return True
    except (OSError, ValueError):
        return False


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill only this child's process tree (never CapCut or anything else)."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass


class Runner:
    """One worker thread: runs queued jobs one at a time (they all use the GPU)."""

    def __init__(self, store: JobStore, on_finish=None):
        self.store = store
        self.on_finish = on_finish  # called with the job dict after a job ended
        self.queue = deque()
        self.cond = threading.Condition()
        self.current = None  # (job_id, Popen)
        self.requested = {}  # job_id -> "cancelled" | "stopped" | "interrupted"
        self.closing = False
        self.thread = threading.Thread(target=self._loop, name="streamclipper-jobs", daemon=True)
        self.thread.start()

    def submit(self, job_id: str) -> None:
        with self.cond:
            self.store.update(job_id, status="queued", stage="queued", progress={}, returncode=None, ended=None)
            self.queue.append(job_id)
            self.cond.notify_all()

    def queued_ids(self) -> list:
        with self.cond:
            return list(self.queue)

    def running_id(self):
        cur = self.current
        return cur[0] if cur else None

    def _loop(self):
        while True:
            with self.cond:
                while not self.queue and not self.closing:
                    self.cond.wait()
                if self.closing:
                    return
                job_id = self.queue.popleft()
            try:
                job = self.store.get(job_id)
            except KeyError:
                continue
            if job.get("status") != "queued":
                continue
            self._run(job_id)

    def _run(self, job_id: str):
        store = self.store
        job = store.get(job_id)
        jdir = store.job_dir(job_id)
        log_path = jdir / "log.txt"
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        argv = list(CLI_PREFIX) + list(job["argv"])
        max_clips = int((job.get("options") or {}).get("max_clips") or 0)
        with open(log_path, "ab") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} streamclipper {job['argv'][0]} ===\n".encode())
            log.flush()
            offset = log_path.stat().st_size
            try:
                proc = subprocess.Popen(
                    argv,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=str(jdir),
                    env=env,
                    **own_group_flags(),
                )
            except OSError as e:
                store.update(job_id, status="failed", stage="failed", ended=time.time(), progress={"error": str(e)})
                return
            self.current = (job_id, proc)
            store.update(job_id, status="running", stage="starting", started=time.time())
            prog, buf = None, b""
            while True:
                done = proc.poll() is not None
                offset, buf, prog = self._read_progress(log_path, offset, buf, prog, max_clips)
                store.update(job_id, stage=prog["stage"], progress=public_progress(prog))
                if done:
                    break
                time.sleep(POLL_S)
        self.current = None
        rc = proc.returncode
        asked = self.requested.pop(job_id, None)
        status = asked or ("done" if rc == 0 else "failed")
        if asked == "stopped" and rc not in (0, None):
            status = "stopped"
        store.update(job_id, status=status, stage=status, returncode=rc, ended=time.time())
        if self.on_finish is not None:
            try:
                self.on_finish(store.get(job_id))
            except Exception as e:  # a bookkeeping error never stops the queue
                print(f"Dashboard: after job {job_id}: {e}")

    @staticmethod
    def _read_progress(log_path: Path, offset: int, buf: bytes, prog, max_clips: int):
        try:
            with open(log_path, "rb") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            data = b""
        offset += len(data)
        buf += data
        *lines, buf = buf.split(b"\n")
        text = [ln.decode("utf-8", "replace") for ln in lines]
        return offset, buf, parse_progress(text, prog, max_clips)

    def cancel(self, job_id: str) -> str:
        return self._end(job_id, "cancelled", CANCEL_GRACE_S)

    def stop(self, job_id: str) -> str:
        """Live: stop the ingest; the clips already queued are finished (Ctrl+Break = the CLI's Ctrl+C path)."""
        return self._end(job_id, "stopped", STOP_GRACE_S)

    def _end(self, job_id: str, how: str, grace: float) -> str:
        with self.cond:
            if job_id in self.queue:
                self.queue.remove(job_id)
                self.store.update(job_id, status="cancelled", stage="cancelled", ended=time.time())
                return "cancelled"
        cur = self.current
        if not cur or cur[0] != job_id:
            raise ToolError("This job is not running.", "only queued or running jobs can be cancelled")
        proc = cur[1]
        self.requested[job_id] = how
        self.store.update(job_id, stage="stopping")
        if how == "stopped" and self._live_already_stopping(job_id):
            # --stop-after was reached: the run is finishing its queued clips; a signal now would cut that short
            return how
        sent = _signal_child(proc)

        def escalate():
            try:
                proc.wait(timeout=grace if sent else 0.1)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)

        threading.Thread(target=escalate, daemon=True).start()
        return how

    def _live_already_stopping(self, job_id: str) -> bool:
        try:
            text = (self.store.job_dir(job_id) / "log.txt").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        run = text.rsplit("\n=== ", 1)[-1]
        return any(m in run for m in ("--stop-after reached", "stopping the ingest", "LIVE SUMMARY"))

    def resume(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job.get("status") not in RESUMABLE:
            raise ToolError(
                f"A {job.get('status')} job cannot be resumed.", "resume a failed, cancelled or stopped job"
            )
        argv = [a for a in job["argv"] if a != "--fresh"]
        self.store.update(job_id, argv=argv)
        self.submit(job_id)

    def shutdown(self, wait: float = 30.0) -> None:
        """Server exit: the running job is stopped cleanly and can be resumed later."""
        with self.cond:
            self.closing = True
            self.cond.notify_all()
        cur = self.current
        if cur:
            job_id, proc = cur
            self.requested[job_id] = "interrupted"
            _signal_child(proc)
            try:
                proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
            self.thread.join(5)
