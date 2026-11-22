"""
clips.csv and a static index.html next to clips.json (single file, no scripts, no external URLs except the
opt-in Drive links of Colab's share_on_drive).
"""

import csv
import html
import os
from pathlib import Path
from urllib.parse import quote

from streamclipper.audio import _mmss
from streamclipper.scoring import COMPONENTS
from streamclipper.util import ToolError, run_cmd

CSV_COLUMNS = (
    "rank",
    "start",
    "end",
    "duration",
    "score",
    *COMPONENTS,
    "category",
    "caption_style",
    "title",
    "hook_headline",
    "reason",
    "mp4",
    "thumb",
    "drive_link",
)

_CSS = """
body{font-family:system-ui,Segoe UI,Arial,sans-serif;background:#f6f7f9;color:#1d2330;margin:0;padding:24px}
h1{font-size:22px;margin:0 0 4px}.meta{color:#5b6475;margin:0 0 20px;font-size:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.card{background:#fff;border:1px solid #e1e4ea;border-radius:10px;overflow:hidden}
.card img{width:100%;aspect-ratio:16/9;object-fit:cover;background:#e9ecf1;display:block}
.card .thumb{aspect-ratio:9/16;object-fit:contain}
.body{padding:12px 14px}.top{display:flex;justify-content:space-between;align-items:center}
.rank{font-weight:700;font-size:15px}.cat{background:#eef2ff;color:#3730a3;border-radius:99px;padding:2px 10px;
font-size:12px}.title{font-weight:600;margin:8px 0 2px}.hook{color:#5b6475;font-size:13px;margin:0 0 8px}
.bar{height:8px;background:#e9ecf1;border-radius:4px;overflow:hidden;margin:6px 0}
.bar span{display:block;height:100%;background:#16a34a}.score{font-size:13px;color:#1d2330}
table{width:100%;font-size:12px;border-collapse:collapse;margin:6px 0}td{padding:1px 0}td.v{text-align:right}
.reason{font-size:13px;color:#374151;margin:6px 0}.links a{font-size:13px;margin-right:10px;color:#1d4ed8}
.tags{font-size:12px;color:#5b6475}.restyle{font-size:12px;color:#5b6475;margin:8px 0 2px}
.cmd{font-family:Consolas,monospace;font-size:11px;background:#f1f3f6;border-radius:6px;padding:6px;
word-break:break-all;user-select:all;margin:0}
"""


def _link(path: str) -> str:
    return quote(str(path).replace("\\", "/"))


def rel_path(path, out_dir: Path) -> str:
    """Path relative to out_dir (forward slashes), or an absolute path on another drive."""
    try:
        return os.path.relpath(Path(path), out_dir).replace("\\", "/")
    except ValueError:
        return Path(path).resolve().as_posix()


def make_previews(out_dir: Path, doc: dict, video: Path) -> None:
    """previews/NN.jpg (480x270) at each clip's peak, from a local video. Existing previews are kept."""
    video = Path(video)
    if not video.is_file():
        return
    pdir = Path(out_dir) / "previews"
    pdir.mkdir(parents=True, exist_ok=True)
    for c in doc["clips"]:
        jpg = pdir / f"{int(c['rank']):02d}.jpg"
        if not jpg.is_file():
            try:
                run_cmd(
                    [
                        "ffmpeg",
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-ss",
                        f"{float(c['peak']):.2f}",
                        "-i",
                        str(video),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=480:270:force_original_aspect_ratio=decrease,pad=480:270:(ow-iw)/2:(oh-ih)/2",
                        "-q:v",
                        "4",
                        str(jpg),
                    ],
                    f"preview frame for clip {c['rank']}",
                )
            except ToolError as e:
                print(f"Preview for clip {c['rank']} skipped: {str(e).splitlines()[0]}")
                continue
        if jpg.is_file():
            c["preview"] = rel_path(jpg, Path(out_dir))


ALT_STYLES = ("bold-pop", "karaoke-highlight", "comic-burst", "minimal-clean")


def restyle_command(c: dict, source: str = "", clips_name: str = "clips.json") -> str:
    """The command that renders this clip again with another caption style (shown in index.html)."""
    alt = next(s for s in ALT_STYLES if s != c.get("caption_style"))
    if c.get("mp4"):
        plan = str(c["mp4"])[:-4] + ".clip.json"
        return f'streamclipper render --spec "{plan}" --caption-style {alt}'
    rank = c.get("rank", 1)
    if source and not str(source).startswith(("http://", "https://")):
        return f'streamclipper render --clips {clips_name} --video "{source}" --max-clips {rank} --caption-style {alt}'
    return f"streamclipper clip-stream --url {source} --out <folder> --max-clips {rank} --render --caption-style {alt}"


