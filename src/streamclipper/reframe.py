"""
Reframe editor model (pure functions, no I/O). An edit is cut into contiguous segments; each segment has its own
layout and crop window(s), and optional keyframes that pan a window inside the segment.

build_panel_graph turns the segments into ONE ffmpeg filter graph: fps=30, a frame-exact trim per segment, a
per-window crop whose x/y are time expressions, scale, stack, and concat into a full-canvas composite panel. The
source audio is kept as one continuous stream. The normal MP4 path then adds captions, loudness and the export
gate. Why one graph instead of one MP4 per segment + the concat demuxer: frames are cut on exact frame numbers,
so the timeline length is exact, and there are no AAC priming gaps or loudness steps at the joins. Why no
sendcmd: its behaviour differs between ffmpeg versions. crop evaluates x/y per frame but w/h only once (and
scale with per-frame sizes needs ffmpeg >= 5), so keyframes pan a window; they never resize it.
"""

import copy
import math

from streamclipper.util import ToolError

MIN_SEG = 0.5  # seconds
MAX_SEGS = 20
MAX_KEYS = 12  # per segment
SMOOTH_S = 0.5  # a smooth join eases over this many seconds at the end of the left segment
FPS = 30
MIN_BOX = 64
ASPECT_TOL = 0.03
TOL = 1 / FPS + 1e-6
LAYOUTS = ("full", "caption-punch", "split", "square-frame", "speaker")
EASES = ("smooth", "linear")
BOUNDARIES = ("cut", "smooth")
WINDOWS = ("cam", "game")
LABELS = {"cam": "facecam", "game": "gameplay"}
SQUARE_Y, SQUARE_H = 470, 900
SMOOTH_HINT = "smooth needs the same layout and crop size on both sides; use Cut"
KEY_SPACING = 1.0  # Auto-reframe proposes at most one keyframe per second


def canvas(ratio: str) -> tuple:
    return (1080, 1920) if ratio == "9:16" else (1080, 1080)


def panel_rects(layout: str, ratio: str) -> list:
    """[(which, x, y, w, h)] canvas rects the windows of a layout fill (square-frame: over a blurred copy)."""
    W, H = canvas(ratio)
    if layout == "split":
        cam_h = 672 if ratio == "9:16" else 454
        return [("cam", 0, 0, W, cam_h), ("game", 0, cam_h, W, H - cam_h)]
    if layout == "square-frame":
        return [("game", 0, SQUARE_Y, W, SQUARE_H)]
    return [("game", 0, 0, W, H)]


def window_aspects(layout: str, ratio: str) -> dict:
    """{which: w/h} of every window a layout needs (the speaker layout uses one full window here)."""
    return {which: w / h for which, _x, _y, w, h in panel_rects(layout, ratio)}


