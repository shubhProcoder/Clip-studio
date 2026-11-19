"""
Submagic-style effects (all opt-in, stored on the clip plan as plan["effects"]): the punch-in zoom events,
the face-safe clamp, the ffmpeg perspective filter that renders them and the CapCut keyframes that match.
Filler removal lives in fillers.py, the brand kit in brand.py; speech enhancement is a filter chain in
render._audio_graph.
"""

from streamclipper.captions.coverage import spoken_words_tl
from streamclipper.util import ToolError, find_triggers

ZOOM_LEVELS = {"off": 1.0, "light": 1.08, "medium": 1.12, "strong": 1.2}
ENHANCE_MIX = {"off": None, "light": 0.6, "strong": 0.9}  # arnndn mix: 10-40% of the dry signal stays
EFFECT_DEFAULTS = {"zoom": "off", "remove_fillers": False, "enhance_speech": "off", "brand_kit": ""}

EASE_IN = 0.25  # seconds from 1.0 to the zoom level
HOLD_TO = 1.2  # the zoom holds until this long after the event start
EASE_OUT = 0.3  # back to 1.0
LEAD = 0.1  # the zoom starts this long before the word, so it is in when the word lands
MIN_GAP = 3.0  # at most one zoom per 3 s
MAX_EVENTS = 6
FACE_MARGIN = 0.04  # a known face stays this far (share of the panel) inside the zoomed view
MIN_LEVEL = 1.03  # a zoom that the face clamp pushes below this is skipped


def check_effects(raw: dict) -> dict:
    """Effect values (CLI/spec/API) -> a clean dict; ToolError on an unknown choice."""
    raw = raw or {}
    out = dict(EFFECT_DEFAULTS)
    z = raw.get("zoom") or "off"
    if z not in ZOOM_LEVELS:
        raise ToolError(f"Bad zoom '{z}'.", "use one of: " + ", ".join(ZOOM_LEVELS))
    e = raw.get("enhance_speech") or "off"
    if e not in ENHANCE_MIX:
        raise ToolError(f"Bad enhance_speech '{e}'.", "use one of: " + ", ".join(ENHANCE_MIX))
    out.update(zoom=z, enhance_speech=e, remove_fillers=bool(raw.get("remove_fillers")))
    out["brand_kit"] = str(raw.get("brand_kit") or "")
    if raw.get("brand_sha1"):
        out["brand_sha1"] = str(raw["brand_sha1"])
    return out


def effects_of(plan: dict) -> dict:
    """The plan's effects with defaults filled in (plans before 5.3 have none: everything off)."""
    return check_effects(plan.get("effects") or {})


def any_on(eff: dict) -> bool:
    eff = eff or {}
    return (
        (eff.get("zoom") or "off") != "off"
        or bool(eff.get("remove_fillers"))
        or (eff.get("enhance_speech") or "off") != "off"
        or bool(eff.get("brand_kit"))
    )


def _tl_total(plan: dict) -> float:
    segs = plan.get("segments") or []
    return max((float(s["tl_start"]) + float(s["src_e"]) - float(s["src_s"]) for s in segs), default=0.0)


def _payoff_tl(plan: dict):
    """Timeline second of the payoff peak in a story (non-tease) piece, or None."""
    if plan.get("peak") is None:
        return None
    p = float(plan["peak"])
    best = None
    for s in plan.get("segments") or []:
        if s.get("kind") == "tease":
            continue
        a, b, t0 = float(s["src_s"]), float(s["src_e"]), float(s["tl_start"])
        if a <= p < b:
            return t0 + p - a
        if p < a and (best is None or a - p < best[0]):
            best = (a - p, t0)  # the peak fell into a cut: the next piece's start
    return best[1] if best and best[0] < 1.0 else None


