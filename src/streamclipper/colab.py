"""
Google Colab helpers. The notebook only sets form values and calls run_pipeline(), so the whole Colab run
can be tested on a normal PC. No web UI or servers: outputs go to a folder (Drive on Colab, or /content
without Drive, then downloaded as a zip). Drive share links only when the user ticks share_on_drive.
"""

import base64
import html
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

from streamclipper.env import cuda_status
from streamclipper.env import gpu_info as _gpu_name_vram
from streamclipper.util import ToolError, fwd, set_ytdlp_cookies

BOT_BLOCK_MARKERS = ("sign in to confirm", "not a bot", "confirm you're not a bot", "confirm you are not a bot")
BOT_BLOCK_HINT = (
    "YouTube often blocks Colab servers. Two options: (1) download the VOD on your PC, upload it to Google Drive "
    "and use its Drive path as the source (e.g. /content/drive/MyDrive/my_stream.mp4); or (2) export a "
    "cookies.txt from a browser where you are signed in to YouTube, upload it to Drive and set the cookies path. "
    "A cookies file gives access to your account: never share it, and delete it from Drive after the run."
)
DRAFTS_ZIP = "capcut_drafts.zip"


def is_colab() -> bool:
    return "google.colab" in sys.modules or bool(os.environ.get("COLAB_RELEASE_TAG"))


def gpu_info() -> dict:
    """{'name', 'vram_mb', 'cuda', 'detail'}; cuda is True when faster-whisper can use the GPU."""
    name, vram = _gpu_name_vram()
    try:
        ok, detail = cuda_status()
    except Exception as e:  # never stop the notebook on a GPU probe
        ok, detail = False, str(e)
    return {"name": name, "vram_mb": vram, "cuda": bool(ok), "detail": detail}


def drive_out_dir(folder: str = "StreamClipper") -> Path:
    """$STREAMCLIPPER_OUT/<folder> when set, /content/drive/MyDrive/<folder> on Colab with Drive, else ./<folder>."""
    env_out = os.environ.get("STREAMCLIPPER_OUT")
    if env_out:
        return Path(env_out) / folder
    drive = Path("/content/drive/MyDrive")
    base = drive if is_colab() and drive.is_dir() else Path.cwd()
    return base / folder


LIVE_SYNC_FILES = ("clips.json", "clips.csv", "index.html", "live_summary.json")


def live_work_dir(folder: str) -> Path:
    """
    Local disk for a live run's rolling buffer, windows and panels: $STREAMCLIPPER_WORK/<folder> when set,
    /content/streamclipper_live/<folder> on Colab, else ./streamclipper_live/<folder>. Never the Drive
    mount: segments written and deleted there would go to Drive's trash and count against the quota.
    """
    env_work = os.environ.get("STREAMCLIPPER_WORK")
    if env_work:
        return Path(env_work) / folder
    base = Path("/content") if is_colab() and Path("/content").is_dir() else Path.cwd()
    return base / "streamclipper_live" / folder


def sync_live_outputs(work: Path, dest: Path) -> list:
    """
    Copy the finished results of a live run from local disk to Drive: <name>.mp4, <name>_thumb.jpg,
    clips.json, clips.csv, index.html and live_summary.json (top-level files only; buffer/, windows/,
    panels/ and half-written .part files stay local). A file is copied again only when it changed.
    Returns the copied file names.
    """
    work, dest = Path(work), Path(dest)
    if not work.is_dir():
        return []
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for p in sorted(work.iterdir()):
        name = p.name
        clip = name.endswith(".mp4") and not name.endswith(".part.mp4")
        if not p.is_file() or not (clip or name.endswith("_thumb.jpg") or name in LIVE_SYNC_FILES):
            continue
        st, target = p.stat(), dest / name
        if target.is_file() and target.stat().st_size == st.st_size and target.stat().st_mtime >= st.st_mtime:
            continue
        tmp = target.with_name(name + ".tmp")
        shutil.copy2(p, tmp)
        os.replace(tmp, target)
        copied.append(name)
    return copied


def is_bot_block(text: str) -> bool:
    low = str(text or "").lower()
    return any(m in low for m in BOT_BLOCK_MARKERS)


