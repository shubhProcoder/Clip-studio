"""`streamclipper styles --preview`: a ~4 s sample of every caption style and hook style, plus a labelled grid."""

import argparse
import json
from pathlib import Path

import numpy as np

from streamclipper.captions import capcut
from streamclipper.captions.styles import DEFAULT_HOOK_STYLE, DEFAULT_STYLE, HOOK_STYLES, STYLES
from streamclipper.gate import _gate_summary, render_and_gate
from streamclipper.layout import probe_video
from streamclipper.paths import TEMPLATE_DIR
from streamclipper.util import ToolError, fwd, run_cmd

# Sample words per caption style, chosen to show its mechanic, and the word whose caption the tile shows.
# Most are hype words, which clips colour red; the gallery shows them in the style's own colours instead.
STYLE_SAMPLES = {
    "bold-pop": ("no way", 1),
    "beast-bold": ("let's go", 1),
    "karaoke-highlight": ("he is cheating", 2),
    "word-by-word": ("insane", 0),
    "two-line-stack": ("i can't believe this", 3),
    "highlight-box": ("clip that", 0),
    "neon-glow": ("chat look", 1),
    "impact-shake": ("bro!", 0),
    "emoji-pop": ("im dead", 1),  # the style adds the skull after "dead" (left out without an emoji font)
    "typewriter": ("wait for it...", 2),
    "bounce": ("gg", 0),
    "gradient-stroke": ("clutch", 0),
    "comic-burst": ("boom", 0),
    "minimal-clean": ("that was close", 2),
    "gaming-glitch": ("wasted", 0),
}
HOOK_SAMPLES = {
    "bold-box": "HE DIDN'T SEE THAT COMING",
    "outline-glow": "WAIT FOR THE END",
    "top-bar": "CHAT WENT CRAZY",
}
SAMPLE_TEXT = "this is how your captions look"  # styles added later without their own sample
SAMPLE_DUR = 4.0
SETTLE_S = 0.3  # pop / bounce / shake are over 0.16 s after a caption starts
GLITCH_S = 0.07  # the glitch ghosts show for the first 0.15 s
HOOK_T = 1.0  # the hook banner shows for the first 2 s
TILE_W, TILE_H, LABEL_H = 520, 280, 40
GRID_COLS, GAP = 3, 6
PAGE_BG, LABEL_BG = "0x0d1117", "0x161b22"  # GitHub dark mode


def sample_words(dur: float, text: str = SAMPLE_TEXT) -> list:
    """English test words spread evenly between 0.3 s and dur - 0.3 s, spoken as one phrase (short gaps)."""
    words = text.split()
    step = (dur - 0.6) / len(words)
    return [
        {"word": w, "start": round(0.3 + i * step, 3), "end": round(0.3 + (i + 1) * step - 0.05, 3)}
        for i, w in enumerate(words)
    ]


def background_frame(w: int = 1920, h: int = 1080) -> np.ndarray:
    """A flat near-black still (#0a0a0a) with a faint vignette to black at the edges: every text colour reads."""
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    u, v = x / w - 0.5, y / h - 0.5
    img = 10.0 * np.clip(1 - 1.2 * (u * u + v * v), 0, 1)
    return np.repeat(np.round(img)[..., None], 3, axis=2).astype(np.uint8)


