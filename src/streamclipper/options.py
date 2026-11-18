"""Presets and option resolution (CLI > spec > preset > defaults)."""

import json
from pathlib import Path

from streamclipper.brand import load_brand_kit, nearest_colour
from streamclipper.captions.styles import (
    AUTO_STYLE,
    CAPTION_CASES,
    CAPTION_FONTS,
    CAPTION_POSITIONS,
    DEFAULT_HOOK_STYLE,
    HIGHLIGHT_COLOURS,
    HOOK_STYLES,
    STYLE_ALIASES,
    STYLES,
    style_name,
)
from streamclipper.effects import EFFECT_DEFAULTS, check_effects
from streamclipper.layout import black_spans, detect_player_and_webcam, gameplay_box_avoiding
from streamclipper.paths import PRESET_DIR
from streamclipper.util import SkipClip, ToolError, box_to_norm, parse_box, parse_time
from streamclipper.vision import classify_layout, face_crop_region, person_box

PRESET_ALIASES = {"gta6-streamer-9x16": "streamer-split-9x16", "hormozi-caption-punch": "caption-punch-9x16"}


OPT_CHOICES = {
    "ratio": ("9:16", "1:1"),
    "layout": ("split", "hook-grabber", "full", "square-frame", "speaker"),
    "hook_mode": ("tease-climax", "story-stitch", "detail-zoom", "split-shock", "uncut-16x9-stack"),
    "caption_mode": ("hormozi-pop", "two-line-stack", "karaoke-word"),
    "font": CAPTION_FONTS,
    "hindi_script": ("devanagari", "roman"),
    "caption_case": ("",) + CAPTION_CASES,
    "caption_position": CAPTION_POSITIONS,
    "highlight_color": ("",) + HIGHLIGHT_COLOURS,
    "caption_font": ("",) + CAPTION_FONTS,
    "hook_style": tuple(HOOK_STYLES),
}

# Caption options: CLI flag > spec > preset > default ('' / None = the caption style's own choice).
CAPTION_DEFAULTS = {
    "caption_case": "",
    "words_per_caption": 0,
    "caption_position": "auto",
    "highlight_color": "",
    "caption_font": "",
    "emoji": None,
    "profanity_filter": False,
    "hook_style": DEFAULT_HOOK_STYLE,
}

_LEGACY_CAPTION_MODES = {"hormozi-pop": "bold-pop", "karaoke-word": "karaoke-highlight"}


DEFAULT_OPTS = {
    "ratio": "9:16",
    "layout": "split",
    "main_ratio": 0.55,
    "hook_mode": "tease-climax",
    "caption_mode": "hormozi-pop",
    "font": "barlow",
    "sfx": True,
    "filler_video": "",
    "cam_crop": None,
    "game_crop": None,
    "hindi_script": "devanagari",
}


GLOW_ALIASES = {
    "glowing_neon": "neon",
    "deep_glow": "deep",
    "glowing_rays": "rays",
    "neon": "neon",
    "deep": "deep",
    "rays": "rays",
}


def load_preset(name: str) -> dict:
    if not name:
        return {}
    key = PRESET_ALIASES.get(name, name)
    p = PRESET_DIR / f"{key}.json"
    if not p.is_file():
        avail = sorted(x.stem for x in PRESET_DIR.glob("*.json")) + sorted(PRESET_ALIASES)
        raise ToolError(f"Unknown preset '{name}'.", "available presets: " + ", ".join(avail))
    return json.loads(p.read_text(encoding="utf-8"))


