"""
Dashboard brand kit: one kit per workspace in <workspace>/brand/brand_kit.json, with its logo, card images and
font next to it. The Settings page uploads the files and saves the fields; the server writes the JSON only
after brand.load_brand_kit accepted it. On the network (--lan) the kit is read-only.
"""

import json
import os
import re
from pathlib import Path

from streamclipper.brand import FONT_EXTS, IMAGE_EXTS, POSITIONS, load_brand_kit
from streamclipper.util import ToolError

MAX_UPLOAD = 20 * 1024 * 1024
KIT_NAME = "brand_kit.json"


def brand_dir(ws: Path) -> Path:
    return Path(ws) / "brand"


def kit_path(ws: Path) -> Path:
    return brand_dir(ws) / KIT_NAME


def safe_name(name: str) -> str:
    """A plain file name for an uploaded brand file (PNG/JPG image or TTF/OTF font)."""
    from streamclipper.dashboard.server import HttpError  # late import: server imports this module

    base = str(name or "").replace("\\", "/").split("/")[-1]
    base = re.sub(r"[^\w.\- ()]+", "_", base).strip(" .")
    stem, ext = os.path.splitext(base)
    if not stem or ext.lower() not in IMAGE_EXTS + FONT_EXTS:
        raise HttpError(415, "Brand files must be PNG or JPG images, or TTF/OTF fonts.")
    return stem[:80] + ext.lower()


def store_file(ws: Path, name: str, data: bytes) -> dict:
    d = brand_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / name
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return {"file": dest.name, "size": len(data)}


def _files(ws: Path) -> dict:
    d = brand_dir(ws)
    if not d.is_dir():
        return {"images": [], "fonts": []}
    names = sorted(p.name for p in d.iterdir() if p.is_file())
    return {
        "images": [n for n in names if Path(n).suffix.lower() in IMAGE_EXTS],
        "fonts": [n for n in names if Path(n).suffix.lower() in FONT_EXTS],
    }


def read(ws: Path, lan: bool = False) -> dict:
    """{kit (the saved JSON or None), files, read_only, error (when the saved kit no longer loads)}."""
    p = kit_path(ws)
    kit, error = None, ""
    if p.is_file():
        try:
            kit = json.loads(p.read_text(encoding="utf-8"))
            load_brand_kit(p)
        except (OSError, ValueError) as e:
            error = str(e)
        except ToolError as e:
            error = f"{e} ({e.hint})" if e.hint else str(e)
    return {"kit": kit, "files": _files(ws), "read_only": bool(lan), "error": error}


def _pick(name, allowed: list, what: str) -> str:
    name = str(name or "").strip()
    if name and name not in allowed:
        raise ToolError(f"{what} '{name}' is not in the brand folder.", "upload it first")
    return name


def save(ws: Path, body: dict) -> dict:
    """Settings form -> brand_kit.json (checked by load_brand_kit before it replaces the old one). clear: remove it."""
    p = kit_path(ws)
    if body.get("clear"):
        p.unlink(missing_ok=True)
        return {"message": "Brand kit removed.", **read(ws)}
    files = _files(ws)
    kit = {}
    logo = body.get("logo") or {}
    if isinstance(logo, dict) and logo.get("image"):
        pos = str(logo.get("position") or "auto")
        if pos not in POSITIONS:
            raise ToolError("Unknown logo position.", "pick one from the list")
        kit["logo"] = {
            "image": _pick(logo["image"], files["images"], "Logo"),
            "position": pos,
            "width_pct": logo.get("width_pct", 12),
            "opacity": logo.get("opacity", 0.85),
        }
    if str(body.get("handle") or "").strip():
        kit["handle"] = str(body["handle"]).strip()
    if body.get("font"):
        kit["font"] = _pick(body["font"], files["fonts"], "Font")
    cols = body.get("colours") or {}
    if isinstance(cols, dict):
        kit["colours"] = {k: str(cols[k]) for k in ("highlight", "hook_box") if cols.get(k)}
    for which in ("intro", "outro"):
        c = body.get(which) or {}
        if isinstance(c, dict) and (c.get("image") or str(c.get("text") or "").strip()):
            card = {"seconds": c.get("seconds", 1.0), "text": str(c.get("text") or "").strip()}
            if c.get("image"):
                card["image"] = _pick(c["image"], files["images"], f"{which.title()} image")
            kit[which] = card
    if not kit or kit == {"colours": {}}:
        raise ToolError("The brand kit is empty.", "add a logo, a handle, a font, colours or a card")
    d = brand_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (KIT_NAME + ".check.json")
    tmp.write_text(json.dumps(kit, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        load_brand_kit(tmp)
    except ToolError:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, p)
    return {"message": "Brand kit saved.", **read(ws)}
