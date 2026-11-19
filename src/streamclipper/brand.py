"""
Brand kit (opt-in, --brand-kit FILE): a JSON file with a logo/watermark, a handle like @name, a caption font,
colours and optional intro/outro cards. Paths inside the JSON are relative to its folder. The MP4 gets all of
it; CapCut drafts get the handle, the font and the colours (logo and cards are MP4 only).

{
  "logo": {"image": "logo.png", "position": "auto", "width_pct": 12, "opacity": 0.85},
  "handle": "@myname",
  "font": "MyFont.ttf",
  "colours": {"highlight": "#FFE600", "hook_box": "#FFE600"},
  "intro": {"image": "intro.png", "text": "NEW CLIP", "seconds": 1.0},
  "outro": {"text": "Follow @myname", "seconds": 1.5}
}
"""

import hashlib
import json
import re
import struct
from pathlib import Path

from streamclipper.captions.styles import HIGHLIGHT_COLOURS, NEON_PALETTE
from streamclipper.util import ToolError

KEYS = ("logo", "handle", "font", "colours", "colors", "intro", "outro", "name")
POSITIONS = ("auto", "top-left", "top-right", "bottom-left", "bottom-right")
CORNER_ORDER = ("top-left", "top-right", "bottom-left", "bottom-right")
IMAGE_EXTS = (".png", ".jpg", ".jpeg")
FONT_EXTS = (".ttf", ".otf")
MAX_IMAGE_PX = 4096
MAX_CARD_S = 1.5
MAX_HANDLE = 30
MAX_CARD_TEXT = 60
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
MARGIN_PX = 40
SHRINK = 0.7  # a logo with no free corner is tried once at 70% size, then skipped
HINT = "see docs/examples/brand_kit.json"


def image_size(path: Path) -> tuple:
    """(width, height) of a PNG or JPEG from its header, or None."""
    with open(path, "rb") as f:
        head = f.read(32)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return struct.unpack(">II", head[16:24])
        if head[:2] != b"\xff\xd8":
            return None
        f.seek(2)
        while True:
            b = f.read(1)
            while b and b != b"\xff":
                b = f.read(1)
            while b == b"\xff":
                b = f.read(1)
            if not b:
                return None
            marker = b[0]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                continue
            seg = f.read(2)
            if len(seg) < 2:
                return None
            n = struct.unpack(">H", seg)[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                data = f.read(5)
                if len(data) < 5:
                    return None
                h, w = struct.unpack(">HH", data[1:5])
                return w, h
            f.seek(n - 2, 1)


def _file(base: Path, value, what: str, exts: tuple) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"Brand kit: {what} must be a file name.", HINT)
    p = Path(value.strip())
    p = (p if p.is_absolute() else base / p).resolve()
    if p.suffix.lower() not in exts:
        raise ToolError(f"Brand kit: {what} must be a {'/'.join(e[1:].upper() for e in exts)} file.", HINT)
    if not p.is_file():
        raise ToolError(f"Brand kit: {what} not found: {p}", "put the file next to the brand kit JSON")
    return p


def _image(base: Path, value, what: str) -> Path:
    p = _file(base, value, what, IMAGE_EXTS)
    size = image_size(p)
    if not size:
        raise ToolError(f"Brand kit: {what} is not a readable PNG or JPEG.", "save it again as PNG")
    if max(size) > MAX_IMAGE_PX or min(size) < 8:
        raise ToolError(f"Brand kit: {what} is {size[0]}x{size[1]} px.", f"use 8-{MAX_IMAGE_PX} px")
    return p


def _num(v, lo: float, hi: float, default: float, what: str) -> float:
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ToolError(f"Brand kit: {what} must be a number.", HINT) from None
    if not lo <= f <= hi:
        raise ToolError(f"Brand kit: {what} must be between {lo:g} and {hi:g}.", HINT)
    return f


def _text(v, what: str, limit: int) -> str:
    s = "" if v is None else str(v)
    if "\n" in s or "\r" in s or len(s) > limit:
        raise ToolError(f"Brand kit: {what} must be one line of at most {limit} characters.", HINT)
    return s.strip()


