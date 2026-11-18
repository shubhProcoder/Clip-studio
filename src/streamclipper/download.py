"""yt-dlp downloads: heatmap, audio, VOD slices."""

import hashlib
import json
import time
from pathlib import Path

from streamclipper.layout import probe_video
from streamclipper.util import TOOL_HINTS, ToolError, fwd, require_tool, run_cmd, ytdlp_cmd

SLICE_RETRY_WAITS = (5.0, 20.0)  # seconds before the 2nd and 3rd slice download attempt


def youtube_heatmap(url: str) -> tuple:
    """(duration, title, [(start, end, value)]) from yt-dlp -J; the list is empty when there is no heatmap."""
    res = run_cmd(
        ytdlp_cmd() + ["-J", "--no-playlist", url],
        "yt-dlp metadata fetch",
        hint="check the URL and your network; " + TOOL_HINTS["yt-dlp"],
    )
    info = json.loads(res.stdout)
    hm = [
        (float(x["start_time"]), float(x["end_time"]), float(x.get("value", 0.0)))
        for x in (info.get("heatmap") or [])
        if "start_time" in x and "end_time" in x
    ]
    return float(info.get("duration") or 0.0), str(info.get("title") or ""), hm


def download_audio(url: str, work_dir: Path) -> Path:
    """Audio only (m4a when available) for analysis; the video is downloaded later per picked clip."""
    work_dir.mkdir(parents=True, exist_ok=True)
    stem = f"audio_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:8]}"
    done = [p for p in work_dir.glob(f"{stem}.*") if not p.name.endswith((".part", ".ytdl"))]
    if done:
        return done[0]
    run_cmd(
        ytdlp_cmd()
        + ["-f", "ba[ext=m4a]/ba", "--no-playlist", "--no-progress", "-o", str(work_dir / f"{stem}.%(ext)s"), url],
        "yt-dlp audio download",
        hint="check the URL/network; update with: python -m pip install -U yt-dlp",
    )
    done = [p for p in work_dir.glob(f"{stem}.*") if not p.name.endswith((".part", ".ytdl"))]
    if not done:
        raise ToolError("yt-dlp finished but produced no audio file.", "python -m pip install -U yt-dlp")
    print(f"Audio for analysis: {fwd(done[0])}")
    return done[0]


def fetch_chat(url: str, work_dir: Path):
    """
    Chat replay of a VOD via yt-dlp subtitles (YouTube live_chat, Twitch rechat), or None with one
    notice when the site has none (Kick, chat replay off, network error).
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    stem = f"chat_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:8]}"

    def found():
        got = [p for p in work_dir.glob(f"{stem}.*") if p.suffix.lower() in (".json", ".jsonl")]
        return got[0] if got else None

    if found() is None:
        res = run_cmd(
            ytdlp_cmd()
            + [
                "--skip-download",
                "--write-subs",
                "--sub-langs",
                "live_chat,rechat",
                "--no-playlist",
                "--no-progress",
                "-o",
                str(work_dir / f"{stem}.%(ext)s"),
                url,
            ],
            "yt-dlp chat replay download",
            check=False,
        )
        if res.returncode != 0 or found() is None:
            print("No chat replay for this link (or the site does not offer one); scoring without chat.")
            return None
    print(f"Chat replay: {fwd(found())}")
    return found()


def video_info(url: str) -> dict:
    """yt-dlp -J metadata of a URL ({} when it cannot be read: callers treat the size as unknown)."""
    res = run_cmd(ytdlp_cmd() + ["-J", "--no-playlist", url], "yt-dlp metadata fetch", check=False)
    try:
        return json.loads(res.stdout) if res.returncode == 0 else {}
    except ValueError:
        return {}


def download_lowres_video(url: str, work_dir: Path, max_height: int = 360) -> Path:
    """Video only, <= max_height (for HUD analysis), resumable, cached as <work_dir>/analysis_<h>p.mp4."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    stem = f"analysis_{max_height}p"
    done = [p for p in work_dir.glob(f"{stem}.*") if not p.name.endswith((".part", ".ytdl"))]
    if done:
        return done[0]
    print(f"Downloading a {max_height}p video-only copy for HUD detection ...")
    run_cmd(
        ytdlp_cmd()
        + ["-f", f"bv*[height<={max_height}][ext=mp4]/bv*[height<={max_height}]/wv*", "--no-playlist"]
        + ["--no-progress", "--continue", "-o", str(work_dir / f"{stem}.%(ext)s"), url],
        "yt-dlp low-res video download",
        hint="check the URL/network; update with: python -m pip install -U yt-dlp",
    )
    done = [p for p in work_dir.glob(f"{stem}.*") if not p.name.endswith((".part", ".ytdl"))]
    if not done:
        raise ToolError("yt-dlp finished but produced no video file.", "python -m pip install -U yt-dlp")
    print(f"Video for HUD detection: {fwd(done[0])}")
    return done[0]