def resolve_options(args, spec: dict = None) -> dict:
    """Precedence: explicit CLI flag > spec key > preset > built-in default."""
    spec = spec or {}
    preset_name = getattr(args, "preset", None) or spec.get("preset")
    preset = load_preset(preset_name)
    opts = dict(DEFAULT_OPTS)
    for src in (preset, spec):
        for k in DEFAULT_OPTS:
            if src.get(k) not in (None, ""):
                opts[k] = src[k]
    for k in ("ratio", "hook_mode", "caption_mode", "font", "cam_crop", "game_crop", "hindi_script"):
        v = getattr(args, k, None)
        if v not in (None, ""):
            opts[k] = v
    cli_filler = getattr(args, "filler_video", None)
    if cli_filler:
        opts["filler_video"] = str(Path(cli_filler).resolve())
    _resolve_caption_options(opts, args, spec, preset)
    for k, allowed in OPT_CHOICES.items():
        if opts[k] not in allowed:
            raise ToolError(f"Bad {k} '{opts[k]}'.", f"use one of: {', '.join(a for a in allowed if a)}")
    opts["cam_crop"] = parse_box(opts["cam_crop"])
    opts["game_crop"] = parse_box(opts["game_crop"])
    opts["preset"] = preset.get("name", preset_name or "")
    # --content > spec > preset > gaming (shapes clip selection; the podcast preset sets talk)
    opts["content"] = getattr(args, "content", None) or spec.get("content") or preset.get("content") or "gaming"
    needs_filler = opts["layout"] == "hook-grabber" or "filler_video" in preset.get("requires", [])
    if needs_filler and not opts["filler_video"]:
        raise ToolError(
            f"Preset '{opts['preset'] or opts['layout']}' needs a filler video for the bottom panel.",
            'pass --filler-video <path> (or set "filler_video" in the spec)',
        )
    if opts["filler_video"] and not Path(opts["filler_video"]).is_file():
        raise ToolError(f"Filler video not found: {opts['filler_video']}", "check the --filler-video path")
    # the render-time LLM (--llm): used for the guarded caption fix, never for selection here
    opts["llm"] = getattr(args, "llm", None) or spec.get("llm") or "none"
    opts["llm_model"] = getattr(args, "llm_model", None) or spec.get("llm_model") or ""
    opts["language"] = getattr(args, "language", None) or spec.get("language") or "auto"
    _resolve_effects(opts, args, spec)
    return opts


def _resolve_effects(opts: dict, args, spec: dict) -> None:
    """opts['effects'] (zoom, remove_fillers, enhance_speech, brand_kit; CLI flag > spec > off) and opts['brand']
    (the loaded brand kit). A brand kit colour sets the highlight colour unless one was chosen."""
    raw = {}
    for k in EFFECT_DEFAULTS:
        if spec.get(k) not in (None, ""):
            raw[k] = spec[k]
        cli = getattr(args, k, None)
        if cli not in (None, ""):
            raw[k] = str(Path(cli).resolve()) if k == "brand_kit" else cli
    eff = check_effects(raw)
    opts["brand"] = None
    if eff["brand_kit"]:
        kit = load_brand_kit(eff["brand_kit"])
        eff["brand_kit"], eff["brand_sha1"] = kit["path"], kit["sha1"]
        opts["brand"] = kit
        if kit["colours"].get("highlight") and not opts.get("highlight_color"):
            opts["highlight_color"] = nearest_colour(kit["colours"]["highlight"])
    opts["effects"] = eff


def _resolve_caption_options(opts: dict, args, spec: dict, preset: dict) -> None:
    """caption_style (old caption_mode values are aliases) and the caption/hook options, in precedence order."""
    style = ""
    for src in (
        {"caption_style": getattr(args, "caption_style", None), "caption_mode": getattr(args, "caption_mode", None)},
        spec,
        preset,
    ):
        raw = src.get("caption_style") or src.get("caption_mode")
        if raw and str(raw).strip().lower() == AUTO_STYLE:
            style = AUTO_STYLE
            break
        if raw:
            style = style_name(_LEGACY_CAPTION_MODES.get(raw, raw))
            if not style:
                names = ", ".join([AUTO_STYLE] + list(STYLES) + sorted(STYLE_ALIASES))
                raise ToolError(f"Unknown caption style '{raw}'.", f"use one of: {names} (see 'streamclipper styles')")
            break
    opts["caption_style"] = style or AUTO_STYLE  # auto = per clip from its category (styles.clip_style)
    for k, default in CAPTION_DEFAULTS.items():
        val = default
        for src in (preset, spec):
            if src.get(k) not in (None, ""):
                val = src[k]
        cli = getattr(args, k, None)
        if cli is not None and cli != "":
            val = cli
        opts[k] = val
    # --caption-font, then the old --font flag; a spec's "font" counts too (presets set fonts per style)
    if not getattr(args, "caption_font", None):
        opts["caption_font"] = getattr(args, "font", None) or opts["caption_font"] or spec.get("font") or ""
    try:
        opts["words_per_caption"] = int(opts["words_per_caption"] or 0)
    except (TypeError, ValueError):
        opts["words_per_caption"] = -1
    if not 0 <= opts["words_per_caption"] <= 4:
        raise ToolError(f"Bad words_per_caption '{opts['words_per_caption']}'.", "use 1, 2, 3 or 4")
    opts["profanity_filter"] = bool(opts["profanity_filter"])
    if opts["emoji"] is not None:
        opts["emoji"] = bool(opts["emoji"])