def _card(base: Path, raw, what: str):
    if raw in (None, False, {}):
        return None
    if not isinstance(raw, dict):
        raise ToolError(f"Brand kit: {what} must be an object.", HINT)
    secs = raw.get("seconds", 1.0)
    try:
        secs = float(secs)
    except (TypeError, ValueError):
        raise ToolError(f"Brand kit: {what}.seconds must be a number.", HINT) from None
    if secs <= 0:
        return None
    if secs > MAX_CARD_S:
        print(f"Brand kit: {what} shortened to {MAX_CARD_S:g} s (longer cards delay the hook).")
        secs = MAX_CARD_S
    card = {
        "image": str(_image(base, raw["image"], f"{what}.image")) if raw.get("image") else "",
        "text": _text(raw.get("text"), f"{what}.text", MAX_CARD_TEXT),
        "seconds": round(secs, 3),
    }
    if not card["image"] and not card["text"]:
        raise ToolError(f"Brand kit: {what} needs an image or a text.", HINT)
    return card


def load_brand_kit(path) -> dict:
    """The checked brand kit with absolute paths, plus path and sha1 (of the JSON) for the clip plan."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise ToolError(f"Brand kit not found: {p}", "check the --brand-kit path")
    raw_bytes = p.read_bytes()
    try:
        raw = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ToolError(f"Brand kit {p.name} is not valid JSON ({e}).", HINT) from None
    if not isinstance(raw, dict):
        raise ToolError("Brand kit: the file must hold one JSON object.", HINT)
    unknown = sorted(set(raw) - set(KEYS))
    if unknown:
        raise ToolError(f"Brand kit: unknown key(s) {', '.join(unknown)}.", "use " + ", ".join(KEYS[:7]))
    base = p.parent
    kit = {"path": str(p), "sha1": hashlib.sha1(raw_bytes).hexdigest(), "logo": None}
    logo = raw.get("logo")
    if logo:
        if isinstance(logo, str):
            logo = {"image": logo}
        if not isinstance(logo, dict) or not logo.get("image"):
            raise ToolError("Brand kit: logo needs an image.", HINT)
        pos = logo.get("position") or "auto"
        if pos not in POSITIONS:
            raise ToolError(f"Brand kit: logo position '{pos}'.", "use " + ", ".join(POSITIONS))
        kit["logo"] = {
            "image": str(_image(base, logo["image"], "logo.image")),
            "position": pos,
            "width_pct": _num(logo.get("width_pct"), 5, 25, 12, "logo.width_pct"),
            "opacity": _num(logo.get("opacity"), 0.2, 1.0, 0.85, "logo.opacity"),
        }
    kit["handle"] = _text(raw.get("handle"), "handle", MAX_HANDLE)
    kit["font"] = str(_file(base, raw["font"], "font", FONT_EXTS)) if raw.get("font") else ""
    cols = raw.get("colours", raw.get("colors")) or {}
    if not isinstance(cols, dict):
        raise ToolError("Brand kit: colours must be an object.", HINT)
    kit["colours"] = {}
    for k in ("highlight", "hook_box"):
        v = cols.get(k)
        if v in (None, ""):
            continue
        if not isinstance(v, str) or not HEX_RE.match(v):
            raise ToolError(f"Brand kit: colours.{k} must look like #FFE600.", HINT)
        kit["colours"][k] = v.upper()
    kit["intro"] = _card(base, raw.get("intro"), "intro")
    kit["outro"] = _card(base, raw.get("outro"), "outro")
    return kit


def nearest_colour(hex_col: str, names=HIGHLIGHT_COLOURS) -> str:
    """The caption palette name closest to #RRGGBB (CapCut and the word captions use named colours)."""
    r, g, b = (int(hex_col[i : i + 2], 16) for i in (1, 3, 5))

    def dist(name):
        hx = NEON_PALETTE[name][1]
        r2, g2, b2 = (int(hx[i : i + 2], 16) for i in (1, 3, 5))
        return (r - r2) ** 2 + (g - g2) ** 2 + (b - b2) ** 2

    return min((n for n in names if n in NEON_PALETTE), key=dist)


def ass_colour(hex_col: str, alpha: str = "00") -> str:
    """#RRGGBB -> ASS &HAABBGGRR."""
    return f"&H{alpha}{hex_col[5:7]}{hex_col[3:5]}{hex_col[1:3]}".upper()


def card_seconds(kit) -> tuple:
    kit = kit or {}
    return (
        float((kit.get("intro") or {}).get("seconds") or 0.0),
        float((kit.get("outro") or {}).get("seconds") or 0.0),
    )