def zoom_events(plan: dict, level: str = None) -> list:
    """
    Punch-in zoom events [{t, level, why, word}] on the clip timeline: the payoff first, then hype/trigger
    words (the red caption words), at most one per MIN_GAP seconds and MAX_EVENTS per clip. t is where the
    zoom starts (LEAD before the word); each event takes EASE_IN + (HOLD_TO - EASE_IN) + EASE_OUT seconds.
    """
    name = level or effects_of(plan)["zoom"]
    lv = ZOOM_LEVELS.get(name, 1.0)
    if lv <= 1.0:
        return []
    total = _tl_total(plan)
    span = HOLD_TO + EASE_OUT
    words = spoken_words_tl(plan)
    cands = []
    p = _payoff_tl(plan)
    if p is not None:
        cands.append((0, p, "payoff", ""))
    for a, b, _label in find_triggers([w[0] for w in words], digits=False):
        cands.append((1, words[a][1], "hype", " ".join(w[0] for w in words[a : b + 1])))
    events = []
    for _prio, t, why, word in sorted(cands, key=lambda c: (c[0], c[1])):
        t0 = round(max(0.0, t - LEAD), 3)
        if t0 + span > total + 1e-6:
            continue
        if any(abs(t0 - e["t"]) < MIN_GAP for e in events):
            continue
        events.append({"t": t0, "level": lv, "why": why, "word": word})
        if len(events) >= MAX_EVENTS:
            break
    return sorted(events, key=lambda e: e["t"])


def _union(boxes: list):
    boxes = [b for b in boxes or [] if b and b[2] > b[0] and b[3] > b[1]]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _axis(level: float, c: float, f0, f1, m: float):
    """Left edge of the zoomed view (share of the panel) on one axis, or None when the face cannot fit."""
    w = 1.0 / level
    lo, hi = 0.0, 1.0 - w
    if f0 is not None:
        lo = max(lo, min(1.0 - w, f1 + m - w))  # the face's far edge (+ margin) stays in view
        hi = min(hi, max(0.0, f0 - m))  # ... and its near edge
        if lo > hi + 1e-9:
            return None
    return min(hi, max(lo, c - w / 2))


def face_safe(level: float, faces: list, centre: tuple = None, margin: float = FACE_MARGIN):
    """
    Zoom level and fixed point (fx, fy, 0-1) for a punch-in on a panel, so every known face box (normalised
    panel boxes [x0, y0, x1, y1]) stays inside the zoomed view with `margin` (a face closer than that to the
    panel edge keeps the edge). The level drops until the faces fit; None when it would drop below MIN_LEVEL.
    The view is centred on the faces (else on `centre`, default the panel centre).
    """
    u = _union(faces)
    if centre is None:
        centre = ((u[0] + u[2]) / 2, (u[1] + u[3]) / 2) if u else (0.5, 0.5)
    lv = float(level)
    while lv >= MIN_LEVEL - 1e-9:
        fx0, fy0, fx1, fy1 = u if u else (None, None, None, None)
        left = _axis(lv, centre[0], fx0, fx1, margin)
        top = _axis(lv, centre[1], fy0, fy1, margin)
        if left is not None and top is not None:
            k = 1.0 - 1.0 / lv
            return round(lv, 3), round(left / k, 4), round(top / k, 4)
        lv = round(lv - 0.01, 3)
    return None


def view_box(level: float, fx: float, fy: float) -> tuple:
    """The visible part of the panel (normalised x0, y0, x1, y1) at full zoom around fixed point (fx, fy)."""
    k = 1.0 - 1.0 / level
    return (fx * k, fy * k, fx * k + 1.0 / level, fy * k + 1.0 / level)


def envelope(t: float, t0: float) -> float:
    """0-1 zoom amount at timeline second t of an event starting at t0 (smoothstep in, hold, smoothstep out)."""

    def ss(x):
        x = max(0.0, min(1.0, x))
        return x * x * (3 - 2 * x)

    return ss((t - t0) / EASE_IN) * ss((t0 + HOLD_TO + EASE_OUT - t) / EASE_OUT)


def _frames(ev: dict, fps: int) -> tuple:
    a = int(round(ev["t"] * fps))
    return a, a + EASE_IN * fps, a + HOLD_TO * fps, a + (HOLD_TO + EASE_OUT) * fps


