"""Colour/shape cues on low-res RGB frames and event onsets (numpy only; OCR is optional, see ocr.py).

Frames are RGB uint8 arrays (h, w, 3). Regions are [x0, y0, x1, y1] fractions of the frame.
detect_events(profile, frames, ocr) -> [{t, event, label, reason, strength, method, conf, text}].
"""

from streamclipper.audio import _np
from streamclipper.hud.ocr import match_words

DARK_V = 30  # pixels darker than this (0-255) have no meaningful hue and are left out of the saturation
STAR_LUMA = 200  # wanted stars are bright white
# area / convex-hull area of a star blob: a drawn five-pointed star is ~0.5; at 360p a GTA V star is an 8-12 px
# blob measured at 0.58-0.80 (FEAT-002 calibration), so the upper end is 0.85, not 0.75
STAR_SOLIDITY = (0.45, 0.85)
STAR_AREA = (0.00002, 0.0012)  # star blob area as a share of the frame area (640x360: ~5-276 px)
TEXT_ROW_EDGE = 40.0  # mean horizontal luma step (0-255) that makes a pixel row part of a text line


def crop(frame, region):
    """The region [x0, y0, x1, y1] (fractions) of a frame; at least 1x1 pixel."""
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = region
    a, b = min(w - 1, int(x0 * w)), min(h - 1, int(y0 * h))
    return frame[b : max(b + 1, int(round(y1 * h))), a : max(a + 1, int(round(x1 * w)))]


def luma(rgb):
    np = _np()
    f = rgb.astype(np.float32)
    return f[..., 0] * 0.299 + f[..., 1] * 0.587 + f[..., 2] * 0.114


def desaturation(frame) -> float:
    """1 - median HSV saturation of the non-dark pixels: ~0.95+ on the grey WASTED/BUSTED screen, ~0.6-0.8 in play."""
    np = _np()
    f = frame.astype(np.int16)
    mx, mn = f.max(axis=2), f.min(axis=2)
    keep = mx >= DARK_V
    if keep.sum() < 0.05 * mx.size:
        return 0.0  # a black frame is not a desaturated game screen
    sat = (mx[keep] - mn[keep]) / mx[keep].astype(np.float32)
    return round(float(1.0 - np.median(sat)), 4)


def colour_mask(rgb, colour: str):
    """Boolean mask of pixels of a HUD text colour: red (WASTED, MISSION FAILED), gold (MISSION PASSED), white."""
    np = _np()
    f = rgb.astype(np.int16)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    if colour == "red":
        return (r >= 90) & (r >= g * 1.7) & (r >= b * 1.7)
    if colour == "gold":
        return (r >= 150) & (g >= 105) & (r >= g) & (b <= g * 0.6)
    if colour == "white":
        return (r >= 200) & (g >= 200) & (b >= 200) & ((f.max(axis=2) - f.min(axis=2)) <= 40)
    return np.zeros(rgb.shape[:2], bool) | (luma(rgb) >= 200)


def colour_text_band(frame, region, colour: str) -> float:
    """Share of the region's pixels in the text colour (0-1)."""
    m = colour_mask(crop(frame, region), colour)
    return round(float(m.mean()), 5) if m.size else 0.0


def _blobs(mask) -> list:
    """4-connected components of a small boolean mask -> [[(y, x), ...], ...]."""
    np = _np()
    ys, xs = np.nonzero(mask)
    todo = set(zip(ys.tolist(), xs.tolist(), strict=True))
    out = []
    while todo:
        seed = todo.pop()
        stack, blob = [seed], [seed]
        while stack:
            y, x = stack.pop()
            for p in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if p in todo:
                    todo.remove(p)
                    stack.append(p)
                    blob.append(p)
        out.append(blob)
    return out


def _hull_area(points: list) -> float:
    """Convex hull area (monotone chain) of pixel squares given as (y, x) pixel indices."""
    pts = sorted({(x + dx, y + dy) for y, x in points for dx in (0, 1) for dy in (0, 1)})
    if len(pts) < 3:
        return float(len(points))

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    return abs(sum(hull[i][0] * hull[i - 1][1] - hull[i - 1][0] * hull[i][1] for i in range(len(hull)))) / 2.0


def star_blobs(frame, region) -> list:
    """Star-like bright blobs in the region: [(cx, cy, size)] in region pixels."""
    np = _np()
    sub = crop(frame, region)
    f = sub.astype(np.int16)
    bright = (luma(sub) >= STAR_LUMA) & ((f.max(axis=2) - f.min(axis=2)) <= 60)
    if bright.mean() > 0.6:  # a white wall or a flash: no HUD to read (stars have a dark outline, so sky is fine)
        return []
    fa = frame.shape[0] * frame.shape[1]
    out = []
    for blob in _blobs(bright):
        n = len(blob)
        if not STAR_AREA[0] * fa <= n <= STAR_AREA[1] * fa:
            continue
        ys = [p[0] for p in blob]
        xs = [p[1] for p in blob]
        bw, bh = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
        if bh > 1.25 * bw or bw > 1.6 * bh:  # stars are about as wide as tall; digits are taller
            continue
        sol = n / max(1.0, _hull_area(blob))
        if STAR_SOLIDITY[0] <= sol <= STAR_SOLIDITY[1]:
            out.append(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, max(bw, bh)))
    return out