def _num(v, what: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ToolError(f"{what} must be a number.", "move the handle again") from None
    if not math.isfinite(f):
        raise ToolError(f"{what} must be a number.", "move the handle again")
    return f


def _even(v: float) -> int:
    return int(math.floor(v)) & ~1


def clamp_box(box, vw: int, vh: int) -> list:
    """Even [x, y, w, h] moved (and, when bigger than the frame, shrunk) so it lies inside the frame."""
    x, y, w, h = (float(v) for v in box)
    w, h = max(2, min(_even(w), vw & ~1)), max(2, min(_even(h), vh & ~1))
    x = max(0, min(int(round(x)), vw - w)) & ~1
    y = max(0, min(int(round(y)), vh - h)) & ~1
    return [x, y, w, h]


def lock_aspect(box, aspect: float, vw: int, vh: int, min_box: int = MIN_BOX) -> list:
    """
    Even [x, y, w, h] with w/h = aspect (within ASPECT_TOL): keeps the box width and centre, shrinks to fit the
    frame, grows to at least min_box on both sides, then clamps inside the frame.
    """
    x, y, w, h = (float(v) for v in box)
    cx, cy = x + w / 2, y + h / 2
    w = max(2.0, w)
    h = w / aspect
    fit = min(1.0, vw / w, vh / h)
    w, h = w * fit, h * fit
    grow = max(1.0, min_box / w, min_box / h)
    w, h = min(vw, w * grow), min(vh, h * grow)
    w = _even(w)
    h = int(round(w / aspect / 2)) * 2
    if h > vh:
        h = vh & ~1
        w = _even(h * aspect)
    return clamp_box([cx - w / 2, cy - h / 2, w, h], vw, vh)


def check_box(v, what: str, vw: int, vh: int, aspect) -> list:
    """An editor crop box [x, y, w, h] (even px), or a ToolError: missing, too small, outside, wrong shape."""
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        raise ToolError(f"The {what} box is missing.", "click Reset to auto-detected")
    x, y, w, h = (int(round(_num(n, f"The {what} box"))) for n in v)
    x, y, w, h = x & ~1, y & ~1, w & ~1, h & ~1
    if w < MIN_BOX or h < MIN_BOX:
        raise ToolError(f"The {what} box is too small.", f"make it at least {MIN_BOX} x {MIN_BOX} pixels")
    if x < 0 or y < 0 or x + w > vw or y + h > vh:
        raise ToolError(f"The {what} box goes outside the video frame.", "drag it back inside the frame")
    if aspect and abs((w / h) / aspect - 1) > ASPECT_TOL:
        raise ToolError(f"The {what} box has the wrong shape for this layout.", "resize it with the corner handle")
    return [x, y, w, h]


def normalize_segments(form: dict, ctx: dict = None) -> list:
    """The segments of an editor form; an old form (no segments) is one segment of its layout and boxes."""
    if form.get("segments"):
        return form["segments"]
    return [
        {
            "start": form["start"],
            "end": form["end"],
            "layout": form["layout"],
            "windows": {"cam": form.get("cam"), "game": form.get("game")},
            "keys": [],
            "ease": "smooth",
            "boundary": "cut",
        }
    ]


def uses_reframe(segs) -> bool:
    """True when the edit needs the reframe render (2+ segments or any keyframe); else the old path is used."""
    return bool(segs) and isinstance(segs, list) and (len(segs) > 1 or any((s or {}).get("keys") for s in segs))


def smooth_allowed(a: dict, b: dict) -> bool:
    """A smooth join needs the same layout and the same window sizes on both sides."""
    if a.get("layout") != b.get("layout"):
        return False
    wa, wb = a.get("windows") or {}, b.get("windows") or {}
    for which in WINDOWS:
        x, y = wa.get(which), wb.get(which)
        if (x is None) != (y is None) or (x and list(x[2:]) != list(y[2:])):
            return False
    return True


def validate_segments(segs, ctx: dict, start: float, end: float) -> list:
    """
    Checked, cleaned segments of an edit (times rounded to ms, the first starting at `start`, the last ending
    at `end`), or a ToolError with a plain message for: unknown layout, a 9:16-only layout on 1:1, a segment
    shorter than MIN_SEG, a gap or overlap, too many segments or keyframes, keyframes outside the segment or out of
    order, a keyframe window of another size, and any box outside the frame, too small or of the wrong shape.
    """
    vw, vh, ratio = int(ctx["width"]), int(ctx["height"]), ctx["ratio"]
    if not isinstance(segs, list) or not segs:
        raise ToolError("The segments are missing.", "reload the editor")
    if len(segs) > MAX_SEGS:
        raise ToolError(f"Too many segments ({len(segs)}).", f"use at most {MAX_SEGS}; merge some of them")
    out, prev = [], float(start)
    for i, s in enumerate(segs):
        n = i + 1
        if not isinstance(s, dict):
            raise ToolError(f"Segment {n} is not valid.", "reload the editor")
        layout = s.get("layout")
        if layout not in LAYOUTS:
            raise ToolError(f"Segment {n} has an unknown layout.", "pick one from the list")
        if layout in ("square-frame", "caption-punch") and ratio != "9:16":
            raise ToolError(f"Segment {n}: this layout is 9:16 only.", "pick split or full for a 1:1 clip")
        a = _num(s.get("start"), f"The start of segment {n}")
        b = _num(s.get("end"), f"The end of segment {n}")
        if abs(a - prev) > TOL:
            if i == 0:
                raise ToolError("The first segment must start where the clip starts.", "reload the editor")
            if a > prev:
                raise ToolError(
                    f"There is a gap before segment {n} ({prev:.2f}-{a:.2f} s).",
                    "segments must follow each other; split at the playhead instead",
                )
            raise ToolError(f"Segment {n} overlaps the segment before it.", "move the split point")
        a = prev
        if b - a < MIN_SEG - 1e-6:
            raise ToolError(f"Segment {n} is shorter than {MIN_SEG:g} s.", "move the split, or merge the segment")
        asp = window_aspects(layout, ratio)
        win_in = s.get("windows") or {}
        if not isinstance(win_in, dict):
            raise ToolError(f"The windows of segment {n} are not valid.", "click Reset to auto-detected")
        wins = {k: None for k in WINDOWS}
        for which, aspect in asp.items():
            wins[which] = check_box(win_in.get(which), f"segment {n} {LABELS[which]}", vw, vh, aspect)
        keys_in = s.get("keys") or []
        if not isinstance(keys_in, list):
            raise ToolError(f"The keyframes of segment {n} are not valid.", "delete them and add them again")
        if len(keys_in) > MAX_KEYS:
            raise ToolError(
                f"Segment {n} has too many keyframes ({len(keys_in)}).", f"use at most {MAX_KEYS} per segment"
            )
        keys, last = [], None
        for j, k in enumerate(keys_in):
            if not isinstance(k, dict):
                raise ToolError(f"Keyframe {j + 1} of segment {n} is not valid.", "delete it and add it again")
            t = _num(k.get("t"), f"The time of keyframe {j + 1} of segment {n}")
            if t < a - 1e-3 or t > b + 1e-3:
                raise ToolError(
                    f"Keyframe {j + 1} of segment {n} is outside the segment.", "drag it inside, or delete it"
                )
            if last is not None and t <= last + 1e-3:
                raise ToolError(f"The keyframes of segment {n} are not in time order.", "drag them apart")
            kk = {"t": round(min(max(t, a), b), 3)}
            for which, aspect in asp.items():
                box = check_box(k.get(which), f"segment {n} keyframe {j + 1} {LABELS[which]}", vw, vh, aspect)
                if box[2:] != wins[which][2:]:
                    raise ToolError(
                        f"Keyframe {j + 1} of segment {n} has another {LABELS[which]} size than its segment.",
                        "keyframes only move a window; split at the playhead to change its size",
                    )
                kk[which] = box
            keys.append(kk)
            last = t
        ease = s.get("ease") or "smooth"
        if ease not in EASES:
            raise ToolError(f"Segment {n} has an unknown easing.", "pick smooth or linear")
        boundary = s.get("boundary") or "cut"
        if boundary not in BOUNDARIES:
            raise ToolError(f"The join after segment {n} is not valid.", "pick Cut or Smooth")
        out.append(
            {
                "start": round(a, 3),
                "end": round(b, 3),
                "layout": layout,
                "windows": wins,
                "keys": keys,
                "ease": ease,
                "boundary": boundary,
            }
        )
        prev = b
    if abs(prev - float(end)) > TOL:
        raise ToolError("The last segment must end where the clip ends.", "reload the editor")
    out[0]["start"], out[-1]["end"] = round(float(start), 3), round(float(end), 3)
    out[-1]["boundary"] = "cut"
    for i in range(len(out) - 1):
        if out[i]["boundary"] == "smooth" and not smooth_allowed(out[i], out[i + 1]):
            raise ToolError(f"The join after segment {i + 1} can not be smooth.", SMOOTH_HINT)
    if any(s["end"] - s["start"] < MIN_SEG - 1e-6 for s in out):
        raise ToolError(f"A segment is shorter than {MIN_SEG:g} s.", "move the split, or merge the segment")
    return out


def validate_anchor(v, ratio: str):
    """Caption anchor [align, y] (canvas px; align 2 = bottom, 5 = middle, 8 = top) or None for the layout's own."""
    if v is None or v == "" or v == []:
        return None
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ToolError("The caption position is not valid.", "click Auto next to the caption position")
    align, y = int(_num(v[0], "The caption alignment")), int(round(_num(v[1], "The caption position")))
    if align not in (2, 5, 8):
        raise ToolError("The caption alignment is not valid.", "click Auto next to the caption position")
    lo, hi = (200, 1600) if ratio == "9:16" else (100, 980)
    if not lo <= y <= hi:
        raise ToolError(f"The caption is too close to the edge (y {y}).", f"keep it between {lo} and {hi} px")
    return [align, y]


# ---- pan paths ----------------------------------------------------------------------------------------------


def ease_p(p: float, ease: str) -> float:
    p = min(1.0, max(0.0, p))
    return p * p * (3 - 2 * p) if ease == "smooth" else p


def pan_value(points: list, t: float, ease: str) -> float:
    """Value at time t of a pan path [(t, v)] (held before the first and after the last point)."""
    if t <= points[0][0]:
        return float(points[0][1])
    for (t0, v0), (t1, v1) in zip(points, points[1:], strict=False):
        if t < t1:
            return v0 + (v1 - v0) * ease_p((t - t0) / max(1e-3, t1 - t0), ease)
    return float(points[-1][1])


def window_points(segs: list, i: int, which: str) -> list:
    """[(t, (x, y))] of window `which` of segment i in source time: the window at the segment start, its keyframes,
    and for a smooth join the ease over the last SMOOTH_S seconds to the next segment's first position."""
    seg = segs[i]
    pts = [(seg["start"], tuple(seg["windows"][which][:2]))]
    for k in seg.get("keys") or []:
        if k["t"] - seg["start"] < 1e-3:
            pts[0] = (seg["start"], tuple(k[which][:2]))
        else:
            pts.append((k["t"], tuple(k[which][:2])))
    nxt = segs[i + 1] if i + 1 < len(segs) else None
    if nxt is not None and seg.get("boundary") == "smooth" and (nxt["windows"] or {}).get(which):
        ease = seg.get("ease") or "smooth"
        t0 = max(seg["start"], seg["end"] - SMOOTH_S)
        here = tuple(pan_value([(t, p[d]) for t, p in pts], t0, ease) for d in (0, 1))
        first = window_points(segs[: i + 2], i + 1, which)[0][1]
        pts = [p for p in pts if p[0] < t0 - 1e-6] + [(t0, here), (seg["end"], first)]
    return pts


def _sub(a: float) -> str:
    return f"-{a:.3f}" if a >= 0 else f"+{-a:.3f}"


def pan_expr(points: list, t0: float, ease: str) -> str:
    """
    ffmpeg expression of a pan path [(t, v)] (source time) for a filter whose t starts at t0: nested
    if(lt(t, ...)) pieces, smoothstep p*p*(3-2*p) or linear, held at both ends (like layout._path_expr).
    """
    pts = [(round(t - t0, 3), int(round(v))) for t, v in points]
    if len({v for _t, v in pts}) == 1:
        return str(pts[0][1])
    expr = str(pts[-1][1])
    for (a, va), (b, vb) in reversed(list(zip(pts, pts[1:], strict=False))):
        span = max(1e-3, b - a)
        p = f"(t{_sub(a)})/{span:.3f}"
        shape = f"{p}*{p}*(3-2*{p})" if ease == "smooth" else p
        expr = f"if(lt(t,{b:.3f}),{va}+({vb - va})*{shape},{expr})"
    return f"if(lt(t,{pts[0][0]:.3f}),{pts[0][1]},{expr})"


def segment_frames(segs: list, ws: float, we: float) -> list:
    """[(f0, f1)] panel frames per segment (30 fps from the panel start ws); the pad before the first and after the
    last segment belongs to them, so the ranges are contiguous and cover the whole panel."""
    total = int(round((we - ws) * FPS))
    cuts = [0] + [int(round((s["start"] - ws) * FPS)) for s in segs[1:]] + [total]
    return list(zip(cuts, cuts[1:], strict=False))


def frame_pieces(segs: list, ws: float) -> list:
    """[(a, b)] source times of the segments snapped to the panel's frame grid (contiguous, frame-exact)."""
    snap = [ws + round((s["start"] - ws) * FPS) / FPS for s in segs] + [ws + round((segs[-1]["end"] - ws) * FPS) / FPS]
    return [(round(a, 6), round(b, 6)) for a, b in zip(snap, snap[1:], strict=False)]


def assign_words(words: list, pieces: list) -> list:
    """Words per piece: each word goes to the piece it starts in (the first piece also takes a word that starts
    just before the clip), so no caption is shown twice at a join."""
    out = [[] for _ in pieces]
    for w in words:
        if w["e"] <= pieces[0][0] or w["s"] >= pieces[-1][1]:
            continue
        k = next((i for i, (_a, b) in enumerate(pieces) if w["s"] < b), len(pieces) - 1)
        out[k].append(w)
    return out


def build_panel_graph(segs: list, ws: float, we: float, ratio: str, vw: int, vh: int) -> str:
    """ffmpeg filter_complex for the composite panel of [ws, we] (input 0 seeked to ws): one trim per segment,
    per-window crop with pan expressions, scale, vstack/overlay, concat -> [v]."""
    W, H = canvas(ratio)
    frames = segment_frames(segs, ws, we)
    n = len(segs)
    head = f"[0:v]fps={FPS},split={n}" + "".join(f"[s{i}]" for i in range(n)) if n > 1 else f"[0:v]fps={FPS}[s0]"
    parts, labels = [head], []
    for i, (seg, (f0, f1)) in enumerate(zip(segs, frames, strict=True)):
        t0 = ws + f0 / FPS
        ease = seg.get("ease") or "smooth"

        def crop(which, i=i, t0=t0, ease=ease, seg=seg):
            _x, _y, w, h = seg["windows"][which]
            pts = window_points(segs, i, which)
            xe = pan_expr([(t, p[0]) for t, p in pts], t0, ease)
            ye = pan_expr([(t, p[1]) for t, p in pts], t0, ease)
            return f"crop={w}:{h}:x='{xe}':y='{ye}'"

        src = f"[s{i}]trim=start_frame={f0}:end_frame={f1},setpts=PTS-STARTPTS"
        layout = seg["layout"]
        if layout == "split":
            (_c, _cx, _cy, _cw, cam_h), _g = panel_rects("split", ratio)
            parts.append(
                f"{src},split=2[c{i}][g{i}];"
                f"[c{i}]{crop('cam')},scale={W}:{cam_h}:flags=lanczos,setsar=1[cc{i}];"
                f"[g{i}]{crop('game')},scale={W}:{H - cam_h}:flags=lanczos,setsar=1[gg{i}];"
                f"[cc{i}][gg{i}]vstack=2,format=yuv420p[v{i}]"
            )
        elif layout == "square-frame":
            parts.append(
                f"{src},split=2[a{i}][b{i}];"
                f"[a{i}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"scale=270:480,gblur=sigma=8,scale={W}:{H}:flags=bicubic[bg{i}];"
                f"[b{i}]{crop('game')},scale={W}:{SQUARE_H}:flags=lanczos[fg{i}];"
                f"[bg{i}][fg{i}]overlay=0:{SQUARE_Y},setsar=1,format=yuv420p[v{i}]"
            )
        else:
            parts.append(f"{src},{crop('game')},scale={W}:{H}:flags=lanczos,setsar=1,format=yuv420p[v{i}]")
        labels.append(f"[v{i}]")
    return ";".join(parts) + ";" + "".join(labels) + f"concat=n={n}:v=1:a=0,format=yuv420p[v]"


def auto_anchor(layout: str, ratio: str):
    """Caption anchor of a reframe panel when the user did not drag the caption: the first segment's layout default
    (None = the full layout's own low position)."""
    if layout == "split":
        return [8, (672 if ratio == "9:16" else 454) + 28]
    if layout == "square-frame":
        return [8, 1400]
    return None


# ---- Auto-reframe -------------------------------------------------------------------------------------------


def _thin(path: list, spacing: float, limit: int) -> list:
    """At most limit + 1 points of a [(t, x)] path, at least `spacing` seconds apart (the last point kept)."""
    if len(path) <= 1:
        return list(path)
    span = path[-1][0] - path[0][0]
    spacing = max(spacing, span / max(1, limit))
    out = [path[0]]
    for p in path[1:-1]:
        if p[0] - out[-1][0] >= spacing - 1e-6:
            out.append(p)
    if path[-1][0] - out[-1][0] >= 0.25 * spacing:
        out.append(path[-1])
    return out[: limit + 1]


CORNER_CAM_AREA = 0.2  # a facecam overlay covers at most this share of the frame
IN_CAM_SHARE = 0.6  # share of a segment's faces inside the cam box that makes it a facecam segment
FACE_TOP_Y = 0.3  # full layout: the face centre goes this far down the window (upper third)
MIN_ZOOM_H = 0.75  # a full-height window may shrink to this share of the frame height to frame a low face


def corner_cam(cam, vw: int, vh: int) -> bool:
    """True when the detected cam box [x, y, w, h] is a small overlay in a corner of the frame."""
    if not cam:
        return False
    x, y, w, h = cam
    cx, cy = (x + w / 2) / vw, (y + h / 2) / vh
    return w * h <= CORNER_CAM_AREA * vw * vh and abs(cx - 0.5) > 0.15 and abs(cy - 0.5) > 0.15


def _in_box(face, box, vw: int, vh: int) -> bool:
    fx, fy = (face[0] + face[2]) / 2 * vw, (face[1] + face[3]) / 2 * vh
    return box[0] <= fx <= box[0] + box[2] and box[1] <= fy <= box[1] + box[3]


def propose_keys(
    segs: list,
    times: list,
    faces: list,
    vw: int,
    vh: int,
    ratio: str,
    cam=None,
    split_game=None,
    track: dict = None,
    spans: list = None,
) -> tuple:
    """
    Auto-reframe: (segments, note). Per segment the window that should show the streamer follows the largest face
    (layout.follow_path: smoothed with a dead zone, speed-limited), as pan keyframes about one per second; with 2+
    faces and track (vision.track_faces output with frames) it follows the active speaker instead (mouth motion
    while someone talks; spans = [(s, e)] speech in video time); split moves the cam window and keeps the gameplay
    window; square-frame is left as it is.
    cam = the detected facecam box [x, y, w, h] (source px). When it is a corner overlay and the face of a full
    segment sits inside it, that segment becomes split (cam panel on top, gameplay below; split_game = its gameplay
    crop) instead of a full-height pan that would keep the face low. A full-layout window puts the face in the upper
    third where it can: it moves up, and a full-height window with the face in the lower half shrinks (to
    MIN_ZOOM_H of the frame height at most) so the face moves up.
    """
    from streamclipper.layout import active_speaker_boxes, follow_path

    out, moved, keys_n, split_n = [], 0, 0, 0
    overlay = cam if corner_cam(cam, vw, vh) else None
    for seg in segs:
        seg = copy.deepcopy(seg)
        seg["keys"] = []
        which = "cam" if seg["layout"] == "split" else (None if seg["layout"] == "square-frame" else "game")
        idx = [k for k, t in enumerate(times) if seg["start"] - 1e-3 <= t <= seg["end"] + 1e-3]
        boxes = [faces[k][0] if faces[k] else None for k in idx]
        spk = active_speaker_boxes(track, seg["start"] - 1e-3, seg["end"] + 1e-3, spans) if track else None
        if which == "game" and spk and any(spk[1]):
            idx, boxes = spk
        if which is None or not any(boxes):
            out.append(seg)
            continue
        found = [b for b in boxes if b]
        if (
            which == "game"
            and overlay
            and seg["layout"] in ("full", "caption-punch")
            and sum(_in_box(b, overlay, vw, vh) for b in found) >= IN_CAM_SHARE * len(found)
        ):
            # the face is the facecam overlay, not the scene: show it in its own panel above the gameplay
            asp = window_aspects("split", ratio)
            game = split_game or [0, 0, vw, vh]
            seg["layout"] = "split"
            seg["windows"] = {
                "cam": lock_aspect(overlay, asp["cam"], vw, vh),
                "game": lock_aspect(game, asp["game"], vw, vh),
            }
            split_n += 1
            out.append(seg)
            continue
        _x, y, w, h = seg["windows"][which]
        cys = sorted((b[1] + b[3]) / 2 * vh for b in found)
        cy = cys[len(cys) // 2]
        top = FACE_TOP_Y if which == "game" else 0.42
        if which == "game" and h >= vh - 2 and cy > 0.5 * vh:
            # full height: y cannot move, so zoom in (bottom-aligned) until the face reaches the upper third
            nh = max(MIN_ZOOM_H * vh, min(h, (vh - cy) / (1 - top)))
            if nh < h - 2:
                _x, y, w, h = lock_aspect([_x + (w - nh * w / h) / 2, 0, nh * w / h, nh], w / h, vw, vh)
        ny = y if h >= vh else max(0, min(vh - h, int(round(cy - top * h)))) & ~1
        path = follow_path(boxes, [times[k] for k in idx], seg["start"], vw, vh, w)
        path = _thin(path, KEY_SPACING, MAX_KEYS)
        seg["windows"][which] = [int(path[0][1]) & ~1, ny, w, h]
        for t, x in path[1:]:
            ta = round(seg["start"] + t, 3)
            if ta <= seg["start"] + 1e-3 or ta > seg["end"]:
                continue
            key = {"t": ta, which: [int(x) & ~1, ny, w, h]}
            for other in WINDOWS:
                if other != which and seg["windows"].get(other):
                    key[other] = list(seg["windows"][other])
            seg["keys"].append(key)
        moved += 1
        keys_n += len(seg["keys"])
        out.append(seg)
    for a, b in zip(out, out[1:], strict=False):  # a changed layout or window size cannot join smoothly
        if a.get("boundary") == "smooth" and not smooth_allowed(a, b):
            a["boundary"] = "cut"
    if out:
        out[-1]["boundary"] = "cut"
    parts = []
    if moved:
        parts.append(f"Followed the face in {moved} segment(s) with {keys_n} keyframe(s).")
    if split_n:
        parts.append(f"Facecam in a corner: {split_n} segment(s) set to Split (facecam on top, gameplay below).")
    note = " ".join(parts) or "No face found in this clip; the windows stay as they are."
    return out, note
