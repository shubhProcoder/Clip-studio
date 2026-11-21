"""Low-res frame source for HUD analysis: never the full-res stream.

keyframes(): one frame every ~KEY_STEP s over the whole stream (ffmpeg -skip_frame nokey + select), KEY_W px.
dense(): DENSE_FPS frames per second at DENSE_W px over one window. For a URL the frames come from a
video-only <= 360p download (<work>/hud/analysis_360p.mp4) when yt-dlp's size estimate is <= 1.5 GB.
"""

import re
import subprocess
import threading
from pathlib import Path

from streamclipper.audio import _np
from streamclipper.env import _first_line
from streamclipper.layout import probe_video
from streamclipper.util import fwd, run_cmd

KEY_W = 640
KEY_STEP = 1.9  # seconds between keyframe-pass samples (at least; sparse keyframes give longer steps)
DENSE_W = 640
DENSE_FPS = 3.0
MAX_LOWRES_BYTES = 1.5e9
LOWRES_H = 360
_PTS = re.compile(r"pts_time:\s*(-?[\d.]+)")
_NO_VIDEO = ("", "mjpeg", "png")  # cover art in an audio file is not a video
_FFMPEG_MAJOR = None


def _fps_mode_args() -> list:
    global _FFMPEG_MAJOR
    if _FFMPEG_MAJOR is None:
        line = _first_line(["ffmpeg", "-hide_banner", "-version"])
        match = re.search(r"version\s+n?(\d+)\.", line)
        _FFMPEG_MAJOR = int(match.group(1)) if match else 99
    # FFmpeg 7+ removed the old -vsync spelling.
    return ["-fps_mode", "passthrough"] if _FFMPEG_MAJOR >= 7 else ["-vsync", "0"]


def has_video(path: Path) -> bool:
    try:
        return probe_video(Path(path))["vcodec"] not in _NO_VIDEO
    except Exception:
        return False


def lowres_size_estimate(info: dict, max_height: int = LOWRES_H):
    """Bytes of the best video-only format <= max_height in a yt-dlp info dict (filesize, filesize_approx or
    tbr x duration), or None when the site gives no size hint."""
    dur = float(info.get("duration") or 0.0)
    best = None
    for f in info.get("formats") or []:
        if (f.get("vcodec") or "none") == "none" or (f.get("height") or 0) > max_height or not f.get("height"):
            continue
        size = f.get("filesize") or f.get("filesize_approx")
        if not size and f.get("tbr") and dur:
            size = float(f["tbr"]) * 1000 / 8 * dur
        if size and (best is None or f["height"] > best[0] or (f["height"] == best[0] and size > best[1])):
            best = (f["height"], float(size))
    return best[1] if best else None


def analysis_source(source: Path, url: str, work_dir: Path, force: bool = False, info: dict = None):
    """
    The video to analyse: the local source when it has a video stream; for a URL the 360p video-only
    download (estimate <= 1.5 GB, or force). None (with a notice) when there is no usable video.
    """
    if source is not None and has_video(source):
        return Path(source)
    if not url:
        print("HUD detection skipped: the input has no video stream.")
        return None
    from streamclipper.download import download_lowres_video, video_info

    info = info if info is not None else video_info(url)
    est = lowres_size_estimate(info or {})
    if est is not None and est > MAX_LOWRES_BYTES and not force:
        print(
            f"HUD detection skipped: the {LOWRES_H}p video would be about {est / 1e9:.1f} GB "
            "(limit 1.5 GB); pass --hud gta5 (or another profile) to download it anyway."
        )
        return None
    return download_lowres_video(url, Path(work_dir) / "hud", LOWRES_H)


def _size(path: Path, width: int) -> tuple:
    m = probe_video(path)
    w = min(width, int(m["width"]) // 2 * 2)
    return w, max(2, int(round(w * m["height"] / max(1, m["width"]) / 2)) * 2), m


def _read_exact(stream, n: int):
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _pipe_frames(cmd: list, w: int, h: int, times=None):
    """Yield (t, rgb) from an ffmpeg rawvideo pipe. times: a callable i -> t, else the showinfo pts_time."""
    np = _np()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pts, err = [], []
    got = threading.Semaphore(0)

    def reader():
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace")
            m = _PTS.search(line)
            if m and "showinfo" in line:
                pts.append(float(m.group(1)))
                got.release()
            elif m is None:
                err.append(line.strip())

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    n, i = w * h * 3, 0
    try:
        while True:
            buf = _read_exact(proc.stdout, n)
            if buf is None:
                break
            if times is None:
                got.acquire(timeout=30)
                t = pts[i] if i < len(pts) else (pts[-1] if pts else 0.0)
            else:
                t = times(i)
            i += 1
            yield t, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        proc.stdout.close()
        rc = proc.wait()
        th.join(timeout=5)
    if rc != 0 and i == 0:
        raise RuntimeError("; ".join(x for x in err[-3:] if x) or f"ffmpeg exit code {rc}")


def _hwaccel(meta: dict, use_cuda) -> list:
    if use_cuda is None:
        from streamclipper.env import cuda_status

        use_cuda = int(meta.get("height") or 0) >= 720 and cuda_status()[0]
    return ["-hwaccel", "cuda"] if use_cuda else []


def keyframes(path: Path, width: int = KEY_W, step: float = KEY_STEP, use_cuda=None, start: float = 0.0):
    """Yield (t, rgb) about every `step` s from the keyframes (codecs that ignore -skip_frame decode all frames
    and select picks from them). -hwaccel cuda (>= 720p sources, CUDA ok) falls back to the CPU on an error."""
    w, h, meta = _size(path, width)
    vf = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{step:g})',scale={w}:{h},format=rgb24,showinfo"
    first = _hwaccel(meta, use_cuda)
    for hw in [first, []] if first else [[]]:
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", *hw, "-skip_frame", "nokey"]
        cmd += (["-ss", f"{start:.3f}"] if start > 0 else []) + ["-i", str(path), "-an", "-sn", "-vf", vf]
        cmd += _fps_mode_args() + ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        last = -1.0
        try:
            for t, fr in _pipe_frames(cmd, w, h):
                if t > last:
                    last = t
                    yield t, fr
            return
        except RuntimeError as e:
            if not hw:
                raise
            print(f"HUD: GPU decode failed ({str(e)[:100]}); using the CPU.")
            start = max(start, last + 0.01) if last >= 0 else start


def dense(path: Path, a: float, b: float, fps: float = DENSE_FPS, width: int = DENSE_W):
    """Yield (t, rgb) at `fps` over [a, b] (fast seek; t = a + i / fps)."""
    a = max(0.0, a)
    if b <= a:
        return
    w, h, _ = _size(path, width)
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}"]
    cmd += ["-i", str(path), "-an", "-sn", "-vf", f"fps={fps:g},scale={w}:{h},format=rgb24"]
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    yield from _pipe_frames(cmd, w, h, times=lambda i: round(a + i / fps, 3))


def frame_at(path: Path, t: float, width: int = DENSE_W):
    """One RGB frame at t (or None)."""
    w, h, _ = _size(path, width)
    res = run_cmd(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path)]
        + ["-frames:v", "1", "-vf", f"scale={w}:{h},format=rgb24", "-f", "rawvideo", "-"],
        f"frame grab from {fwd(path)}",
        binary=True,
        check=False,
    )
    buf = res.stdout or b""
    if len(buf) < w * h * 3:
        return None
    return _np().frombuffer(buf[: w * h * 3], _np().uint8).reshape(h, w, 3)