def count_stars(frame, region) -> int:
    """Wanted stars: the longest row of similar-sized star blobs on one line, evenly spaced (0-5)."""
    blobs = sorted(star_blobs(frame, region))
    best = 0
    for i, (cx, cy, size) in enumerate(blobs):
        chain, last, gap = 1, (cx, cy, size), None
        for b in blobs[i + 1 :]:
            d = b[0] - last[0]
            if abs(b[1] - cy) > max(1.5, 0.4 * size) or not 0.6 <= b[2] / size <= 1.6:
                continue
            if not 1.5 * size <= d <= 2.4 * size:  # GTA V stars sit ~1.9 star widths apart
                continue
            if gap is not None and abs(d - gap) > max(1.5, 0.35 * gap):
                break
            gap = d if gap is None else gap
            chain, last = chain + 1, b
        best = max(best, chain)
    return min(best, 5)


def flash(prev, cur) -> float:
    """Brightness jump between two frames (0-1 of the luma range); explosions and muzzle flashes."""
    if prev is None or prev.shape != cur.shape:
        return 0.0
    return round(max(0.0, float(luma(cur).mean() - luma(prev).mean()) / 255.0), 4)


def text_rows(frame, region) -> list:
    """Text lines in the region as (y0, y1) row ranges: rows with many strong horizontal luma steps."""
    np = _np()
    g = luma(crop(frame, region))
    if g.shape[1] < 3:
        return []
    edge = np.abs(np.diff(g, axis=1))
    on = (edge >= TEXT_ROW_EDGE).mean(axis=1) >= 0.08
    rows, start = [], None
    for i, v in enumerate(on.tolist() + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= 2:
                rows.append((start, i))
            start = None
    return rows


def new_rows(prev_rows: list, rows: list) -> int:
    """Rows in this frame that overlap no row of the previous frame (new kill-feed lines)."""
    return sum(1 for a, b in rows if not any(a < d and c < b for c, d in prev_rows))


def text_burst(frames: list, region, window_s: float = 4.0) -> list:
    """[(t, new rows in the last window_s seconds)] over a list of (t, frame)."""
    out, prev, hist = [], [], []
    for t, fr in frames:
        rows = text_rows(fr, region)
        hist.append((t, new_rows(prev, rows) if prev or hist else 0))
        prev = rows
        hist = [(a, n) for a, n in hist if a > t - window_s]
        out.append((t, sum(n for _, n in hist)))
    return out


def text_line_box(sub, colour: str):
    """(y0, y1, x0, x1) of the main text line in a region crop, or None: rows/columns with text-colour pixels
    (colour != any) or strong horizontal luma steps (any)."""
    np = _np()
    if colour != "any":
        m = colour_mask(sub, colour)
    else:
        g = luma(sub)
        m = np.zeros(g.shape, bool)
        m[:, 1:] = np.abs(np.diff(g, axis=1)) >= TEXT_ROW_EDGE
    rows = m.mean(axis=1)
    if rows.max() <= 0.01:
        return None
    on = rows >= max(0.01, 0.25 * rows.max())
    peak = int(rows.argmax())
    y0 = y1 = peak
    while y0 > 0 and (on[y0 - 1] or (y0 > 1 and on[y0 - 2])):
        y0 -= 1
    while y1 < len(on) - 1 and (on[y1 + 1] or (y1 < len(on) - 2 and on[y1 + 2])):
        y1 += 1
    cols = np.nonzero(m[y0 : y1 + 1].any(axis=0))[0]
    if len(cols) < 3:
        return None
    return y0, y1 + 1, int(cols[0]), int(cols[-1]) + 1


def text_crop(frame, region, colour: str, pad: float = 0.35):
    """(crop, height share of the frame) of the main text line in the region, padded for OCR; (None, 0)."""
    sub = crop(frame, region)
    box = text_line_box(sub, colour)
    if box is None:
        return None, 0.0
    y0, y1, x0, x1 = box
    hh = y1 - y0
    py = max(2, int(hh * pad))
    px = max(4, int(hh * pad * 1.5))
    h, w = sub.shape[:2]
    out = sub[max(0, y0 - py) : min(h, y1 + py), max(0, x0 - px) : min(w, x1 + px)]
    return out, hh / frame.shape[0]


# ---------------------------------------------------------------- event onsets


def _cue(ev: dict, t: float, fr, prev, ocr, cache: dict, burst: dict) -> tuple:
    """(on, strength, conf, text) of one event spec on one frame."""
    m, p = ev["method"], ev["params"]
    if m == "flash":
        j = flash(prev, fr)
        return j >= p.get("min_jump", 0.25), ev["strength"], round(min(1.0, j / 0.5), 3), ""
    if m == "stars":
        n = count_stars(fr, ev["region"])
        if n < p.get("min_count", 3):
            return False, 0.0, 0.0, ""
        strong = n >= p.get("strong_count", 5)
        return True, (p.get("strong_strength", 0.9) if strong else ev["strength"]), 1.0, f"{n} stars"
    if m == "text_burst":
        n = burst.get(ev["id"], 0)
        return n >= p.get("min_rows", 3), ev["strength"], round(min(1.0, n / 6.0), 3), f"{n} new lines"
    # ocr / desat_ocr: cheap colour gates first, OCR only when they pass
    if m == "desat_ocr":
        if cache.setdefault("desat", desaturation(fr)) < p.get("min_desat", 0.85):
            return False, 0.0, 0.0, ""
    share = colour_text_band(fr, ev["region"], ev["colour"]) if ev["colour"] != "any" else 1.0
    if share < p.get("colour_min", 0.002):
        return False, 0.0, 0.0, ""
    if ocr is None:  # cue-only mode (no OCR model): colour gates only, weaker
        if not p.get("cue_only", 1 if m == "desat_ocr" else 0):
            return False, 0.0, 0.0, ""
        return True, round(ev["strength"] * 0.8, 3), 0.5, ""
    key = (tuple(ev["region"]), ev["colour"])
    if key not in cache:
        sub, hshare = text_crop(fr, ev["region"], ev["colour"])
        cache[key] = (ocr.read(sub) if sub is not None else ("", 0.0)) + (hshare,)
    text, conf, hshare = cache[key]
    if conf < p.get("min_conf", 0.5):
        return False, 0.0, 0.0, text
    if p.get("any_word"):
        letters = sum(ch.isalpha() for ch in text)
        ok = letters >= p.get("min_letters", 3) and hshare >= p.get("min_height", 0.07)
        return ok, ev["strength"], round(conf, 3), text.strip().upper()
    hit = match_words(text, ev["words"], p.get("max_dist", 0.25))
    if hit is None:
        return False, 0.0, 0.0, text
    return True, ev["strength"], round(conf * (1.0 - hit[1]), 3), hit[0]


def detect_events(profile: dict, frames, ocr=None, on_frame=None, stats: dict = None) -> list:
    """
    Event onsets over (t, rgb) frames in time order. An event starts when its cue holds for min_s; the
    same event can start again only after the cue was off for params.rearm_s (default 2 s) and cooldown_s
    has passed since its last start. Returns [{t, event, label, reason, strength, method, conf, text}].
    on_frame(t, frame) is called once per frame (the face signal uses it); stats[event id] counts the frames
    on which OCR read an expected word (game auto-detection).
    """
    evs = profile["events"]
    st = {
        e["id"]: {"since": None, "last": -1e9, "last_on": -1e9, "off_since": -1e9, "armed": True, "best": None}
        for e in evs
    }
    bursts = {e["id"]: ([], []) for e in evs if e["method"] == "text_burst"}
    out, prev = [], None
    for t, fr in frames:
        if on_frame is not None:
            on_frame(t, fr)
        cache, burst = {}, {}
        for e in evs:
            if e["method"] == "text_burst":
                prev_rows, hist = bursts[e["id"]]
                rows = text_rows(fr, e["region"])
                hist.append((t, new_rows(prev_rows, rows) if prev is not None else 0))
                win = e["params"].get("window_s", 4.0)
                hist[:] = [(a, n) for a, n in hist if a > t - win]
                bursts[e["id"]] = (rows, hist)
                burst[e["id"]] = sum(n for _, n in hist)
        for e in evs:
            s = st[e["id"]]
            on, strength, conf, text = _cue(e, t, fr, prev, ocr, cache, burst)
            if on and stats is not None and ocr is not None and e["method"] in ("ocr", "desat_ocr"):
                stats[e["id"]] = stats.get(e["id"], 0) + 1  # frames where an expected word was read
            if not on:
                if s["since"] is not None and t - s["last_on"] < e["params"].get("gap_s", 0.0):
                    continue  # a short dropout (blinking wanted stars) does not end the cue
                if s["since"] is not None:
                    s["since"], s["best"] = None, None
                    s["off_since"] = t
                if not s["armed"] and t - s["off_since"] >= e["params"].get("rearm_s", 2.0):
                    s["armed"] = True
                continue
            if s["since"] is None:
                s["since"] = t
            s["last_on"] = t
            if s["best"] is None or strength > s["best"][1]:
                s["best"] = (t, strength, conf, text)
            if s["armed"] and t - s["since"] >= e["min_s"] and t - s["last"] >= e["cooldown_s"]:
                _, b_str, b_conf, b_text = s["best"]
                out.append(
                    {
                        "t": round(s["since"], 2),
                        "event": e["id"],
                        "label": e["label"],
                        "reason": e["reason"],
                        "strength": round(b_str, 3),
                        "method": e["method"],
                        "conf": b_conf,
                        "text": b_text,
                    }
                )
                s["last"], s["armed"] = s["since"], False
        prev = fr
    return sorted(out, key=lambda x: x["t"])