def perspective_filter(events: list, fps: int = 30) -> str:
    """
    ffmpeg perspective filter (eval=frame, works on ffmpeg 4.4) for the face-safe events of one panel
    ([{t, level, fx, fy}]): per frame `on`, the source corners of a view 1/z of the panel around the event's
    fixed point. Frames outside the events are left alone (enable). '' when there is nothing to zoom.
    """
    events = [e for e in events if e.get("level", 1.0) > 1.0]
    if not events:
        return ""

    def ss(x):
        return f"({x})*({x})*(3-2*({x}))"

    terms, en = [], []
    for e in events:
        a, b, c, d = _frames(e, fps)
        up = f"clip((on-{a})/{b - a:g},0,1)"
        down = f"clip(({d:g}-on)/{d - c:g},0,1)"
        env = f"{ss(up)}*{ss(down)}"
        terms.append((e, f"(1-1/(1+{e['level'] - 1:.4f}*{env}))"))
        en.append(f"between(n,{a},{int(d) + 1})")
    q = "+".join(t for _, t in terms)
    x0 = "W*(" + "+".join(f"{e['fx']:.4f}*{t}" for e, t in terms) + ")"
    y0 = "H*(" + "+".join(f"{e['fy']:.4f}*{t}" for e, t in terms) + ")"
    x1 = f"{x0}+W*(1-({q}))"
    y2 = f"{y0}+H*(1-({q}))"
    corners = {"x0": x0, "y0": y0, "x1": x1, "y1": y0, "x2": x0, "y2": y2, "x3": x1, "y3": y2}
    opts = ":".join(f"{k}='{v}'" for k, v in corners.items())
    return f"perspective={opts}:interpolation=linear:sense=source:eval=frame:enable='{'+'.join(en)}'"


GAME_MAIN = ("split", "uncut-stack", "hook-grabber")  # layouts whose main panel is the gameplay
FACE_REL_H = 0.5  # faces smaller than this share of the biggest one (posters, crowd) do not limit the zoom
SAMPLE_DT = (EASE_IN, 0.7, HOLD_TO)  # where in an event the faces are looked up


def _faces_at(det, path, times: list):
    """Union-ready face boxes (normalised panel boxes) on a panel file at the given panel times."""
    from streamclipper.layout import probe_video
    from streamclipper.vision import _grab_rgb, _grab_size

    meta = probe_video(path)
    gw, gh = _grab_size(det, meta["width"], meta["height"])
    boxes = []
    for t in times:
        rgb = _grab_rgb(path, min(max(0.0, t), max(0.0, meta["dur"] - 0.05)), gw, gh)
        if rgb is None:
            continue
        found = [d["box"] for d in det.detect(rgb)]
        big = max((b[3] - b[1] for b in found), default=0.0)
        boxes += [b for b in found if b[3] - b[1] >= FACE_REL_H * big]
    return boxes


def plan_zoom(plan: dict, level: str = None) -> dict:
    """
    Face-safe zoom events per panel: {level, main: [...], overlay: [...], notes: [...]}; each event
    {t, level, fx, fy, why, word, faces}. Faces come from faces.get_detector() on 3 frames per event of the
    panel file. A panel that shows the streamer (cam panel, full/speaker layouts) is zoomed only where a face
    was found; without a face detector only gameplay panels are zoomed.
    """
    from streamclipper.faces import get_detector
    from streamclipper.render import tl_to_src

    name = level or effects_of(plan)["zoom"]
    out = {"level": name, "main": [], "overlay": [], "notes": []}
    events = zoom_events(plan, name)
    if not events:
        return out
    pn = plan.get("panels") or {}
    panels = [("main", pn.get("main_src"), plan.get("layout") not in GAME_MAIN)]
    if pn.get("overlay_src") and not pn.get("overlay_loop"):
        panels.append(("overlay", pn["overlay_src"], True))
    det = get_detector()
    for which, path, face_panel in panels:
        if not path:
            continue
        if det is None and face_panel:
            out["notes"].append(f"{which} panel not zoomed: no face detector (pip install -e .[vision])")
            continue
        for ev in events:
            boxes = [] if det is None else _faces_at(det, path, [tl_to_src(plan, ev["t"] + dt) for dt in SAMPLE_DT])
            if face_panel and not boxes:
                out["notes"].append(f"{which} zoom at {ev['t']:.1f}s skipped: no face found to keep in view")
                continue
            fit = face_safe(ev["level"], boxes)
            if fit is None:
                out["notes"].append(f"{which} zoom at {ev['t']:.1f}s skipped: the face would not fit")
                continue
            lv, fx, fy = fit
            out[which].append(dict(ev, level=lv, fx=fx, fy=fy, faces=len(boxes)))
    return out