def _card(c: dict, source: str = "", clips_name: str = "clips.json") -> str:
    e = html.escape
    img = ""
    if c.get("thumb"):
        img = f'<img class="thumb" src="{_link(c["thumb"])}" alt="thumbnail of clip {c["rank"]}">'
    elif c.get("preview"):
        img = f'<img src="{_link(c["preview"])}" alt="frame at the peak of clip {c["rank"]}">'
    rows = "".join(
        f'<tr><td>{k}</td><td class="v">{"-" if v is None else v}</td></tr>'
        for k, v in ((k, (c.get("breakdown") or {}).get(k)) for k in COMPONENTS)
    )
    links = []
    if c.get("mp4"):
        links.append(f'<a href="{_link(c["mp4"])}">MP4</a>')
    if c.get("thumb"):
        links.append(f'<a href="{_link(c["thumb"])}">Thumbnail</a>')
    if str(c.get("drive_link") or "").startswith("https://"):
        links.append(f'<a href="{e(c["drive_link"])}">Drive link</a>')
    tags = " ".join(c.get("hashtags") or [])
    score = max(0, min(100, int(c.get("score") or 0)))
    return (
        f'<div class="card">{img}<div class="body">'
        f'<div class="top"><span class="rank">#{c["rank"]} &middot; {_mmss(c["start"])}-{_mmss(c["end"])} '
        f'({c["duration"]:.0f} s)</span><span class="cat">{e(str(c.get("category") or "reaction"))}</span></div>'
        f'<div class="title">{e(str(c.get("title") or c.get("hook_headline") or ""))}</div>'
        f'<p class="hook">Hook: {e(str(c.get("hook_headline") or ""))}</p>'
        f'<div class="score">Score {score}/100</div><div class="bar"><span style="width:{score}%"></span></div>'
        f"<table>{rows}</table>"
        f'<p class="reason">{e(str(c.get("reason") or ""))}</p>'
        + (f'<p class="tags">{e(tags)}</p>' if tags else "")
        + (f'<div class="links">{"".join(links)}</div>' if links else "")
        + f'<p class="restyle">Captions: {e(str(c.get("caption_style") or "-"))}. Re-render with another style '
        "(any name from <code>streamclipper styles</code>):</p>"
        f'<p class="cmd">{e(restyle_command(c, source, clips_name))}</p>' + "</div></div>"
    )


def write_report(
    out_dir: Path, doc: dict, video: Path = None, stem: str = "clips", html_name: str = "index.html"
) -> tuple:
    """
    Write <stem>.csv (UTF-8 with BOM for Excel) and html_name (index.html) into out_dir; with a local video also
    previews/NN.jpg. Returns (csv path, html path).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    clips = sorted(doc.get("clips") or [], key=lambda c: c.get("rank", 0))
    if video:
        make_previews(out_dir, {"clips": clips}, video)
    csv_p = out_dir / f"{stem}.csv"
    with open(csv_p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for c in clips:
            bd = c.get("breakdown") or {}
            w.writerow(
                [c.get("rank"), c.get("start"), c.get("end"), c.get("duration"), c.get("score")]
                + [bd.get(k) if bd.get(k) is not None else "" for k in COMPONENTS]
                + [c.get(k) or "" for k in CSV_COLUMNS[5 + len(COMPONENTS) :]]
            )
    title = html.escape(str(doc.get("title") or doc.get("source") or "Stream"))
    src = str(doc.get("source") or "")
    st = doc.get("settings") or {}
    meta = (
        f"{len(clips)} clips &middot; stream {_mmss(doc.get('duration') or 0)} &middot; min score "
        f"{st.get('min_score', '-')} &middot; transcript {html.escape(str(doc.get('language') or '-'))} &middot; "
        f"LLM {html.escape(str(st.get('llm') or 'none'))} &middot; chat {'yes' if st.get('chat') else 'no'}"
    )
    page = (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Clips: {title}</title><style>{_CSS}</style></head><body>"
        f'<h1>Clips: {title}</h1><p class="meta">{meta}. Ranked best first; post from the top.</p>'
        f'<div class="grid">{"".join(_card(c, src, stem + ".json") for c in clips)}</div></body></html>\n'
    )
    html_p = out_dir / html_name
    html_p.write_text(page, encoding="utf-8")
    return csv_p, html_p
