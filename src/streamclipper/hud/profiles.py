"""Game profiles: which on-screen events a game has, where they appear and how they are recognised.

A profile is JSON: {name, label, experimental, title_keywords[], events: [{id, label, reason, strength,
method, region, words[], colour, min_s, cooldown_s, params{}}]}. region = [x0, y0, x1, y1] as fractions of
the frame. Shipped profiles live in data/hud/; --hud-profile FILE loads a user profile (e.g. from calibrate-hud).
"""

import json
from pathlib import Path

from streamclipper.paths import DATA_DIR
from streamclipper.util import ToolError

PROFILE_DIR = DATA_DIR / "hud"
METHODS = ("ocr", "desat_ocr", "stars", "flash", "text_burst")
COLOURS = ("any", "red", "gold", "white")
_EVENT_KEYS = {
    "id",
    "label",
    "reason",
    "strength",
    "method",
    "region",
    "words",
    "colour",
    "min_s",
    "cooldown_s",
    "params",
    "notes",
}
_TOP_KEYS = {"name", "label", "experimental", "title_keywords", "events", "notes"}
_HINT = "see the shipped profiles in streamclipper/data/hud/ (gta5.json) for the format, or run calibrate-hud"


def list_profiles() -> list:
    """Names of the shipped profiles (sorted)."""
    return sorted(p.stem for p in PROFILE_DIR.glob("*.json"))


def _bad(where: str, msg: str):
    raise ToolError(f"HUD profile {where}: {msg}", _HINT)


def _num(ev: dict, key: str, where: str, lo: float, hi: float, default=None) -> float:
    v = ev.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _bad(where, f"'{key}' must be a number (got {v!r})")
    if not lo <= float(v) <= hi:
        _bad(where, f"'{key}' = {v} is outside {lo:g}-{hi:g}")
    return float(v)


def validate(doc, where: str = "profile") -> dict:
    """Checks a profile dict and returns a normalised copy (defaults filled in). Raises ToolError with a hint."""
    if not isinstance(doc, dict):
        _bad(where, "the file must hold a JSON object")
    extra = set(doc) - _TOP_KEYS
    if extra:
        _bad(where, f"unknown key(s) {', '.join(sorted(extra))}")
    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        _bad(where, "'name' must be a non-empty string")
    kws = doc.get("title_keywords", [])
    if not isinstance(kws, list) or not all(isinstance(k, str) for k in kws):
        _bad(where, "'title_keywords' must be a list of strings")
    events = doc.get("events")
    if not isinstance(events, list) or not events:
        _bad(where, "'events' must be a non-empty list")
    out_events, seen = [], set()
    for i, ev in enumerate(events):
        w = f"{where} event {i + 1}"
        if not isinstance(ev, dict):
            _bad(w, "each event must be a JSON object")
        extra = set(ev) - _EVENT_KEYS
        if extra:
            _bad(w, f"unknown key(s) {', '.join(sorted(extra))}")
        eid = ev.get("id")
        if not isinstance(eid, str) or not eid.strip():
            _bad(w, "'id' must be a non-empty string")
        if eid in seen:
            _bad(w, f"id '{eid}' is used twice")
        seen.add(eid)
        w = f"{where} event '{eid}'"
        method = ev.get("method")
        if method not in METHODS:
            _bad(w, f"'method' must be one of {', '.join(METHODS)} (got {method!r})")
        region = ev.get("region", [0.0, 0.0, 1.0, 1.0])
        if (
            not isinstance(region, list)
            or len(region) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in region)
        ):
            _bad(w, "'region' must be [x0, y0, x1, y1] (fractions of the frame)")
        x0, y0, x1, y1 = (float(v) for v in region)
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            _bad(w, f"'region' {region} must satisfy 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1")
        words = ev.get("words", [])
        if not isinstance(words, list) or not all(isinstance(x, str) and x.strip() for x in words):
            _bad(w, "'words' must be a list of non-empty strings")
        params = ev.get("params", {})
        if not isinstance(params, dict) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in params.values()
        ):
            _bad(w, "'params' must be an object of numbers")
        if method in ("ocr", "desat_ocr") and not words and not params.get("any_word"):
            _bad(w, f"method '{method}' needs 'words' (or params.any_word = 1)")
        colour = ev.get("colour", "any")
        if colour not in COLOURS:
            _bad(w, f"'colour' must be one of {', '.join(COLOURS)} (got {colour!r})")
        label = ev.get("label") or eid.replace("_", " ")
        out_events.append(
            {
                "id": eid,
                "label": str(label),
                "reason": str(ev.get("reason") or f"{label} on screen"),
                "strength": _num(ev, "strength", w, 0.0, 1.0),
                "method": method,
                "region": [round(v, 4) for v in (x0, y0, x1, y1)],
                "words": [x.strip().upper() for x in words],
                "colour": colour,
                "min_s": _num(ev, "min_s", w, 0.0, 30.0, 0.0),
                "cooldown_s": _num(ev, "cooldown_s", w, 0.0, 600.0, 20.0),
                "params": {k: float(v) for k, v in params.items()},
            }
        )
    return {
        "name": name.strip(),
        "label": str(doc.get("label") or name),
        "experimental": bool(doc.get("experimental", False)),
        "title_keywords": [k.lower() for k in kws],
        "events": out_events,
    }


def load_profile(name_or_path) -> dict:
    """A shipped profile by name (gta5, gta6, generic) or a JSON file path; validated (ToolError on errors)."""
    s = str(name_or_path)
    p = Path(s)
    if s.lower().endswith(".json") or p.is_file():
        if not p.is_file():
            raise ToolError(f"HUD profile file not found: {s}", "check the path given to --hud-profile")
        where = p.name
    else:
        p = PROFILE_DIR / f"{s}.json"
        if not p.is_file():
            raise ToolError(
                f"Unknown HUD profile '{s}'.", f"use one of: {', '.join(list_profiles())}, or --hud-profile FILE"
            )
        where = s
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ToolError(f"HUD profile {where} is not valid JSON ({e}).", _HINT) from None
    return validate(doc, where)


def match_title(title: str, profiles: list):
    """The first profile whose title keyword appears in the title / file name (case-insensitive), else None."""
    t = " " + " ".join((title or "").lower().replace("_", " ").replace("-", " ").split()) + " "
    for prof in profiles:
        for kw in prof["title_keywords"]:
            if f" {kw} " in t or (len(kw) > 4 and kw in t):
                return prof
    return None