def sample_video(out_dir: Path, dur: float = SAMPLE_DUR + 0.5) -> Path:
    """A synthetic 1920x1080 30 fps background (background_frame, quiet brown noise): no third-party media."""
    out = Path(out_dir) / "sample_background.mp4"
    if out.is_file():
        return out
    raw = Path(out_dir) / "sample_background.rgb"
    raw.write_bytes(background_frame().tobytes())
    video = ["-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", "1920x1080", "-framerate", "30"]
    audio = ["-f", "lavfi", "-i", f"anoisesrc=color=brown:amplitude=0.2:sample_rate=48000:d={dur}"]
    enc = ["-t", str(dur), "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)]
    try:
        run_cmd(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1"]
            + video
            + ["-i", str(raw)]
            + audio
            + enc,
            "synthetic sample background",
        )
    finally:
        raw.unlink(missing_ok=True)
    return out


def capture_time(plan: dict, style: str, pick: int) -> float:
    """When the tile is taken: the picked word's caption once its effect shows, or the hook banner."""
    caps = [c for c in plan.get("captions") or [] if c.get("layer") != 1 and not c.get("capcut_only")]
    if not caps:
        return HOOK_T
    c = caps[min(pick, len(caps) - 1)]
    s, e = float(c["tl_start"]), float(c["tl_end"])
    delay = GLITCH_S if STYLES[style].anim == "glitch" else SETTLE_S
    return round(min(s + delay, (s + e) / 2), 3)


def _frame(path: Path, t: float, w: int, h: int):
    """RGB frame of a video at t as an (h, w, 3) int16 array, or None when it is not w x h."""
    res = run_cmd(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path)]
        + ["-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        "gallery frame",
        binary=True,
    )
    if len(res.stdout) != w * h * 3:
        return None
    return np.frombuffer(res.stdout, np.uint8).reshape(h, w, 3).astype(np.int16)


def text_box(mp4: Path, plan: dict, t: float, canvas: list):
    """
    (x0, y0, x1, y1) of the captions on the MP4 frame at t: where it differs from the caption-free main
    panel at the same moment. None when that cannot be measured (other panel size, frames not aligned).
    """
    W, H = canvas
    seg = (plan.get("segments") or [{}])[0]
    panel = (plan.get("panels") or {}).get("main_src")
    if not panel:
        return None
    a = _frame(mp4, t, W, H)
    b = _frame(Path(panel), float(seg.get("src_s", 0.0)) + t - float(seg.get("tl_start", 0.0)), W, H)
    if a is None or b is None:
        return None
    mask = np.abs(a - b).max(axis=2) > 60
    rows, cols = np.nonzero(mask.sum(axis=1) >= 3)[0], np.nonzero(mask.sum(axis=0) >= 3)[0]
    if not len(rows) or not len(cols):
        return None
    box = (int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1)
    if (box[2] - box[0]) * (box[3] - box[1]) > 0.5 * W * H:
        return None  # the whole picture changed: not just captions
    return box


def caption_crop(spans: list, t: float, canvas: list, box=None) -> tuple:
    """
    (w, h, x, y) of the canvas around the captions at t, TILE_W:TILE_H, as tight as the text allows so it
    reads large in the tile. box: the measured text box (text_box); else the sidecar's estimated spans.
    """
    W, H = canvas
    aspect = TILE_W / TILE_H
    vis = [s for s in spans if s[0] <= t < s[1]]
    if box:
        x0, y0, x1, y1 = box
        if x1 - x0 >= 0.9 * W:  # a full-width bar: frame its text, the bar still runs edge to edge
            ch = max(400.0, (y1 - y0) * 2.2)
        else:
            ch = max(300.0, (y1 - y0) * 1.7, (x1 - x0) * 1.15 / aspect)
    else:
        if vis:
            x0, y0 = min(s[2] for s in vis), min(s[3] for s in vis)
            x1, y1 = max(s[4] for s in vis), max(s[5] for s in vis)
        else:
            x0, y0, x1, y1 = 0, H * 0.7, W, H * 0.8
        # span widths are an estimate (0.6 em per letter): wide fonts need the extra room
        ch = max(300.0, (y1 - y0) * 1.8, (x1 - x0) * 1.35 / aspect)
    ch = min(ch, W / aspect, H)
    cw = ch * aspect
    x = min(max(0.0, (x0 + x1 - cw) / 2), W - cw)
    y = min(max(0.0, (y0 + y1 - ch) / 2), H - ch)
    return tuple(int(round(v / 2)) * 2 for v in (cw, ch, x, y))


def _tile(mp4: Path, t: float, crop: tuple, label: str, out: Path) -> None:
    """One gallery tile: the caption area of the frame at t, TILE_W x TILE_H, with the style name under it."""
    cw, ch, x, y = crop
    vf = (
        f"crop={cw}:{ch}:{x}:{y},scale={TILE_W}:{TILE_H}:flags=lanczos,"
        f"pad={TILE_W}:{TILE_H + LABEL_H}:0:0:color={LABEL_BG},"
        f"drawtext=fontfile=Montserrat-Bold.ttf:text='{label}':fontsize=22:fontcolor=white:"
        f"x=(w-text_w)/2:y={TILE_H}+({LABEL_H}-text_h)/2"
    )
    run_cmd(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(t), "-i", str(mp4)]
        + ["-frames:v", "1", "-vf", vf, str(out)],
        f"gallery tile {label}",
        cwd=TEMPLATE_DIR / "fonts",  # plain font file name: no path escaping inside the filter
    )