def zoom_plan_for(plan: dict) -> dict:
    """The plan's cached zoom placement when it matches the zoom level, else a new one."""
    name = effects_of(plan)["zoom"]
    zp = plan.get("zoom_plan")
    if isinstance(zp, dict) and zp.get("level") == name:
        return zp
    return plan_zoom(plan, name)


def add_capcut_zoom(spec: dict, zp: dict, panel_h: dict) -> int:
    """
    zoom_keys on the CapCut video entries that hold each event (main track: zp['main'], overlay track:
    zp['overlay']); their punch-zoom anim gives way to the zoom. panel_h = {'main': h/H, 'overlay': h/H}.
    Returns the number of segments with keys.
    """
    n = 0
    tracks = [("main", spec.get("video_track") or [])]
    if spec.get("overlay_tracks"):
        tracks.append(("overlay", spec["overlay_tracks"][0]))
    for which, items in tracks:
        evs = zp.get(which) or []
        if not evs:
            continue
        for it in items:
            tl0 = float(it.get("tl_start", 0.0))
            dur = float(it["src_end"]) - float(it["src_start"])
            keys = []
            for ev in evs:
                if ev["t"] < tl0 + dur and ev["t"] + HOLD_TO + EASE_OUT > tl0:
                    keys += capcut_zoom_keys(
                        ev, tl0, dur, float(it.get("pos_x", 0.0)), float(it.get("pos_y", 0.0)), panel_h.get(which, 1)
                    )
            if keys:
                base = float(it.get("zoom", 1.0) or 1.0)
                it["zoom_keys"] = sorted((t, round(s * base, 4), x, y) for t, s, x, y in keys)
                it["anim"] = "none"
                n += 1
    return n


def capcut_zoom_keys(
    ev: dict, seg_tl: float, seg_dur: float, base_x: float = 0.0, base_y: float = 0.0, panel_h: float = 1.0
) -> list:
    """
    CapCut keyframe points [(time offset from the segment start in us, scale, x, y)] for one face-safe event
    on a video segment: the MP4's curve at its corners, plus the segment edges when the event crosses them.
    CapCut scales a segment around its own centre, so the position moves the fixed point back where it was
    (CapCut units: half the canvas = 1, y up; panel_h = panel height / canvas height).
    """
    lv, fx, fy = ev["level"], ev["fx"], ev["fy"]
    t_end = ev["t"] + HOLD_TO + EASE_OUT
    times = [ev["t"] + dt for dt in (0.0, EASE_IN, HOLD_TO, HOLD_TO + EASE_OUT)]
    times += [x for x in (seg_tl, seg_tl + seg_dur) if ev["t"] < x < t_end]
    pts = []
    for t in sorted(set(round(x, 4) for x in times)):
        rel = t - seg_tl
        if rel < -1e-6 or rel > seg_dur + 1e-6:
            continue
        z = 1.0 + (lv - 1.0) * envelope(t, ev["t"])
        dx = (0.5 - fx) * (z - 1) * 2
        dy = -(0.5 - fy) * (z - 1) * 2 * panel_h
        pts.append((int(round(max(0.0, rel) * 1_000_000)), round(z, 4), round(base_x + dx, 4), round(base_y + dy, 4)))
    return pts
