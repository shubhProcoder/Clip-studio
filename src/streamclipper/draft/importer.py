"""import-draft: unpack a zip of portable CapCut drafts (e.g. from Colab) into a local draft root and relink it."""

import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from streamclipper.draft.rootmeta import register_draft, unique_draft_name
from streamclipper.draft.writer import (
    CAPCUT_LOCAL_TOKEN,
    FONTS_TOKEN,
    PORTABLE_MANIFEST,
    fonts_dir_fwd,
    guard_capcut,
    load_schema,
    resolve_draft_root,
)
from streamclipper.env import find_capcut_local
from streamclipper.util import ToolError, fwd, new_id

REPLACED_DIR = "streamclipper_replaced_drafts"  # next to the draft root; --overwrite moves old drafts here


def check_zip_entries(zf: zipfile.ZipFile) -> None:
    """Refuse absolute paths and '..' parts (zip-slip) before anything is extracted."""
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        parts = PurePosixPath(name).parts
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in parts:
            raise ToolError(
                f"Unsafe path in the zip: '{info.filename}'.",
                "only import zips made by streamclipper (capcut_drafts.zip); this one could write outside the folder",
            )


def _media_name(old: str, media: dict, media_dir: Path):
    """File name in <draft>/media for a material path: manifest entry first, else the same base name."""
    name = media.get(old)
    if name and (media_dir / name).is_file():
        return name
    base = PurePosixPath(str(old).replace("\\", "/")).name
    return base if base and (media_dir / base).is_file() else None


def relink_draft(proj_dir: Path, name: str, draft_root: Path, taken_ids: set) -> dict:
    """
    Point every video/audio material at <draft>/media, fill in the CapCut/font tokens for this PC and
    update the draft's own name/folder fields. Returns {'draft_id', 'duration', 'relinked', 'missing'}
    (relinked = number of distinct media files; missing = distinct paths not found).
    """
    manifest = {}
    mpath = proj_dir / PORTABLE_MANIFEST
    if mpath.is_file():
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    media = manifest.get("media") or {}
    cc_token = manifest.get("capcut_local_token") or CAPCUT_LOCAL_TOKEN
    font_token = manifest.get("fonts_token") or FONTS_TOKEN
    media_dir = proj_dir / "media"
    cc_local = fwd(find_capcut_local())
    fonts = fonts_dir_fwd()

    dc_path = proj_dir / "draft_content.json"
    dc = json.loads(dc_path.read_text(encoding="utf-8"))
    draft_id = dc.get("id") or new_id()
    if draft_id in taken_ids:  # the same zip imported twice: CapCut needs distinct ids
        draft_id = new_id()
    linked, missing = set(), []
    for kind in ("videos", "audios"):
        for m in dc.get("materials", {}).get(kind, []):
            old = m.get("path") or ""
            fname = _media_name(old, media, media_dir)
            if fname:
                m["path"] = fwd(media_dir / fname)
                linked.add(fname)
            elif not os.path.isfile(old) and old not in missing:
                missing.append(old)
    dc["id"] = draft_id
    dc["name"] = name
    dc_str = json.dumps(dc, ensure_ascii=False, separators=(",", ":"))
    dc_str = dc_str.replace(cc_token, cc_local).replace(font_token, fonts)
    dc_path.write_text(dc_str, encoding="utf-8")
    (proj_dir / "draft_content.json.bak").write_text(dc_str, encoding="utf-8")

    meta_p = proj_dir / "draft_meta_info.json"
    if meta_p.is_file():
        dmi = json.loads(meta_p.read_text(encoding="utf-8"))
        dmi.update(
            draft_id=draft_id,
            draft_name=name,
            draft_fold_path=fwd(proj_dir),
            draft_root_path=fwd(draft_root),
            tm_draft_modified=int(time.time() * 1_000_000),
        )
        meta_p.write_text(json.dumps(dmi, ensure_ascii=False), encoding="utf-8")
    return {"draft_id": draft_id, "duration": int(dc.get("duration") or 0), "relinked": len(linked), "missing": missing}


def import_draft_zip(zip_path, draft_root: str = "", overwrite: bool = False, close_capcut: bool = False) -> list:
    """Unpack, rename (_2 ...), move into the draft root, relink and register every draft in the zip."""
    zp = Path(zip_path).resolve()
    if not zp.is_file():
        raise ToolError(f"Zip not found: {fwd(zp)}", "pass the capcut_drafts.zip you downloaded from Colab/Drive")
    root = resolve_draft_root(draft_root or "").resolve()
    guard_capcut(root, close_capcut)
    try:
        zf = zipfile.ZipFile(zp)
    except zipfile.BadZipFile:
        raise ToolError(
            f"Not a zip file: {fwd(zp)}", "download capcut_drafts.zip again (it may be incomplete)"
        ) from None
    tmp = Path(tempfile.mkdtemp(prefix=".streamclipper_import_", dir=root.parent))
    results = []
    try:
        with zf:
            check_zip_entries(zf)
            zf.extractall(tmp)
        found = sorted({p.parent for p in tmp.rglob("draft_content.json")}, key=lambda p: str(p))
        if not found:
            raise ToolError("No CapCut draft in the zip (no draft_content.json).", "zip whole draft folders")
        entry_base = load_schema(find_capcut_local())["root_entry_base"]
        rm_path = root / "root_meta_info.json"
        rm = json.loads(rm_path.read_text(encoding="utf-8")) if rm_path.is_file() else {}
        taken_ids = {s.get("draft_id") for s in rm.get("all_draft_store", [])}
        for src in found:
            req = src.name
            name = req
            if (root / req).exists():
                if overwrite:
                    keep = root.parent / REPLACED_DIR / f"{req}_{time.strftime('%Y%m%d-%H%M%S')}"
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(root / req), str(keep))
                    print(f"Moved the old draft '{req}' to {fwd(keep)} (--overwrite).")
                else:
                    name = unique_draft_name(root, req)
            dst = root / name
            shutil.move(str(src), str(dst))
            info = relink_draft(dst, name, root, taken_ids)
            taken_ids.add(info["draft_id"])
            register_draft(root, dst, name, info["draft_id"], info["duration"], entry_base)
            results.append({"zip_name": req, "name": name, "dir": dst, **info})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"IMPORTED into {fwd(root)}")
    print(f"  {'IN ZIP':<24}{'DRAFT':<26}{'RELINKED':<10}MISSING")
    for r in results:
        print(f"  {r['zip_name']:<24}{r['name']:<26}{r['relinked']:<10}{len(r['missing'])}")
    for r in results:
        for m in r["missing"]:
            print(f"WARN: {r['name']}: media not found: {m}")
    renamed = [r for r in results if r["name"] != r["zip_name"]]
    if renamed:
        print(f"{len(renamed)} draft(s) got a _2 name because the name was taken (pass --overwrite to replace).")
    print("Open CapCut Desktop to see the drafts. Check one with: streamclipper audit --draft NAME")
    return results


def cmd_import_draft(args):
    import_draft_zip(args.zip, args.draft_root, overwrite=args.overwrite, close_capcut=args.close_capcut)
