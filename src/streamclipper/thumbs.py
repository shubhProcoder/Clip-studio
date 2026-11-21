"""Best-frame thumbnail per rendered clip: sharp frame (face visible when there is a cam panel) + hook text."""

import re
from pathlib import Path

from streamclipper.audio import _np
from streamclipper.captions.ass import build_ass
from streamclipper.layout import apply_caption_anchor, layout_boxes, probe_video
from streamclipper.render import render_still
from streamclipper.util import fwd, run_cmd

STEP_S = 0.25
SAMPLE_W = 270


def _frames(path: Path, start: float, dur: float, w: int, h: int, rgb: bool) -> list:
    """Frames every STEP_S seconds of [start, start+dur] as numpy arrays (h, w) grey or (h, w, 3) RGB."""
    np = _np()
    ch = 3 if rgb else 1
    res = run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, start):.3f}",
            "-t",
            f"{max(STEP_S, dur):.3f}",
            "-i",
            str(path),
            "-vf",
            f"fps={1 / STEP_S:g},scale={w}:{h},format={'rgb24' if rgb else 'gray'}",
            "-f",
            "rawvideo",
            "-",
        ],
        "thumbnail frame scan",
        binary=True,
    )
    buf = res.stdout or b""
    size = w * h * ch
    shape = (h, w, 3) if rgb else (h, w)
    return [np.frombuffer(buf[i : i + size], np.uint8).reshape(shape) for i in range(0, len(buf) - size + 1, size)]


def sharpness(grey) -> float:
    """Variance of the 4-neighbour Laplacian (higher = sharper)."""
    f = grey.astype("float32")
    lap = 4 * f[1:-1, 1:-1] - f[:-2, 1:-1] - f[2:, 1:-1] - f[1:-1, :-2] - f[1:-1, 2:]
    return float(lap.var())


def skin_ratio(rgb) -> float:
    """Share of skin-coloured pixels (same mask as the export gate's face check)."""
    px = rgb.astype("int16")
    r, g, b = px[..., 0], px[..., 1], px[..., 2]
    return float(((r > 85) & (g > 40) & (b > 20) & (r - g > 12) & (r > b)).mean())


def pick_frame(plan: dict) -> float:
    """Timeline second of the best frame in the payoff/reaction pieces (sharpness x face presence)."""
    pn = plan["panels"]
    segs = plan.get("segments") or []
    pick = [s for s in segs if s.get("kind") in ("payoff", "reaction")]
    pick = pick or [s for s in segs if s.get("kind") != "tease"] or segs
    main = Path(pn["main_src"])
    cam = Path(pn["overlay_src"]) if pn.get("overlay_src") and not pn.get("overlay_loop") else None
    mm = probe_video(main)
    mh = max(2, int(round(SAMPLE_W * mm["height"] / mm["width"] / 2)) * 2)
    cmeta = probe_video(cam) if cam else None
    chh = max(2, int(round(SAMPLE_W * cmeta["height"] / cmeta["width"] / 2)) * 2) if cmeta else 0
    best = None
    for s in pick:
        a, d = float(s["src_s"]), float(s["src_e"]) - float(s["src_s"])
        greys = _frames(main, a, d, SAMPLE_W, mh, rgb=False)
        faces = _frames(cam, a, d, SAMPLE_W, chh, rgb=True) if cam else []
        for i, gf in enumerate(greys):
            t = float(s["tl_start"]) + i * STEP_S
            if t > float(s["tl_start"]) + d - 0.05:
                break
            score = sharpness(gf)
            if cam:
                skin = skin_ratio(faces[i]) if i < len(faces) else 0.0
                score *= 0.5 + 0.5 * min(1.0, skin / 0.08)
            if best is None or score > best[0]:
                best = (score, t)
    if best is None:
        return float(segs[-1]["tl_start"]) if segs else 0.0
    return round(best[1], 3)


def make_thumbnail(plan: dict, mp4: Path) -> Path:
    """<name>_thumb.jpg next to the MP4 at the canvas size: best frame + the hook banner."""
    mp4 = Path(mp4).resolve()
    out = mp4.with_name(f"{mp4.stem}_thumb.jpg")
    rdir = mp4.parent / "_render" / re.sub(r"[^\w.-]", "_", plan.get("name") or mp4.stem)
    rdir.mkdir(parents=True, exist_ok=True)
    fdir = mp4.parent / "_render" / "_fonts"
    t = pick_frame(plan)
    mmeta = probe_video(Path(plan["panels"]["main_src"]))
    lay = layout_boxes(plan["ratio"], plan["layout"], main_h=mmeta["height"])
    apply_caption_anchor(lay, plan["panels"].get("caption_anchor"))
    hook = str(plan.get("hook_text") or "")
    caps = [{"text": hook, "tl_start": 0.0, "tl_end": 30.0, "layer": 1}] if hook.strip() else []
    ass = rdir / "thumb.ass"
    build_ass(dict(plan, captions=caps, segments=[]), lay, ass, fdir)
    render_still(plan, t, out, ass)
    print(f"Thumbnail -> {fwd(out)} (t={t:.2f}s)")
    return out
