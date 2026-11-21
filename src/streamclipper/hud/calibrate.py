"""calibrate-hud: find where a game shows text and which words it uses, and write a profile skeleton.

Samples frames (--times or every N s), reads a 4x4 grid of cells with the OCR model, writes
calibrate_sheet.jpg (the frames with the cells where text was read outlined) and a profile JSON whose
events carry those regions and words. --check runs an edited profile over the video and prints its detections.
"""

from pathlib import Path

from streamclipper.audio import _mmss, _np
from streamclipper.hud import frames as F
from streamclipper.hud.detect import crop, text_line_box
from streamclipper.hud.ocr import match_words
from streamclipper.hud.profiles import validate
from streamclipper.state import write_json_atomic
from streamclipper.util import fwd, run_cmd

GRID = 4
MAX_FRAMES = 36
MIN_CONF = 0.6
COLOURS = ("any", "red", "gold", "white")
MIN_LETTERS = 3
SHEET_W = 320


def sample_times(dur: float, times: list = None, every: float = 30.0) -> list:
    """The given times (inside the video), else every `every` s from every/2; at most MAX_FRAMES."""
    if times:
        out = [t for t in times if 0 <= t < dur]
    else:
        every = max(1.0, float(every))
        out, t = [], every / 2
        while t < dur:
            out.append(round(t, 2))
            t += every
    if len(out) > MAX_FRAMES:
        step = len(out) / MAX_FRAMES
        out = [out[int(i * step)] for i in range(MAX_FRAMES)]
    return out


def grid_cells(n: int = GRID) -> list:
    return [
        [round(c / n, 4), round(r / n, 4), round((c + 1) / n, 4), round((r + 1) / n, 4)]
        for r in range(n)
        for c in range(n)
    ]


def scan_regions(n: int = GRID) -> list:
    """Where text is looked for: n full-width row bands, the n - 1 bands between them (so a line on a band edge
    is read whole) and the n x n grid cells (small corner text)."""
    rows = [[0.0, r / n, 1.0, (r + 1) / n] for r in range(n)]
    mid = [[0.0, (r + 0.5) / n, 1.0, (r + 1.5) / n] for r in range(n - 1)]
    return rows + mid + grid_cells(n)


def read_cells(frame, ocr) -> list:
    """[(box, WORD, conf)]: words of >= 3 letters read with conf >= MIN_CONF; box = the text line found in a scan
    region, as frame fractions. A word read in several overlapping regions is kept once (best confidence)."""
    h, w = frame.shape[:2]
    best = {}
    for reg in scan_regions():
        sub = crop(frame, reg)
        for colour in COLOURS:  # game text is often coloured (red WASTED, gold MISSION PASSED) on a busy picture
            line = text_line_box(sub, colour)
            if line is None:
                continue
            y0, y1, x0, x1 = line
            pad = max(2, int((y1 - y0) * 0.35))
            piece = sub[max(0, y0 - pad) : y1 + pad, max(0, x0 - 2 * pad) : x1 + 2 * pad]
            text, conf = ocr.read(piece)
            if conf < MIN_CONF:
                continue
            ox, oy = int(reg[0] * w), int(reg[1] * h)
            box = [round((ox + x0) / w, 4), round((oy + y0) / h, 4), round((ox + x1) / w, 4), round((oy + y1) / h, 4)]
            for tok in "".join(ch if ch.isalpha() else " " for ch in text.upper()).split():
                if len(tok) >= MIN_LETTERS and (tok not in best or conf > best[tok][2]):
                    best[tok] = (box, tok, round(conf, 3), colour)
    return list(best.values())


def _union(boxes: list, grow: float = 0.3) -> list:
    """Bounding box of the boxes, grown by `grow` of its size on every side (text moves a little), clipped."""
    x0, y0 = min(c[0] for c in boxes), min(c[1] for c in boxes)
    x1, y1 = max(c[2] for c in boxes), max(c[3] for c in boxes)
    gx, gy = (x1 - x0) * grow, (y1 - y0) * grow
    return [
        round(max(0.0, x0 - gx), 4),
        round(max(0.0, y0 - gy), 4),
        round(min(1.0, x1 + gx), 4),
        round(min(1.0, y1 + gy), 4),
    ]


