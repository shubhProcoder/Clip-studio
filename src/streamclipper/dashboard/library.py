"""Clips library of one dashboard job: list, mark, delete (to trash), Open in CapCut, re-render."""

import json
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from streamclipper import env as _env
from streamclipper.captions.styles import AUTO_STYLE, HOOK_STYLES, STYLES
from streamclipper.colab import zip_drafts
from streamclipper.dashboard.jobs import build_render_argv
from streamclipper.draft import importer
from streamclipper.draft.rootmeta import OUTPUT_SUFFIXES, RENDER_SUFFIXES, unique_output_name
from streamclipper.util import ToolError

KEY_RE = re.compile(r"^[0-9A-Za-z_\-]{1,80}$")
MARKS = ("posted", "rejected")
CAPCUT_OPEN_MSG = "CapCut is open. Close CapCut first, then click Open in CapCut again."
_GATE_NAME_RE = re.compile(r"EXPORT GATE: (\S+)\.mp4")


class CapCutOpen(ToolError):
    """Open in CapCut while CapCut runs: CapCut rewrites its draft list on exit, so nothing is imported."""


def check_key(key: str) -> str:
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise ToolError("Unknown clip.", "reload the page")
    return key


def _inside(path: Path, base: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path(base).resolve())
    except (OSError, ValueError):
        return False


def load_doc(out: Path) -> dict:
    p = Path(out) / "clips.json"
    if not p.is_file():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def clip_key(c: dict) -> str:
    mp4 = c.get("mp4")
    if mp4:
        return Path(str(mp4)).stem
    return f"rank{int(c.get('rank') or 0)}"


def _file_url(job_id: str, out: Path, rel) -> str:
    """URL of a file inside <job>/out, or '' when it is missing or outside the folder."""
    if not rel:
        return ""
    p = (out / str(rel)).resolve()
    if not p.is_file() or not _inside(p, out):
        return ""
    return f"/files/{job_id}/out/" + quote(p.relative_to(out.resolve()).as_posix())


def gate_rows(out: Path, name: str):
    p = Path(out) / f"{name}.gate.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {"pass": bool(data.get("pass")), "rows": [[str(x) for x in r[:3]] for r in data.get("rows") or []]}


def _plan(out: Path, key: str) -> dict:
    p = Path(out) / f"{key}.clip.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def list_clips(store, job_id: str, statuses: dict = None) -> list:
    """Clips of a job (clips.json) with file URLs, audit rows, marks and re-rendered variants."""
    job = store.get(job_id)
    jdir = store.job_dir(job_id)
    out = jdir / "out"
    doc = load_doc(out)
    deleted = set(job.get("deleted") or [])
    marks = job.get("marks") or {}
    statuses = statuses or {}
    res = []
    for c in sorted(doc.get("clips") or [], key=lambda c: c.get("rank", 0)):
        key = clip_key(c)
        if key in deleted:
            continue
        plan = _plan(out, key)
        draft = plan.get("draft") or ""
        variants = []
        for v in (job.get("variants") or {}).get(key, []):
            if v["name"] in deleted:
                continue
            variants.append(
                {
                    **v,
                    "status": statuses.get(v.get("job"), "done"),
                    "mp4_url": _file_url(job_id, out, f"{v['name']}.mp4"),
                    "thumb_url": _file_url(job_id, out, f"{v['name']}_thumb.jpg"),
                    "gate": gate_rows(out, v["name"]),
                    "mark": marks.get(v["name"]),
                }
            )
        res.append(
            {
                "key": key,
                "rank": c.get("rank"),
                "start": c.get("start"),
                "end": c.get("end"),
                "duration": c.get("duration"),
                "score": c.get("score"),
                "breakdown": c.get("breakdown") or {},
                "category": c.get("category") or "",
                "caption_style": c.get("caption_style") or "",
                "hook_headline": c.get("hook_headline") or "",
                "title": c.get("title") or "",
                "description": c.get("description") or "",
                "hashtags": list(c.get("hashtags") or []),
                "reason": c.get("reason") or "",
                "latency_s": c.get("latency_s"),
                "mp4_url": _file_url(job_id, out, c.get("mp4")),
                "thumb_url": _file_url(job_id, out, c.get("thumb")),
                "preview_url": _file_url(job_id, out, c.get("preview")),
                "gate": gate_rows(out, key) if c.get("mp4") else None,
                "draft": draft if draft and (jdir / "capcut_drafts" / draft).is_dir() else "",
                "imported": (job.get("imported") or {}).get(key),
                "mark": marks.get(key),
                "publish": dict((job.get("publish") or {}).get(key) or {}),
                "variants": variants,
            }
        )
    return res


def _known_keys(store, job_id: str) -> set:
    job = store.get(job_id)
    out = store.job_dir(job_id) / "out"
    keys = {clip_key(c) for c in load_doc(out).get("clips") or []}
    keys |= {v["name"] for vs in (job.get("variants") or {}).values() for v in vs}
    return keys - set(job.get("deleted") or [])


def mark(store, job_id: str, key: str, value) -> dict:
    check_key(key)
    if value not in (*MARKS, None, ""):
        raise ToolError("Unknown mark.", "use posted, rejected or none")
    if key not in _known_keys(store, job_id):
        raise ToolError("Unknown clip.", "reload the page")

    def fn(job):
        marks = job.setdefault("marks", {})
        if value:
            marks[key] = value
        else:
            marks.pop(key, None)
        return dict(marks)

    return store.mutate(job_id, fn)