def clip_boxes(src: Path, meta: dict, opts: dict, stream_layout: dict = None) -> tuple:
    """Normalised webcam and game boxes: --cam-crop/--game-crop override auto detection."""
    vw, vh = meta["width"], meta["height"]
    cam = box_to_norm(opts["cam_crop"], vw, vh) if opts.get("cam_crop") else None
    game = box_to_norm(opts["game_crop"], vw, vh) if opts.get("game_crop") else None
    if cam is None and opts["layout"] in ("split", "uncut-stack", "square-frame"):
        layout = stream_layout or detect_player_and_webcam(src, meta["dur"])
        cam = layout["player_top_crop"]
    return cam, game


CAM_LAYOUTS = ("split", "uncut-stack")


def clip_layout(src: Path, meta: dict, opts: dict, start: float = None, end: float = None) -> tuple:
    """
    Layout for one clip window from the faces in it: (opts for this clip, cam_box, game_box, note).
      corner-cam -> the chosen layout with the detected webcam box;
      full-cam   -> 'full' with a 9:16 (1:1) crop centred on the face;
      no-cam     -> 'full' centre crop of the gameplay.
    A split is never made without a face in its cam panel: a preset's split falls back to 'full' too.
    --cam-crop, portrait sources and the hook-grabber layout skip detection. Without OpenCV the old
    skin-colour corner heuristic is used.
    """
    vw, vh = meta["width"], meta["height"]
    layout = opts["layout"]
    if layout == "speaker":  # podcasts: the panel render tracks the faces shot by shot
        return opts, None, None, "layout: speaker (face tracking per camera shot)"
    if opts.get("cam_crop") or vw < vh or layout == "hook-grabber":
        cam, game = clip_boxes(src, meta, opts)
        return opts, cam, game, f"layout: {layout} ({'--cam-crop' if opts.get('cam_crop') else 'no detection'})"
    s = 0.0 if start is None else start
    e = meta["dur"] if end is None else end
    res = classify_layout(src, s, e, vw, vh)
    if res is None:
        cam, game = clip_boxes(src, meta, opts)
        if cam is None and layout in CAM_LAYOUTS:
            copts = dict(opts, layout="full")
            return copts, None, game, "layout: no-cam -> full (no confident facecam detected)"
        return opts, cam, game, f"layout: {layout} (skin heuristic; pip install -e .[vision] for face detection)"
    game = box_to_norm(opts["game_crop"], vw, vh) if opts.get("game_crop") else None
    kind = res["kind"]
    if kind == "blank":
        raise SkipClip(
            f"{int(res['dark'] * 100)}% of the sampled frames are mostly black (blank or disconnected camera screen)",
            "the clip is skipped; the next clip in clips.json is used",
        )
    if kind == "corner-cam":
        cam = res["cam_box"] if layout in CAM_LAYOUTS + ("square-frame",) else None
        if layout in CAM_LAYOUTS and game is None:
            # the gameplay panel must not show any part of the streamer (a keyed streamer has no cam
            # border: arms and hands reach far past the face), so crop around the whole person
            person = person_box(res["face_box"], res["corner"])
            aspect = 1080 / 1248 if opts["ratio"] == "9:16" else 1080 / 626
            game = gameplay_box_avoiding(person, vw, vh, aspect)
            opts = dict(opts, person_box=person)
            black = black_spans(src, game, vw, vh, s, e) if game else []
            if black:
                # the game screen is black (stream start, loading) while the cam is lit: show the streamer
                copts = dict(opts, layout="full")
                copts.pop("person_box", None)
                game = face_crop_region(res["face_box"], vw, vh, 9 / 16 if opts["ratio"] == "9:16" else 1.0)
                secs = sum(b - a for a, b in black)
                return (
                    copts,
                    None,
                    game,
                    (f"layout: corner-cam ({res['corner']}) -> full (game screen black for {secs:.1f}s)"),
                )
            if game is None:
                copts = dict(opts, layout="full")
                game = face_crop_region(res["face_box"], vw, vh, 9 / 16 if opts["ratio"] == "9:16" else 1.0)
                return (
                    copts,
                    None,
                    game,
                    (f"layout: corner-cam ({res['corner']}) -> full (no gameplay crop clear of the streamer)"),
                )
        return opts, cam, game, f"layout: corner-cam ({res['corner']}) -> {layout}"
    copts = dict(opts)
    if layout in CAM_LAYOUTS or (layout == "full" and not opts.get("preset")):
        copts["layout"] = "full"
    if kind == "full-cam" and copts["layout"] == "full" and not opts.get("game_crop"):
        game = face_crop_region(res["face_box"], vw, vh, 9 / 16 if opts["ratio"] == "9:16" else 1.0)
    detail = "moving face" if res.get("moving") else ("face-centred crop" if kind == "full-cam" else "centre crop")
    return copts, None, game, f"layout: {kind} -> {copts['layout']} ({detail})"