def build_profile(base: dict, reads: list, name: str) -> dict:
    """Profile skeleton from [(t, box, WORD, conf[, colour])]: base events whose words were read get the area
    where they were read as region; words read on >= 2 frames that no base event has become new 'ocr' events."""
    events = []
    used = set()
    for ev in base["events"]:
        ev = dict(ev)
        hits = [r for r in reads if ev["words"] and match_words(r[2], ev["words"], 0.25)]
        if hits:
            ev["region"] = _union([r[1] for r in hits])
            used.update(r[2] for r in hits)
        events.append(ev)
    words = {}
    for r in reads:
        t, box, word = r[:3]
        if word not in used:
            info = words.setdefault(word, {"times": set(), "cells": [], "colours": []})
            info["times"].add(t)
            info["cells"].append(box)
            info["colours"].append(r[4] if len(r) > 4 else "any")
    taken = {e["id"] for e in events}
    for word, info in sorted(words.items(), key=lambda kv: -len(kv[1]["times"]))[:10]:
        wid = word.lower()
        if len(info["times"]) < 2 or wid in taken:
            continue
        taken.add(wid)
        events.append(
            {
                "id": wid,
                "label": word,
                "reason": f"{word} on screen",
                "strength": 0.5,
                "method": "ocr",
                "region": _union(info["cells"]),
                "words": [word],
                "colour": max(set(info["colours"]), key=info["colours"].count),
                "min_s": 0,
                "cooldown_s": 20,
                "params": {"min_conf": MIN_CONF},
            }
        )
    doc = {
        "name": name,
        "label": f"{base['label']} (calibrated)",
        "experimental": True,
        "title_keywords": base["title_keywords"],
        "notes": "Written by calibrate-hud: check the regions, words and strengths, then run calibrate-hud --check.",
        "events": events,
    }
    validate(doc, name)
    return doc


def _draw_box(img, box, colour=(0, 255, 0)):
    h, w = img.shape[:2]
    x0, y0 = int(box[0] * w), int(box[1] * h)
    x1, y1 = min(w - 1, int(box[2] * w) - 1), min(h - 1, int(box[3] * h) - 1)
    img[y0 : y0 + 2, x0 : x1 + 1] = colour
    img[max(y0, y1 - 1) : y1 + 1, x0 : x1 + 1] = colour
    img[y0 : y1 + 1, x0 : x0 + 2] = colour
    img[y0 : y1 + 1, max(x0, x1 - 1) : x1 + 1] = colour


def write_sheet(frames: list, out: Path) -> Path:
    """[(frame, [boxes])] -> a 4-column JPEG sheet (SHEET_W px per frame) with the read text outlined."""
    np = _np()
    if not frames:
        return None
    tiles = []
    for fr, cells in frames[:16]:
        h, w = fr.shape[:2]
        th = max(2, int(round(SHEET_W * h / w / 2)) * 2)
        ys = (np.arange(th) * h // th).astype(int)
        xs = (np.arange(SHEET_W) * w // SHEET_W).astype(int)
        tile = fr[ys][:, xs].copy()
        for c in cells:
            _draw_box(tile, c)
        tiles.append(tile)
    th = tiles[0].shape[0]
    cols = 4
    rows = (len(tiles) + cols - 1) // cols
    sheet = np.zeros((rows * th, cols * SHEET_W, 3), np.uint8)
    for i, tile in enumerate(tiles):
        r, c = divmod(i, cols)
        sheet[r * th : r * th + tile.shape[0], c * SHEET_W : (c + 1) * SHEET_W] = tile[:th]
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".rgb")
    tmp.write_bytes(sheet.tobytes())
    try:
        run_cmd(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24"]
            + ["-s", f"{sheet.shape[1]}x{sheet.shape[0]}", "-i", str(tmp), "-frames:v", "1", "-q:v", "3", str(out)],
            "calibrate sheet",
        )
    finally:
        tmp.unlink(missing_ok=True)
    return out


def calibrate(video: Path, base: dict, ocr, out_json: Path, times: list = None, every: float = 30.0) -> dict:
    """Samples frames, reads the grid, writes the profile JSON and calibrate_sheet.jpg next to it."""
    from streamclipper.layout import probe_video

    dur = probe_video(video)["dur"]
    reads, sheet = [], []
    for t in sample_times(dur, times, every):
        fr = F.frame_at(video, t)
        if fr is None:
            continue
        found = read_cells(fr, ocr)
        reads += [(t, box, word, conf, colour) for box, word, conf, colour in found]
        sheet.append((fr, [f[0] for f in found]))
        if found:
            print(f"  {_mmss(t)}  " + ", ".join(sorted({f[1] for f in found})))
    out_json = Path(out_json)
    doc = build_profile(base, reads, out_json.stem)
    write_json_atomic(out_json, doc)
    sheet_p = write_sheet(sheet, out_json.with_name("calibrate_sheet.jpg"))
    print(f"Read {len(reads)} words on {len(sheet)} frames. Profile: {fwd(out_json)}")
    if sheet_p:
        print(f"Sheet: {fwd(sheet_p)} (green boxes = text that was read)")
    return doc


def check(video: Path, profile: dict, ocr) -> list:
    """Runs a profile over the video (keyframe pass + refinement, like discover) and prints the detections."""
    from streamclipper.hud.analyze import scan

    res = scan(video, [profile], ocr)
    print(f"{'TIME':<8}{'EVENT':<18}{'STRENGTH':<10}{'CONF':<6}TEXT")
    for e in res["events"]:
        print(f"{_mmss(e['t']):<8}{e['event']:<18}{e['strength']:<10}{e['conf']:<6}{e['text']}")
    print(f"{len(res['events'])} events from {res['keyframes']} keyframes")
    return res["events"]