def download_slice(url: str, start: float, end: float, out: Path, max_height: int = 1080) -> Path:
    """
    Download only [start, end] of a video with yt-dlp, then make sure the result is
    H.264 yuv420p + AAC 48 kHz, <= max_height (re-encode only when needed, never upscale).
    """
    out = Path(out).resolve()
    if end <= start:
        raise ToolError(f"--end ({end}) must be after --start ({start}).", "example: --start 60 --end 70")
    out.parent.mkdir(parents=True, exist_ok=True)
    require_tool("ffmpeg")
    tmp_base = out.parent / f"{out.stem}.dl_tmp"
    for old in out.parent.glob(f"{out.stem}.dl_tmp.*"):
        old.unlink()
    fmt = (
        f"bv*[height<={max_height}][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<={max_height}]+ba/b[height<={max_height}]"
    )
    argv = ytdlp_cmd() + [
        "-f",
        fmt,
        "--download-sections",
        f"*{start:g}-{end:g}",
        "--force-keyframes-at-cuts",
        "--merge-output-format",
        "mp4",
        "--no-playlist",
        "--no-progress",
        "-o",
        f"{tmp_base}.%(ext)s",
        url,
    ]
    # a long clip-stream run downloads one slice per clip: a short network drop must not end it
    for attempt, wait in enumerate(SLICE_RETRY_WAITS + (None,)):
        try:
            run_cmd(
                argv,
                "yt-dlp download",
                hint="check the URL/network (private or region-locked videos fail); "
                "update with: python -m pip install -U yt-dlp",
            )
            break
        except ToolError:
            if wait is None:
                raise
            for old in out.parent.glob(f"{out.stem}.dl_tmp.*"):
                old.unlink()
            print(f"Slice download failed (attempt {attempt + 1}); retrying in {wait:g} s ...")
            time.sleep(wait)
    got = [p for p in out.parent.glob(f"{out.stem}.dl_tmp.*") if not p.name.endswith((".part", ".ytdl"))]
    if not got:
        raise ToolError("yt-dlp finished but produced no file.", "python -m pip install -U yt-dlp")
    tmp = got[0]
    m = probe_video(tmp)
    v_ok = m["vcodec"] == "h264" and m["pix_fmt"] == "yuv420p" and m["height"] <= max_height
    a_ok = (not m["has_audio"]) or (m["acodec"] == "aac" and m["sample_rate"] == 48000)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(tmp)]
    if v_ok:
        cmd += ["-c:v", "copy"]
    else:
        cmd += [
            "-vf",
            f"scale=-2:'min(ih,{max_height})'",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
        ]
    cmd += ["-c:a", "copy"] if a_ok else ["-c:a", "aac", "-ar", "48000", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", str(out)]
    run_cmd(cmd, "slice re-encode to H.264/AAC")
    tmp.unlink()
    f = probe_video(out)
    print(
        f"DOWNLOADED: {fwd(out)} | {f['vcodec']} {f['pix_fmt']} {f['width']}x{f['height']}@{f['fps']} | "
        f"{f['acodec']} {f['sample_rate']} Hz | {f['dur']:.2f}s"
        + ("" if v_ok else " | video re-encoded")
        + ("" if a_ok else " | audio re-encoded")
    )
    return out