def zip_drafts(draft_root: Path, names: list, zip_path: Path) -> Path:
    """Zip the given draft folders (paths inside the zip start with the draft name)."""
    draft_root, zip_path = Path(draft_root), Path(zip_path)
    tmp = zip_path.with_name(zip_path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for name in names:
            d = draft_root / name
            for f in sorted(d.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(draft_root).as_posix())
    os.replace(tmp, zip_path)
    return zip_path


def _clip_argv(source: str, work: Path, o: dict) -> list:
    argv = ["clip-stream", "--out", str(work), "--prefix", o["prefix"]]
    if re.match(r"^https?://", source, re.I):
        argv += ["--url", source]
    else:
        src = Path(source)
        if not src.is_file():
            raise ToolError(
                f"Source video not found: {source}",
                "use a stream link, or the Drive path of an uploaded file (e.g. /content/drive/MyDrive/my_stream.mp4)",
            )
        argv += ["--input", str(src.resolve())]
    argv += ["--max-clips", str(int(o["max_clips"])), "--min-score", str(float(o["min_score"]))]
    argv += ["--llm", o["llm"], "--whisper-model", o["whisper_model"]]
    argv += ["--device", o["device"], "--language", o["language"]]
    for flag, key in (
        ("--ratio", "ratio"),
        ("--preset", "preset"),
        ("--caption-style", "caption_style"),
        ("--hook-style", "hook_style"),
        ("--llm-model", "llm_model"),
        ("--chat", "chat"),
    ):
        if o.get(key):
            argv += [flag, str(o[key])]
    # 5.3 effects and HUD: only passed when not the default, so older callers get the same argv
    if (o.get("zoom") or "off") != "off":
        argv += ["--zoom", str(o["zoom"])]
    if o.get("remove_fillers"):
        argv.append("--remove-fillers")
    if (o.get("enhance_speech") or "off") != "off":
        argv += ["--enhance-speech", str(o["enhance_speech"])]
    if o.get("brand_kit"):
        argv += ["--brand-kit", str(o["brand_kit"])]
    if (o.get("hud") or "auto") != "auto":
        argv += ["--hud", str(o["hud"])]
    if o.get("static_analysis") is False:
        argv += ["--static-analysis", "off"]
    elif o.get("static_bucket") is not None:
        argv += ["--static-bucket", str(o["static_bucket"])]
    if o.get("static_min") is not None:
        argv += ["--static-min", str(o["static_min"])]
    if o.get("static_threshold") is not None:
        argv += ["--static-threshold", str(o["static_threshold"])]
    if o.get("game"):
        argv += ["--game", str(o["game"])]
    if o["render"]:
        argv.append("--render")
    if o["drafts"]:
        argv += ["--draft-root", str(o["draft_root"]), "--portable-drafts"]
    else:
        argv.append("--no-draft")
    if not o["resume"]:
        argv.append("--fresh")
    return argv


def run_pipeline(
    source: str,
    out_dir: str = "",
    preset: str = "",
    caption_style: str = "auto",
    hook_style: str = "bold-box",
    min_score: float = 50.0,
    max_clips: int = 10,
    ratio: str = "9:16",
    llm: str = "none",
    render: bool = True,
    drafts: bool = True,
    draft_root: str = "",
    cookies: str = "",
    resume: bool = True,
    whisper_model: str = "auto",
    device: str = "auto",
    language: str = "auto",
    prefix: str = "clip",
    llm_model: str = "",
    chat: str = "",
    zoom: str = "off",
    remove_fillers: bool = False,
    enhance_speech: str = "off",
    brand_kit: str = "",
    hud: str = "auto",
) -> dict:
    """
    Stream -> ranked clips (clips.json, clips.csv, index.html) -> MP4s + thumbnails (render) -> portable
    CapCut drafts zipped to <out_dir>/capcut_drafts.zip (drafts). Same code path as 'streamclipper clip-stream'.
    Progress is saved in <out_dir>; running it again with the same inputs resumes (resume=False starts over).
    max_clips = how many clips get an MP4/draft (0 = every clip above min_score; all clips go to clips.json).
    Effects (all off by default): zoom off|light|medium|strong, remove_fillers, enhance_speech off|light|strong,
    brand_kit = path of a brand kit JSON (e.g. on Drive). hud = auto|off|gta5|gta6|generic.
    """
    from streamclipper.cli import build_parser  # late import: cli imports every command module
    from streamclipper.commands import clip_stream

    out = Path(out_dir or drive_out_dir()).resolve()
    out.mkdir(parents=True, exist_ok=True)
    droot = Path(draft_root).resolve() if draft_root else out / "capcut_drafts"
    if drafts:
        droot.mkdir(parents=True, exist_ok=True)
    opts = {
        "prefix": prefix,
        "max_clips": max_clips,
        "min_score": min_score,
        "ratio": ratio,
        "llm": llm or "none",
        "whisper_model": whisper_model or "auto",
        "device": device or "auto",
        "language": language or "auto",
        "preset": preset,
        "caption_style": caption_style,
        "hook_style": hook_style,
        "llm_model": llm_model,
        "chat": chat,
        "render": render,
        "drafts": drafts,
        "draft_root": droot,
        "resume": resume,
        "zoom": zoom or "off",
        "remove_fillers": bool(remove_fillers),
        "enhance_speech": enhance_speech or "off",
        "brand_kit": str(brand_kit or "").strip(),
        "hud": hud or "auto",
    }
    if opts["brand_kit"] and not Path(opts["brand_kit"]).is_file():
        raise ToolError("The brand kit file was not found.", "check the brand_kit path, or leave it empty")
    args = build_parser().parse_args(_clip_argv(str(source).strip(), out, opts))
    if cookies:
        if not Path(cookies).is_file():
            raise ToolError("The cookies file was not found.", "check the cookies path, or leave it empty")
        print("Using your cookies file for yt-dlp. It gives access to your account: never share it.")
        set_ytdlp_cookies(cookies)
    try:
        summary = clip_stream(args)
    except ToolError as e:
        if is_bot_block(f"{e} {e.hint}"):
            raise ToolError("YouTube asked to 'Sign in to confirm you're not a bot'.", BOT_BLOCK_HINT) from None
        raise
    finally:
        set_ytdlp_cookies(None)

    result = collect_outputs(out, summary, droot if drafts else None)
    failed = [name for name, ok in summary["gate"] if not ok]
    if failed:
        raise ToolError(
            f"{len(failed)} MP4(s) failed the export gate: {', '.join(failed)} (see the tables above). "
            "The other outputs are saved.",
            f"check {fwd(out)}; re-run with other settings or skip those clips",
        )
    return result


def collect_outputs(out: Path, summary: dict, draft_root) -> dict:
    """Paths of everything a run produced, plus the drafts zip. Printed as a short list."""
    clips_json = out / "clips.json"
    doc = summary.get("doc") or {}
    plans = summary.get("plans") or []
    if summary.get("gate"):
        print("RENDER SUMMARY")
        for name, ok in summary["gate"]:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    mp4s = [out / f"{p['name']}.mp4" for p in plans if (out / f"{p['name']}.mp4").is_file()]
    thumbs = [m.with_name(f"{m.stem}_thumb.jpg") for m in mp4s if m.with_name(f"{m.stem}_thumb.jpg").is_file()]
    zip_path = None
    if draft_root is not None:
        names = [p.get("draft") for p in plans if p.get("draft") and (Path(draft_root) / p["draft"]).is_dir()]
        if names:
            zip_path = zip_drafts(Path(draft_root), names, out / DRAFTS_ZIP)
    result = {
        "out_dir": out,
        "clips_json": clips_json,
        "clips_csv": out / "clips.csv",
        "index_html": out / "index.html",
        "mp4s": mp4s,
        "thumbs": thumbs,
        "drafts_zip": zip_path,
        "clips": doc.get("clips") or [],
    }
    print("OUTPUTS")
    print(f"  folder      {fwd(out)}")
    print(f"  clips       {len(result['clips'])} in clips.json, clips.csv, index.html")
    print(f"  MP4s        {len(mp4s)} (+ {len(thumbs)} thumbnails)")
    if zip_path:
        print(f"  drafts zip  {fwd(zip_path)}  ->  on your PC: streamclipper import-draft --zip {DRAFTS_ZIP}")
    return result


ZIP_SUFFIXES = (".mp4", ".jpg", ".csv", ".html", ".json", ".zip")


def zip_outputs(out_dir, zip_path) -> Path:
    """The top-level results of a run (MP4s, thumbnails, clips.csv/json, index.html, drafts zip) in one zip."""
    out_dir, zip_path = Path(out_dir), Path(zip_path)
    tmp = zip_path.with_name(zip_path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w") as zf:
        for p in sorted(out_dir.iterdir()):
            if not p.is_file() or p.suffix.lower() not in ZIP_SUFFIXES or p.resolve() == zip_path.resolve():
                continue
            if p.name.endswith((".part.mp4", ".tmp")):
                continue
            stored = p.suffix.lower() in (".mp4", ".zip", ".jpg")  # already compressed
            zf.write(p, p.name, compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
    os.replace(tmp, zip_path)
    return zip_path


_COPY_JS = (
    "navigator.clipboard.writeText(this.parentElement.querySelector('.sc-t').innerText)"
    ".then(()=>{this.innerText='Copied'});return false;"
)


def _copy_line(label: str, text: str) -> str:
    if not text:
        return ""
    return (
        f"<div style='margin:4px 0'><b>{label}:</b> <span class='sc-t'>{html.escape(text)}</span> "
        f"<button onclick=\"{_COPY_JS}\" style='font-size:11px;padding:1px 8px;cursor:pointer'>Copy</button></div>"
    )


def results_html(result: dict, previews: int = 3, max_preview_mb: float = 15.0) -> str:
    """
    Result cards for IPython.display.HTML: thumbnail, an inline <video> for the first `previews` MP4s (base64,
    only files up to max_preview_mb so the notebook stays small), score, category, title/description/hashtags
    with Copy buttons and the Drive link when share_on_drive made one.
    """
    out = Path(result["out_dir"])
    cards, shown = [], 0
    for c in sorted(result.get("clips") or [], key=lambda c: c.get("rank", 0)):
        mp4 = out / c["mp4"] if c.get("mp4") else None
        media = ""
        if (
            mp4 is not None
            and shown < previews
            and mp4.is_file()
            and mp4.stat().st_size <= max_preview_mb * 1024 * 1024
        ):
            b64 = base64.b64encode(mp4.read_bytes()).decode("ascii")
            media = f"<video width='220' controls src='data:video/mp4;base64,{b64}'></video>"
            shown += 1
        elif c.get("thumb") and (out / c["thumb"]).is_file():
            b64 = base64.b64encode((out / c["thumb"]).read_bytes()).decode("ascii")
            media = f"<img width='220' src='data:image/jpeg;base64,{b64}' alt='thumbnail'>"
        score = max(0, min(100, int(float(c.get("score") or 0))))
        drive = str(c.get("drive_link") or "")
        link = (
            f"<div><a href='{html.escape(drive)}' target='_blank'>Drive link</a> "
            "(anyone with the link can see it)</div>"
            if drive.startswith("https://")
            else ""
        )
        cards.append(
            "<div style='display:inline-block;vertical-align:top;width:240px;margin:8px;padding:10px;"
            "border:1px solid #ccc;border-radius:10px;font-size:13px'>"
            f"{media}<div style='font-weight:bold;margin-top:6px'>#{html.escape(str(c.get('rank')))} &middot; "
            f"score {score} &middot; {html.escape(str(c.get('category') or ''))}</div>"
            f"<div style='height:6px;background:#eee;border-radius:3px'><div style='width:{score}%;height:6px;"
            "background:#8B5CF6;border-radius:3px'></div></div>"
            + _copy_line("Title", str(c.get("title") or c.get("hook_headline") or ""))
            + _copy_line("Description", str(c.get("description") or ""))
            + _copy_line("Hashtags", " ".join(c.get("hashtags") or []))
            + (f"<div style='color:#666'>{html.escape(str(c['mp4']))}</div>" if c.get("mp4") else "")
            + link
            + "</div>"
        )
    if not cards:
        return "<p>No clip passed the quality bar. Try a lower min_score.</p>"
    return "<div>" + "".join(cards) + "</div>"