def ui_zones(W: int, H: int) -> list:
    """Boxes [x0, y0, x1, y1] the platform UI covers on 9:16 (Reels/Shorts/TikTok): the action buttons in
    the right 15% below 40% of the height, and the caption/sound bar in the bottom 20%. None on 1:1."""
    if H <= W:
        return []
    return [[int(W * 0.85), int(H * 0.40), W, H], [0, int(H * 0.80), W, H]]


def _hits(box, boxes) -> bool:
    x0, y0, x1, y1 = box
    return any(x0 < b[2] and b[0] < x1 and y0 < b[3] and b[1] < y1 for b in boxes)


def corner_box(corner: str, w: int, h: int, W: int, H: int) -> list:
    top = int(H * 0.06) if H > W else MARGIN_PX  # below the status bar and the app's top icons
    x = MARGIN_PX if corner.endswith("left") else W - MARGIN_PX - w
    y = top if corner.startswith("top") else H - MARGIN_PX - h
    return [x, y, x + w, y + h]


def place_box(position: str, w: int, h: int, W: int, H: int, avoid: list, shrink: bool = True):
    """
    (corner, [x0, y0, x1, y1]) of the first corner (or the fixed one) where a w x h box hits none of the
    avoid boxes (captions, faces) or the platform UI zones; tried again at 70% size; None when nothing fits.
    """
    corners = CORNER_ORDER if position in ("", "auto") else (position,)
    blocked = list(avoid or []) + ui_zones(W, H)
    for k in (1.0, SHRINK) if shrink else (1.0,):
        ww, hh = max(2, int(w * k)) & ~1, max(2, int(h * k)) & ~1
        for c in corners:
            box = corner_box(c, ww, hh, W, H)
            if not _hits(box, blocked):
                return c, box
    return None


def logo_size(kit_logo: dict, W: int) -> tuple:
    iw, ih = image_size(Path(kit_logo["image"]))
    w = max(2, int(W * kit_logo["width_pct"] / 100.0)) & ~1
    return w, max(2, int(round(w * ih / max(1, iw))) & ~1)


HANDLE_SIZE = 40


def handle_box_size(handle: str, size: int = HANDLE_SIZE) -> tuple:
    return int(len(handle) * size * 0.58 + 24), int(size * 1.35)


def plan_brand(kit: dict, W: int, H: int, avoid: list) -> dict:
    """
    Where the logo and the handle go on a W x H canvas: {logo: {box, corner} | None, handle: {box, corner,
    align} | None, notes: [...]}. The handle sits under (top corners) or over (bottom corners) the logo.
    """
    out = {"logo": None, "handle": None, "notes": []}
    lg = kit.get("logo")
    hw, hh = handle_box_size(kit.get("handle") or "")
    if lg:
        w, h = logo_size(lg, W)
        extra = hh + 8 if kit.get("handle") else 0
        # the logo and the handle under/over it are placed as one block
        got = place_box(lg["position"], max(w, hw if extra else 0), h + extra, W, H, avoid)
        if got is None:
            out["notes"].append("logo skipped: no free corner (captions, faces or the app's buttons are there)")
        else:
            corner, block = got
            k = SHRINK if block[3] - block[1] < h + extra - 1 else 1.0
            lw, lh = max(2, int(w * k)) & ~1, max(2, int(h * k)) & ~1
            left, top = corner.endswith("left"), corner.startswith("top")
            x = block[0] if left else block[2] - lw
            y = block[1] if top else block[3] - lh
            out["logo"] = {"corner": corner, "box": [x, y, x + lw, y + lh]}
            if k < 1.0:
                out["notes"].append("logo 30% smaller: the full size did not fit in a free corner")
            if extra:
                hy = y + lh + 8 if top else y - 8 - hh
                hx = x if left else x + lw - hw
                hbox = [hx, hy, hx + hw, hy + hh]
                inside = hbox[0] >= 0 and hbox[1] >= 0 and hbox[2] <= W and hbox[3] <= H
                if inside and not _hits(hbox, list(avoid or []) + ui_zones(W, H)):
                    out["handle"] = {"corner": corner, "box": hbox}
    if kit.get("handle") and out["handle"] is None:
        taken = [out["logo"]["box"]] if out["logo"] else []
        got = place_box("auto", hw, hh, W, H, list(avoid or []) + taken, shrink=False)
        if got is None:
            out["notes"].append("handle skipped: no free corner")
        else:
            out["handle"] = {"corner": got[0], "box": got[1]}
    return out