def normalize_clip_spec(spec: dict, base_dir: Path) -> dict:
    """README-style high-level clip spec -> normalised dict (paths absolute, words as {w, s, e})."""

    def _path(v) -> Path:
        p = Path(str(v))
        return (p if p.is_absolute() else (base_dir / p)).resolve()

    if not spec.get("source_video"):
        raise ToolError(
            "The clip spec has no 'source_video'.",
            'add "source_video": "stream.mp4" (or use a low-level spec with project_name + video_track)',
        )
    out = dict(spec)
    out["source_video"] = _path(spec["source_video"])
    if spec.get("filler_video"):
        out["filler_video"] = str(_path(spec["filler_video"]))
    if spec.get("brand_kit"):
        out["brand_kit"] = str(_path(spec["brand_kit"]))
    out["draft_name"] = str(spec.get("draft_name") or f"{out['source_video'].stem}_clip")
    out["start"] = parse_time(spec["start"]) if spec.get("start") not in (None, "") else None
    out["end"] = parse_time(spec["end"]) if spec.get("end") not in (None, "") else None
    words = []
    for w in spec.get("words") or []:
        txt = w.get("word", w.get("w"))
        s = w.get("start", w.get("s"))
        e = w.get("end", w.get("e"))
        if txt is None or s is None or e is None:
            raise ToolError(f"Bad word entry {w}.", 'use {"word": "bro", "start": 1.2, "end": 1.5}')
        words.append({"w": str(txt), "s": float(s), "e": float(e)})
    out["words"] = words
    if spec.get("glow_preset"):
        gp = str(spec["glow_preset"])
        if gp not in GLOW_ALIASES:
            raise ToolError(f"Unknown glow_preset '{gp}'.", "use glowing_neon, deep_glow or glowing_rays")
        out["glow_preset"] = GLOW_ALIASES[gp]
    out["trim_silence"] = bool(spec.get("trim_silence", True))
    # reframe editor fields: checked against the probed source in build_high_level_clip (reframe.validate_*)
    if spec.get("reframe_segments") is not None and not isinstance(spec["reframe_segments"], list):
        raise ToolError("'reframe_segments' must be a list of segments.", "save the edit again in the dashboard editor")
    out["reframe_segments"] = spec.get("reframe_segments") or None
    out["caption_anchor"] = spec.get("caption_anchor") or None
    return out