def make_gallery(input_video: str = "", out_dir: str = "style_previews") -> Path:
    """
    Render <out>/style_<name>.mp4 + .png for every caption style and hook style through the normal
    clip-spec path (export gate included), then <out>/styles_gallery.jpg: one tile per caption style
    (caption only, cropped to the caption) and one per hook style (hook banner only). Returns the grid path.
    """
    from streamclipper.commands import build_high_level_clip  # late import: commands imports this module's users

    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    src = Path(input_video).resolve() if input_video else sample_video(out)
    if not src.is_file():
        raise ToolError(f"Video not found: {src}", "check the --input path, or leave it out for the synthetic sample")
    dur = min(SAMPLE_DUR, probe_video(src)["dur"])
    # (label, caption style, hook style, words, hook text, word to capture)
    jobs = []
    for name in STYLES:
        text, pick = STYLE_SAMPLES.get(name, (SAMPLE_TEXT, 1))
        jobs.append((name, name, DEFAULT_HOOK_STYLE, sample_words(dur, text), "", pick))
    for h in HOOK_STYLES:
        jobs.append((f"{h} hook", DEFAULT_STYLE, h, [], HOOK_SAMPLES.get(h, "YOUR HOOK HERE"), 0))
    args = argparse.Namespace(
        no_draft=True,
        render=False,
        out=str(out),
        overwrite=True,
        close_capcut=False,
        draft_root="",
        portable_drafts=False,
    )
    results, tiles = [], []
    tdir = out / "tiles"
    tdir.mkdir(exist_ok=True)
    for old in tdir.glob("*.png"):
        old.unlink()
    for i, (label, cap, hook, words, hook_text, pick) in enumerate(jobs, start=1):
        name = "style_" + label.replace(" ", "_")
        spec = {
            "draft_name": name,
            "source_video": str(src),
            "start": 0,
            "end": dur,
            "layout": "full",
            "sfx": False,
            "trim_silence": False,
            "caption_style": cap,
            "hook_style": hook,
            "hook_headline": hook_text,
            "words": words,
        }
        find_triggers = capcut.find_triggers
        capcut.find_triggers = lambda *_a, **_k: []  # no red hype words: each style shows its own colours
        try:
            build_high_level_clip(spec, out, args)
        finally:
            capcut.find_triggers = find_triggers
        plan = json.loads((out / f"{name}.clip.json").read_text(encoding="utf-8"))
        mp4 = out / f"{name}.mp4"
        results.append((name, render_and_gate(plan, mp4, black_background=not input_video)))
        side = json.loads(mp4.with_name(f"{mp4.stem}.timeline.json").read_text(encoding="utf-8"))
        t = capture_time(plan, cap, pick) if words else HOOK_T
        run_cmd(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(t), "-i", str(mp4)]
            + ["-frames:v", "1", str(out / f"{name}.png")],
            f"preview frame of {name}",
        )
        tile = tdir / f"{i:02d}.png"
        box = text_box(mp4, plan, t, side["canvas"])
        _tile(mp4, t, caption_crop(side["caption"].get("spans") or [], t, side["canvas"], box), label, tile)
        tiles.append(tile)
    rows = -(-len(tiles) // GRID_COLS)
    grid = out / "styles_gallery.jpg"
    run_cmd(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-framerate", "1", "-i", str(tdir / "%02d.png")]
        + ["-vf", f"tile={GRID_COLS}x{rows}:padding={GAP}:margin={GAP}:color={PAGE_BG}", "-frames:v", "1"]
        + ["-q:v", "2", str(grid)],  # JPEG: smaller than PNG for the text edges
        "styles gallery grid",
    )
    print(f"Style gallery -> {fwd(grid)} ({len(tiles)} samples; MP4 + PNG per style in {fwd(out)})")
    _gate_summary(results)
    return grid