def delete_clip(store, ws: Path, job_id: str, key: str, confirm: bool = False) -> list:
    """
    Move the clip's own files (<out>/<key><suffix>, its re-rendered variants and its portable draft) to
    <workspace>/trash/<job>/<time>/. Nothing is deleted for good; other files in the folder stay.
    """
    check_key(key)
    if confirm is not True:
        raise ToolError("Delete needs a confirmation.", "click Delete again and confirm")
    if key not in _known_keys(store, job_id):
        raise ToolError("Unknown clip.", "reload the page")
    job = store.get(job_id)
    jdir = store.job_dir(job_id)
    out = jdir / "out"
    names = [key] + [v["name"] for v in (job.get("variants") or {}).get(key, [])]
    sources = [out / f"{n}{s}" for n in names for s in OUTPUT_SUFFIXES]
    draft = _plan(out, key).get("draft") or ""
    if draft and KEY_RE.match(draft):
        sources.append(jdir / "capcut_drafts" / draft)
    sources = [p for p in sources if p.exists() and _inside(p, jdir)]
    trash = Path(ws).resolve() / "trash" / job_id / time.strftime("%Y%m%d-%H%M%S")
    k = 2
    while trash.exists():
        trash = trash.with_name(f"{trash.name.split('_')[0]}_{k}")
        k += 1
    moved = []
    for p in sources:
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(trash / p.name))
        moved.append(p.name)

    def fn(j):
        d = j.setdefault("deleted", [])
        for n in names:
            if n not in d:
                d.append(n)

    store.mutate(job_id, fn)
    return moved


def open_in_capcut(store, job_id: str, key: str, draft_root: str = "") -> str:
    """Import the clip's portable draft into CapCut's draft list (the import-draft path). Never closes CapCut."""
    check_key(key)
    if _env.capcut_running():
        raise CapCutOpen(CAPCUT_OPEN_MSG, "save your work in CapCut and close it")
    jdir = store.job_dir(job_id)
    draft = _plan(jdir / "out", key).get("draft") or ""
    droot = jdir / "capcut_drafts"
    if not draft or not KEY_RE.match(draft) or not (droot / draft).is_dir():
        raise ToolError("This job ran without CapCut drafts.", "start a new job with 'CapCut drafts' ticked")
    tmp = jdir / "tmp"
    tmp.mkdir(exist_ok=True)
    zp = zip_drafts(droot, [draft], tmp / f"{key}_draft.zip")
    try:
        res = importer.import_draft_zip(zp, draft_root=draft_root or "", overwrite=False, close_capcut=False)
    finally:
        zp.unlink(missing_ok=True)
    name = res[0]["name"] if res else draft

    def fn(job):
        job.setdefault("imported", {})[key] = name

    store.mutate(job_id, fn)
    return name


def rerender(store, runner, job_id: str, key: str, caption_style: str = "", hook_style: str = "") -> dict:
    """Queue `render --spec <out>/<key>.clip.json` with other styles; the MP4 gets the next free _2 name."""
    check_key(key)
    if caption_style and caption_style not in (AUTO_STYLE, *STYLES):
        raise ToolError(f"Unknown caption style: {caption_style}", "pick one from the list")
    if hook_style and hook_style not in HOOK_STYLES:
        raise ToolError(f"Unknown hook style: {hook_style}", "pick one from the list")
    if not caption_style and not hook_style:
        raise ToolError("Pick another caption or hook style.", "choose a style, then click Re-render")
    parent = store.get(job_id)
    if key in set(parent.get("deleted") or []):
        raise ToolError("Unknown clip.", "reload the page")
    out = store.job_dir(job_id) / "out"
    plan_path = out / f"{key}.clip.json"
    if not plan_path.is_file():
        raise ToolError("This clip has no clip plan to re-render.", "re-render clips made with an MP4 or draft")
    base = _plan(out, key).get("name") or key
    # the name `render` will pick (unique_output_name), skipping names of re-renders still in the queue
    reserved = {v["name"] for vs in (parent.get("variants") or {}).values() for v in vs}

    def taken(n: str) -> bool:
        return n in reserved or unique_output_name(out, n, suffixes=RENDER_SUFFIXES) != n

    name, k = base, 1
    while taken(name):
        k += 1
        name = f"{base}_{k}"
    argv = build_render_argv(plan_path, caption_style, hook_style)
    label = f"Re-render {key} ({caption_style or 'same captions'}, {hook_style or 'same hook'})"
    opts = {"caption_style": caption_style, "hook_style": hook_style, "clip": key}
    job = store.create("render", label, argv, opts, parent=job_id)
    variant = {"name": name, "job": job["id"], "caption_style": caption_style, "hook_style": hook_style}
    store.mutate(job_id, lambda j: j.setdefault("variants", {}).setdefault(key, []).append(variant))
    runner.submit(job["id"])
    return variant


def fix_variant_name(store, job: dict) -> None:
    """After a re-render job: take the MP4 name from its log ('EXPORT GATE: <name>.mp4')."""
    if job.get("kind") != "render" or not job.get("parent"):
        return
    log = store.job_dir(job["id"]) / "log.txt"
    try:
        found = _GATE_NAME_RE.findall(log.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return
    if not found or not KEY_RE.match(found[-1]):
        return
    actual = found[-1]

    def fn(parent):
        for vs in (parent.get("variants") or {}).values():
            for v in vs:
                if v.get("job") == job["id"]:
                    v["name"] = actual

    store.mutate(job["parent"], fn)
