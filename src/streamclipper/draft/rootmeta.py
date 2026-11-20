"""Draft/output naming, root_meta_info.json backups and draft registration."""

import copy
import json
import os
import re
import shutil
import time
from pathlib import Path

from streamclipper.util import fwd

_ROOT_META_BACKED_UP = set()

# Every file one clip writes into its output folder; all of them share the clip name.
OUTPUT_SUFFIXES = (
    ".mp4",
    ".clip.json",
    "_spec.json",
    ".timeline.json",
    "_timeline.json",
    "_thumb.jpg",
    ".graph.txt",
    ".gate.json",
)
RENDER_SUFFIXES = (".mp4", ".timeline.json", "_thumb.jpg", ".graph.txt", ".gate.json")
ROLLING_BACKUPS = 5
_ROLLING_RE = re.compile(r"\.streamclipper_\d{8}-\d{6}\.bak$")


def unique_draft_name(draft_root: Path, name: str) -> str:
    if not (draft_root / name).exists():
        return name
    k = 2
    while (draft_root / f"{name}_{k}").exists():
        k += 1
    return f"{name}_{k}"


def unique_output_name(out_dir, name: str, draft_root=None, suffixes: tuple = OUTPUT_SUFFIXES) -> str:
    """
    First of name, name_2, name_3, ... for which no <n><suffix> exists in out_dir and no draft folder
    <draft_root>/<n> exists (draft_root None = no draft is written). MP4, clip plan, sidecars, thumbnail
    and draft then all share the returned name.
    """
    out_dir = Path(out_dir) if out_dir else None
    root = Path(draft_root) if draft_root else None

    def taken(n: str) -> bool:
        if out_dir is not None and any((out_dir / f"{n}{s}").exists() for s in suffixes):
            return True
        return root is not None and (root / n).exists()

    if not taken(name):
        return name
    k = 2
    while taken(f"{name}_{k}"):
        k += 1
    return f"{name}_{k}"


def backup_root_meta(rm_path: Path, keep: int = ROLLING_BACKUPS, now: float = None):
    """
    Before the first root_meta_info.json write of this run: keep root_meta_info.json.streamclipper_first.bak
    (written once, never overwritten) and add root_meta_info.json.streamclipper_<YYYYmmdd-HHMMSS>.bak.
    Only our own timestamped backups are pruned, to the newest `keep`.
    """
    rm_path = Path(rm_path)
    key = str(rm_path.resolve())
    if key in _ROOT_META_BACKED_UP:
        return
    if rm_path.is_file():
        first = rm_path.with_name(rm_path.name + ".streamclipper_first.bak")
        if not first.exists():
            shutil.copy2(rm_path, first)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() if now is None else now))
        rolling = rm_path.with_name(f"{rm_path.name}.streamclipper_{stamp}.bak")
        if not rolling.exists():
            shutil.copy2(rm_path, rolling)
        ours = sorted(
            p for p in rm_path.parent.glob(f"{rm_path.name}.streamclipper_*.bak") if _ROLLING_RE.search(p.name)
        )
        for old in ours[: max(0, len(ours) - keep)]:
            old.unlink()
    _ROOT_META_BACKED_UP.add(key)


def register_draft(draft_root: Path, proj_dir: Path, name: str, draft_id: str, duration_us: int, entry_base: dict):
    """Back up root_meta_info.json, then add (or replace) the entry of one draft, written atomically."""
    draft_root, proj_dir = Path(draft_root), Path(proj_dir)
    rm_path = draft_root / "root_meta_info.json"
    backup_root_meta(rm_path)
    if rm_path.is_file():
        rm = json.loads(rm_path.read_text(encoding="utf-8"))
    else:
        rm = {"all_draft_store": [], "draft_ids": 0, "root_path": fwd(draft_root)}
    now_us = int(time.time() * 1_000_000)
    rentry = copy.deepcopy(entry_base)
    rentry["draft_id"] = draft_id
    rentry["draft_name"] = name
    rentry["draft_fold_path"] = fwd(proj_dir)
    rentry["draft_json_file"] = fwd(proj_dir) + "\\draft_content.json"
    rentry["draft_root_path"] = fwd(draft_root)
    rentry["tm_duration"] = duration_us
    rentry["tm_draft_create"] = now_us
    rentry["tm_draft_modified"] = now_us
    rentry["streaming_edit_draft_ready"] = True
    stores = [
        s
        for s in rm.get("all_draft_store", [])
        if s.get("draft_name") != name and s.get("draft_fold_path") != fwd(proj_dir)
    ]
    rm["all_draft_store"] = [rentry] + stores
    rm["draft_ids"] = len(rm["all_draft_store"])
    tmp = rm_path.with_name(rm_path.name + ".tmp")
    tmp.write_text(json.dumps(rm, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")  # CapCut's compact form
    os.replace(tmp, rm_path)
