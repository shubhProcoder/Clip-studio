"""
Simple clip editor of the dashboard: trim, crop boxes, caption words, styles. An edit is saved as a README-style
clip spec (<job>/edits/<key>/vN.json) and rendered by the normal CLI (`render --spec`, i.e. build_high_level_clip:
the same panels, captions, _2 names and export gate as every other MP4). Nothing here renders by itself.
"""

import json
import math
import re
import time
from pathlib import Path

from streamclipper import env as _env
from streamclipper import reframe
from streamclipper.captions.styles import (
    AUTO_STYLE,
    CAPTION_POSITIONS,
    HIGHLIGHT_COLOURS,
    HOOK_STYLES,
    NEON_PALETTE,
    STYLES,
)
from streamclipper.dashboard import brandkit, library
from streamclipper.dashboard.jobs import parse_bool
from streamclipper.draft.rootmeta import OUTPUT_SUFFIXES, unique_output_name
from streamclipper.effects import ENHANCE_MIX, ZOOM_LEVELS, effects_of
from streamclipper.state import write_json_atomic
from streamclipper.util import ToolError, run_cmd

LAYOUTS = {
    "split": {"layout": "split"},
    "full": {"layout": "full"},
    "square-frame": {"layout": "square-frame"},
    "caption-punch": {"preset": "caption-punch-9x16"},
    "speaker": {"layout": "speaker"},
}
LAYOUT_LABELS = {
    "split": "Split (facecam on top, gameplay below)",
    "full": "Full (one crop fills the frame)",
    "square-frame": "Square frame (gameplay in a frame)",
    "caption-punch": "Caption punch (full crop, big captions)",
    "speaker": "Podcast speaker (follows the face)",
}
PAD_S = 10.0  # the trim timeline shows this much before and after the clip
MIN_DUR, MAX_DUR = 1.0, 59.5
MIN_BOX = reframe.MIN_BOX
ASPECT_TOL = reframe.ASPECT_TOL
THUMB_W = 160  # px width of one frame of the timeline thumbnail strip (one frame per second)
MAX_WORD, MAX_HOOK, MAX_TITLE, MAX_DESC, MAX_TAGS, MAX_TAG = 40, 80, 100, 500, 15, 40
WAVE_STEP = 0.1  # seconds per waveform bar


def aspects(layout: str, ratio: str) -> dict:
    """Locked w/h aspect of the facecam and gameplay crop boxes per layout (None = no box)."""
    if layout == "split":
        return {"cam": 1080 / 672, "game": 1080 / 1248} if ratio == "9:16" else {"cam": 1080 / 454, "game": 1080 / 626}
    if layout in ("full", "caption-punch"):
        return {"cam": None, "game": 1080 / 1920 if ratio == "9:16" else 1.0}
    if layout == "square-frame":
        return {"cam": None, "game": 1080 / 900}
    return {"cam": None, "game": None}


def _xywh(t) -> list:
    return [int(v) for v in t] if t else None


def effective_boxes(layout: str, ratio: str, cam_px, game_px, vw: int, vh: int) -> dict:
    """The crops the render will really use for this layout (the same layout.py functions), as [x, y, w, h]."""
    from streamclipper.layout import _fill_crop, frame_crop, grow_to_aspect

    a = aspects(layout, ratio)
    out = {"cam": None, "game": None, "cam_aspect": a["cam"], "game_aspect": a["game"]}
    if layout == "split":
        cam = tuple(cam_px) if cam_px else (0, 0, int(vw * 0.25) & ~1, int(vh * 0.25) & ~1)
        cx, cy, cw, ch = grow_to_aspect(cam, vw, vh, a["cam"])
        out["cam"] = [cx, cy, cw, ch]
        if game_px:
            gw, gh, gx, gy = _fill_crop(tuple(game_px), vw, vh, a["game"])
        elif cam[1] < vh // 4 and cam[1] + cam[3] < vh // 2:
            top = (cam[1] + cam[3]) & ~1
            gw, gh, gx, gy = _fill_crop((0, top, vw, vh - top), vw, vh, a["game"])
        else:
            gw, gh, gx, gy = frame_crop(vw, vh, None, tuple(cam), a["game"])
        out["game"] = [gx, gy, gw, gh]
    elif layout in ("full", "caption-punch"):
        gw, gh, gx, gy = _fill_crop(tuple(game_px) if game_px else (0, 0, vw, vh), vw, vh, a["game"])
        out["game"] = [gx, gy, gw, gh]
    elif layout == "square-frame":
        gw, gh, gx, gy = frame_crop(
            vw, vh, tuple(game_px) if game_px else None, tuple(cam_px) if cam_px else None, a["game"]
        )
        out["game"] = [gx, gy, gw, gh]
    return out


def _editor_layout(plan_layout: str) -> str:
    lay = str(plan_layout or "split")
    if lay.startswith("speaker"):
        return "speaker"
    if lay in LAYOUTS:
        return lay
    return "split"  # uncut-stack, hook-grabber


def _dirs(store, job_id: str, key: str):
    jdir = store.job_dir(job_id)
    return jdir, jdir / "out", jdir / "edits" / key


def _probe_dur(path: Path, cache: Path = None) -> float:
    """Duration of the source; cached next to the edits (keyed by path, size and mtime) so a reopen needs no ffprobe."""
    try:
        st = Path(path).stat()
        key = f"{path}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        key = ""
    if cache and key:
        try:
            c = json.loads(cache.read_text(encoding="utf-8"))
            if c.get("key") == key:
                return float(c["dur"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    res = run_cmd(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        "editor source probe",
    )
    try:
        dur = float((res.stdout or "0").strip() or 0)
    except ValueError:
        return 0.0
    if cache and key and dur > 0:
        write_json_atomic(cache, {"key": key, "dur": dur})
    return dur


def waveform(video: Path, lo: float, hi: float, cache: Path) -> list:
    """Peak level (0-1) per WAVE_STEP seconds of [lo, hi], from one ffmpeg call; cached next to the edits."""
    key = f"{video}|{lo:.3f}|{hi:.3f}"
    try:
        c = json.loads(cache.read_text(encoding="utf-8"))
        if c.get("key") == key:
            return c["peaks"]
    except (OSError, ValueError, KeyError):
        pass
    import numpy as np

    sr = 8000
    try:
        res = run_cmd(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{lo:.3f}",
                "-t",
                f"{hi - lo:.3f}",
                "-i",
                str(video),
            ]
            + ["-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
            "editor waveform",
            binary=True,
        )
        raw = res.stdout or b""
        pcm = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
    except (ToolError, OSError):
        pcm = np.zeros(0, dtype=np.float32)
    step = int(sr * WAVE_STEP)
    n = max(1, int(math.ceil((hi - lo) / WAVE_STEP)))
    peaks = []
    for i in range(n):
        seg = pcm[i * step : (i + 1) * step]
        peaks.append(round(float(np.abs(seg).max()) if seg.size else 0.0, 3))
    top = max(peaks) or 1.0
    peaks = [round(p / top, 3) for p in peaks]
    write_json_atomic(cache, {"key": key, "peaks": peaks})
    return peaks


def _load_plan(out: Path, key: str) -> dict:
    p = out / f"{key}.clip.json"
    if not p.is_file():
        raise ToolError("This clip has no clip plan to edit.", "edit clips that were made with an MP4 or a draft")
    plan = json.loads(p.read_text(encoding="utf-8"))
    if plan.get("type") != "clip_plan":
        raise ToolError("This clip has no clip plan to edit.", "edit clips that were made with an MP4 or a draft")
    return plan


def _history(edir: Path) -> list:
    try:
        h = json.loads((edir / "history.json").read_text(encoding="utf-8"))
        return h if isinstance(h, list) else []
    except (OSError, ValueError):
        return []


def _inside(p: Path, roots) -> bool:
    try:
        rp = Path(p).resolve()
    except (OSError, ValueError):
        return False
    return any(rp.is_relative_to(Path(r).resolve()) for r in roots)


def lan_roots(store) -> tuple:
    """The folders a --lan user may read videos from: the workspace's uploads and jobs."""
    return (store.ws / "uploads", store.ws / "jobs")


def source_info(plan: dict, roots=None) -> dict:
    """
    The video the editor shows and its time base. The original local stream when it still exists (so the cut
    can grow past the window), else the clip's window file. t_off converts plan window time -> file time.
    roots (--lan mode): only videos inside these folders are used; else the window file, else an error.
    """
    sw = plan.get("source_window") or {}
    win = Path(str(sw.get("path") or ""))
    src = Path(str(plan.get("source") or ""))
    original = (
        not re.match(r"^https?://", str(plan.get("source") or ""), re.I)
        and src.is_file()
        and src.resolve() != win.resolve()
        and (roots is None or _inside(src, roots))
    )
    if original:
        return {"video": src, "t_off": float(sw.get("start") or 0.0), "original": True}
    if not win.is_file():
        raise ToolError("The clip's source video is gone.", "the window file was moved or deleted; run the job again")
    if roots is not None and not _inside(win, roots):
        raise ToolError("The source video is outside the workspace.", "edit this clip on the PC that runs it")
    return {"video": win, "t_off": float(sw.get("offset") or 0.0), "original": False}


def context(store, job_id: str, key: str, lan: bool = False) -> dict:
    library.check_key(key)
    job = store.get(job_id)
    if key in set(job.get("deleted") or []):
        raise ToolError("Unknown clip.", "reload the page")
    _jdir, out, edir = _dirs(store, job_id, key)
    plan = _load_plan(out, key)
    si = source_info(plan, lan_roots(store) if lan else None)
    sw = plan.get("source_window") or {}
    vw, vh = int(sw.get("width") or 1920), int(sw.get("height") or 1080)
    t_off = si["t_off"]
    story = [s for s in plan.get("segments") or [] if s.get("kind") != "tease"] or plan.get("segments") or []
    start = min(float(s["src_s"]) for s in story) + t_off
    end = max(float(s["src_e"]) for s in story) + t_off
    win_lo = float(sw.get("offset") or 0.0) if not si["original"] else t_off
    win_hi = win_lo + float(sw.get("end", 0)) - float(sw.get("start", 0))
    if si["original"]:
        dur = _probe_dur(si["video"], edir / "probe.json")
        lo, hi = max(0.0, start - PAD_S), min(dur or end + PAD_S, end + PAD_S)
    else:
        lo, hi = win_lo, win_hi
    words = [
        {"w": w["w"], "s": round(float(w["s"]) + t_off, 3), "e": round(float(w["e"]) + t_off, 3)}
        for w in plan.get("words") or []
    ]
    ratio = plan.get("ratio") or "9:16"
    boxes = plan.get("boxes_px") or {}
    layouts = {name: effective_boxes(name, ratio, boxes.get("cam"), boxes.get("game"), vw, vh) for name in LAYOUTS}
    copts = plan.get("caption_opts") or {}
    clip = next((c for c in library.list_clips(store, job_id) if c["key"] == key), {})
    layout = _editor_layout(plan.get("layout"))
    orig = {
        "start": round(start, 3),
        "end": round(end, 3),
        "layout": layout,
        "cam": layouts[layout]["cam"],
        "game": layouts[layout]["game"],
        "words": [w["w"] for w in words],
        "caption_style": plan.get("caption_style") or AUTO_STYLE,
        "hook_style": plan.get("hook_style") or "bold-box",
        "hook_text": plan.get("hook_text") or "",
        "caption_position": copts.get("position") or "auto",
        "highlight_color": copts.get("highlight") or "",
        "title": clip.get("title", ""),
        "description": clip.get("description", ""),
        "hashtags": clip.get("hashtags", []),
        **_plan_effects(plan),
    }
    hist = _history(edir)
    current = orig
    statuses = {j["id"]: j["status"] for j in store.list()}
    for h in hist:
        h["status"] = statuses.get(h.get("job"), "done")
    rendered = [h for h in hist if h.get("kind") == "render" and h["status"] == "done"]
    if rendered:
        current = _read_form(edir / rendered[-1]["file"]) or orig
    pending = _read_form(edir / "pending.json")
    for h in hist:
        h["mp4_url"] = library._file_url(job_id, out, f"{h.get('name')}.mp4") if h.get("kind") == "render" else ""
        h["gate"] = library.gate_rows(out, h.get("name") or "") if h.get("kind") == "render" else None
    return {
        "job": job_id,
        "key": key,
        "video_url": f"/api/jobs/{job_id}/clips/{key}/source",
        "original_source": si["original"],
        "width": vw,
        "height": vh,
        "ratio": ratio,
        "bounds": [round(lo, 3), round(hi, 3)],
        "window": [round(win_lo, 3), round(win_hi, 3)],
        "min_dur": MIN_DUR,
        "max_dur": MAX_DUR,
        "min_box": MIN_BOX,
        "words": words,
        "wave": {"lo": round(lo, 3), "step": WAVE_STEP, "peaks": waveform(si["video"], lo, hi, edir / "wave.json")},
        "layouts": layouts,
        "cam_px": _xywh(boxes.get("cam")),
        "layout_labels": LAYOUT_LABELS,
        "canvas": list(reframe.canvas(ratio)),
        "aspects": {name: reframe.window_aspects(name, ratio) for name in LAYOUTS},
        "panel_rects": {name: [list(r) for r in reframe.panel_rects(name, ratio)] for name in LAYOUTS},
        "limits": {
            "min_seg": reframe.MIN_SEG,
            "max_segs": reframe.MAX_SEGS,
            "max_keys": reframe.MAX_KEYS,
            "smooth_s": reframe.SMOOTH_S,
            "fps": reframe.FPS,
            "anchor_y": [200, 1600] if ratio == "9:16" else [100, 980],
        },
        "thumbs": {
            "url": f"/api/jobs/{job_id}/clips/{key}/thumbs",
            "lo": round(lo, 3),
            "step": 1.0,
            "n": _thumb_count(lo, hi),
            "w": THUMB_W,
            "h": max(2, int(round(THUMB_W * vh / max(1, vw) / 2)) * 2),
        },
        "faces": _cached_faces(edir),
        "caption_styles": [AUTO_STYLE, *STYLES],
        "style_info": {
            n: {
                "words": s.words,
                "primary": NEON_PALETTE[s.primary][1],
                "active": NEON_PALETTE[s.active][1],
                "box": s.box,
                "case": s.case,
                "size": s.size,
                "anim_up": s.anim_up,
            }
            for n, s in STYLES.items()
        },
        "hook_zones": _hook_zones(ratio),
        "palette": {k: v[1] for k, v in NEON_PALETTE.items()},
        "hook_styles": list(HOOK_STYLES),
        "positions": list(CAPTION_POSITIONS),
        "highlights": ["", *HIGHLIGHT_COLOURS],
        "effect_choices": {"zoom": list(ZOOM_LEVELS), "enhance_speech": list(ENHANCE_MIX)},
        "brand_kit_ready": brandkit.kit_path(store.ws).is_file(),
        "original": orig,
        "current": current,
        "pending": pending,
        "history": hist,
        "mp4_url": clip.get("mp4_url", ""),
        "gate": clip.get("gate"),
    }


def _plan_effects(plan: dict) -> dict:
    """The editor form's effect fields from a clip plan (all off for plans before 5.3)."""
    eff = effects_of(plan)
    return {
        "zoom": eff["zoom"],
        "remove_fillers": eff["remove_fillers"],
        "enhance_speech": eff["enhance_speech"],
        "brand_kit": bool(eff["brand_kit"]),
    }


def _hook_zones(ratio: str) -> dict:
    """Per editor layout: the render's hook banner anchor (layout.layout_boxes), the square frame rect and the
    reframe panel's auto caption anchor, so the preview places the hook and moves the caption as build_ass does."""
    from streamclipper.layout import layout_boxes

    out = {}
    for name in [*LAYOUTS, "full"]:
        try:
            lay = layout_boxes(ratio, name)
        except ToolError:
            lay = layout_boxes(ratio, "full")
        out[name] = {
            "hook": lay["hook"],
            "frame": (lay.get("rects") or {}).get("frame"),
            "auto_anchor": reframe.auto_anchor(name, ratio),
        }
    return out


def _thumb_count(lo: float, hi: float) -> int:
    return max(1, int(math.ceil(hi - lo - 1e-6)))


def _cached_faces(edir: Path) -> list:
    try:
        return json.loads((edir / "reframe.json").read_text(encoding="utf-8")).get("faces") or []
    except (OSError, ValueError, AttributeError):
        return []


def thumbs(store, job_id: str, key: str, lan: bool = False) -> Path:
    """
    Thumbnail strip of the editor timeline: one THUMB_W px frame per second of the bounds, in one row, made by
    one ffmpeg call (fps=1, scale, tile) and cached in edits/<key>/thumbs.jpg (keyed by video, size, mtime, bounds).
    """
    ctx = context(store, job_id, key, lan)
    _jdir, out, edir = _dirs(store, job_id, key)
    video = source_info(_load_plan(out, key), lan_roots(store) if lan else None)["video"]
    lo, hi = ctx["bounds"]
    n = _thumb_count(lo, hi)
    jpg, meta = edir / "thumbs.jpg", edir / "thumbs.json"
    try:
        st = Path(video).stat()
        ckey = f"{video}|{st.st_size}|{st.st_mtime_ns}|{lo:.3f}|{hi:.3f}|{n}|{THUMB_W}"
    except OSError:
        raise ToolError("The clip's source video is gone.", "run the job again") from None
    try:
        if jpg.is_file() and json.loads(meta.read_text(encoding="utf-8")).get("key") == ckey:
            return jpg
    except (OSError, ValueError, AttributeError):
        pass
    edir.mkdir(parents=True, exist_ok=True)
    tmp = edir / "thumbs.part.jpg"
    run_cmd(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{lo:.3f}", "-t", f"{hi - lo:.3f}"]
        + ["-i", str(video), "-an", "-vf", f"fps=1,scale={THUMB_W}:-2,tile={n}x1", "-frames:v", "1"]
        + ["-q:v", "6", str(tmp)],
        "editor thumbnails",
    )
    if not tmp.is_file():
        raise ToolError("Could not make the timeline thumbnails.", "the source video may be damaged")
    tmp.replace(jpg)
    write_json_atomic(meta, {"key": ckey})
    return jpg


def auto_reframe(ctx: dict, form: dict, video: Path, edir: Path) -> dict:
    """
    Auto-reframe of an edit (runs on a background thread of the dashboard): faces every 0.5 s over the cut
    (vision.track_faces), then reframe.propose_keys. Result {segments, faces, note}, cached in edits/<key>/reframe.json.
    """
    from streamclipper import vision

    segs = reframe.normalize_segments(form)
    vw, vh, ratio = ctx["width"], ctx["height"], ctx["ratio"]
    full_game = ctx["layouts"]["full"]["game"]
    for s in segs:  # an old speaker form has no window: start from the full layout's crop
        for which, aspect in reframe.window_aspects(s["layout"], ratio).items():
            if not (s.get("windows") or {}).get(which):
                s.setdefault("windows", {})[which] = reframe.lock_aspect(full_game, aspect, vw, vh)
    track = vision.track_faces(video, form["start"], form["end"], 0.5, vw, vh)
    if track is None:
        raise ToolError(
            "Auto-reframe needs face detection (OpenCV).",
            'install the vision extra: pip install "streamclipper[vision]"',
        )
    from streamclipper.faces import speech_spans

    out, note = reframe.propose_keys(
        segs,
        track["times"],
        track["faces"],
        vw,
        vh,
        ratio,
        ctx.get("cam_px"),
        ctx["layouts"]["split"]["game"],
        track=track,
        spans=speech_spans(ctx["words"]) if ctx.get("words") else None,  # switch only while someone talks
    )
    faces = [
        {"t": t, "box": [round(f[0][0] * vw), round(f[0][1] * vh), round(f[0][2] * vw), round(f[0][3] * vh)]}
        for t, f in zip(track["times"], track["faces"], strict=False)
        if f
    ]
    res = {"segments": out, "faces": faces, "note": note}
    edir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(edir / "reframe.json", res)
    return res


def _read_form(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("editor_form")
    except (OSError, ValueError, AttributeError):
        return None


def _num(v, what: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ToolError(f"{what} must be a number.", "move the handle again") from None
    if not math.isfinite(f):
        raise ToolError(f"{what} must be a number.", "move the handle again")
    return f


_box = reframe.check_box  # the crop box rules live in reframe.py (shared with the segment checks)


def _text(v, what: str, limit: int, allow_newline: bool = False) -> str:
    s = "" if v is None else str(v)
    if not allow_newline and ("\n" in s or "\r" in s):
        raise ToolError(f"The {what} must be one line.", "remove the line break")
    if len(s) > limit:
        raise ToolError(f"The {what} is too long ({len(s)} characters).", f"use at most {limit} characters")
    return s


def validate(ctx: dict, edit: dict) -> dict:
    """Every value of an edit, checked against the clip's source; a clear ToolError for anything wrong."""
    if not isinstance(edit, dict):
        raise ToolError("Nothing to save.", "change something first")
    lo, hi = ctx["bounds"]
    start, end = _num(edit.get("start"), "The start"), _num(edit.get("end"), "The end")
    if start < lo - 0.05 or end > hi + 0.05 or start >= end:
        raise ToolError(f"The cut must lie between {lo:.1f} s and {hi:.1f} s.", "move the handles inside the timeline")
    start, end = max(lo, start), min(hi, end)
    if end - start < MIN_DUR:
        raise ToolError("The clip is too short.", f"keep at least {MIN_DUR:g} s")
    if end - start > MAX_DUR:
        raise ToolError(f"The clip is {end - start:.1f} s long.", f"keep it at most {MAX_DUR:g} s (Shorts/Reels limit)")
    ratio = ctx["ratio"]
    segs_in, segs = edit.get("segments"), None
    if reframe.uses_reframe(segs_in):
        # 2+ segments or keyframes: the reframe render; the first segment stands in for the old fields
        segs = reframe.validate_segments(segs_in, ctx, start, end)
        layout, cam, game = segs[0]["layout"], segs[0]["windows"]["cam"], segs[0]["windows"]["game"]
    else:
        if isinstance(segs_in, list) and len(segs_in) == 1 and isinstance(segs_in[0], dict):
            w0 = segs_in[0].get("windows") if isinstance(segs_in[0].get("windows"), dict) else {}
            edit = dict(edit, layout=segs_in[0].get("layout"), cam=w0.get("cam"), game=w0.get("game"))
        layout = edit.get("layout")
        if layout not in LAYOUTS:
            raise ToolError("Unknown layout.", "pick one from the list")
        if layout in ("square-frame", "caption-punch") and ratio != "9:16":
            raise ToolError("This layout is 9:16 only.", "pick split or full for a 1:1 clip")
        if layout == "speaker":
            w0, w1 = ctx["window"]
            if start < w0 - 0.05 or end > w1 + 0.05:
                raise ToolError(
                    f"The speaker layout can only use the clip's window ({w0:.1f}-{w1:.1f} s).",
                    "move the handles inside it",
                )
        a = aspects(layout, ratio)
        vw, vh = ctx["width"], ctx["height"]
        cam = _box(edit.get("cam"), "facecam", vw, vh, a["cam"]) if a["cam"] else None
        game = _box(edit.get("game"), "gameplay", vw, vh, a["game"]) if a["game"] else None
    anchor = reframe.validate_anchor(edit.get("caption_anchor"), ratio)
    words = edit.get("words")
    if not isinstance(words, list) or len(words) != len(ctx["words"]):
        raise ToolError("The caption words do not match the clip.", "reload the editor")
    texts = []
    for i, w in enumerate(words):
        s = _text(w, "caption word", MAX_WORD).strip()
        if not s:
            raise ToolError(f"Caption word {i + 1} is empty.", "type the word, or put back the old one")
        texts.append(s)
    cs, hs = edit.get("caption_style") or AUTO_STYLE, edit.get("hook_style") or "bold-box"
    if cs not in (AUTO_STYLE, *STYLES):
        raise ToolError("Unknown caption style.", "pick one from the list")
    if hs not in HOOK_STYLES:
        raise ToolError("Unknown hook style.", "pick one from the list")
    pos, hl = edit.get("caption_position") or "auto", edit.get("highlight_color") or ""
    if pos not in CAPTION_POSITIONS:
        raise ToolError("Unknown caption position.", "pick one from the list")
    if hl not in ("", *HIGHLIGHT_COLOURS):
        raise ToolError("Unknown highlight colour.", "pick one from the list")
    zoom, enh = edit.get("zoom") or "off", edit.get("enhance_speech") or "off"
    if zoom not in ZOOM_LEVELS:
        raise ToolError("Unknown zoom level.", "pick one from the list")
    if enh not in ENHANCE_MIX:
        raise ToolError("Unknown speech enhancement.", "pick one from the list")
    fillers = parse_bool(edit.get("remove_fillers"), "remove_fillers", False)
    brand_on = parse_bool(edit.get("brand_kit"), "brand_kit", False)
    tags = edit.get("hashtags") or []
    if isinstance(tags, str):
        tags = tags.split()
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        raise ToolError("Too many hashtags.", f"use at most {MAX_TAGS}")
    clean_tags = []
    for t in tags:
        t = _text(t, "hashtag", MAX_TAG).strip()
        if t:
            clean_tags.append(t if t.startswith("#") else "#" + t)
    out = {
        "start": round(start, 3),
        "end": round(end, 3),
        "layout": layout,
        "cam": cam,
        "game": game,
        "words": texts,
        "caption_style": cs,
        "hook_style": hs,
        "hook_text": _text(edit.get("hook_text"), "hook headline", MAX_HOOK, allow_newline=True).strip(),
        "caption_position": pos,
        "highlight_color": hl,
        "title": _text(edit.get("title"), "title", MAX_TITLE).strip(),
        "description": _text(edit.get("description"), "description", MAX_DESC, allow_newline=True).strip(),
        "hashtags": clean_tags,
        "caption_anchor": anchor,
        "zoom": zoom,
        "remove_fillers": fillers,
        "enhance_speech": enh,
        "brand_kit": brand_on,
    }
    if segs:
        out["segments"] = segs
    return out


def clip_spec(ctx: dict, form: dict, plan: dict, video: Path, base_name: str, brand_kit: Path = None) -> dict:
    """README-style clip spec for build_high_level_clip (render --spec / build --spec). brand_kit: the
    workspace's brand kit JSON, used when the form turns it on."""
    words = [
        {"word": text, "start": w["s"], "end": w["e"]} for text, w in zip(form["words"], ctx["words"], strict=True)
    ]
    spec = {
        "draft_name": base_name,
        "source_video": str(video),
        "start": form["start"],
        "end": form["end"],
        "ratio": ctx["ratio"],
        "trim_silence": False,
        "caption_style": form["caption_style"],
        "hook_style": form["hook_style"],
        "hook_headline": form["hook_text"],
        "caption_position": form["caption_position"],
        "words": words,
        "editor_form": form,
    }
    for k in ("zoom", "enhance_speech"):
        if (form.get(k) or "off") != "off":
            spec[k] = form[k]
    if form.get("remove_fillers"):
        spec["remove_fillers"] = True
    if form.get("brand_kit") and brand_kit:
        spec["brand_kit"] = str(brand_kit)
    if form["highlight_color"]:
        spec["highlight_color"] = form["highlight_color"]
    if form.get("caption_anchor"):
        spec["caption_anchor"] = form["caption_anchor"]
        spec["caption_position"] = "auto"  # a dragged caption wins over the position list
    if form.get("segments"):
        # reframe render (layout.render_reframe_panel): one composite full-canvas panel; the first segment's
        # gameplay window is the plan's game box
        spec["layout"] = "full"
        spec["game_crop"] = ",".join(str(v) for v in form["segments"][0]["windows"]["game"])
        spec["reframe_segments"] = form["segments"]
        return spec
    spec.update(LAYOUTS[form["layout"]])
    if form["game"]:
        spec["game_crop"] = ",".join(str(v) for v in form["game"])
    if form["cam"]:
        spec["cam_crop"] = ",".join(str(v) for v in form["cam"])
    elif form["layout"] == "square-frame":
        spec["cam_crop"] = spec["game_crop"]  # a cam inside the frame: frame_crop keeps the chosen crop as is
    if form["layout"] == "speaker" and ctx["original_source"]:
        win = (plan.get("source_window") or {}).get("path")
        if win and Path(win).is_file():  # the speaker panel renders its whole input: use the short window file
            shift = float((plan.get("source_window") or {}).get("start") or 0.0) - float(
                (plan.get("source_window") or {}).get("offset") or 0.0
            )
            spec["source_video"] = str(Path(win))
            spec["start"], spec["end"] = round(form["start"] - shift, 3), round(form["end"] - shift, 3)
            spec["words"] = [dict(w, start=round(w["start"] - shift, 3), end=round(w["end"] - shift, 3)) for w in words]
    return spec


def _save_copy(store, job_id: str, key: str, form: dict) -> None:
    """Title, description and hashtags go straight into clips.json (+ clips.csv, index.html)."""
    from streamclipper.commands import save_clips

    out = store.job_dir(job_id) / "out"
    doc = library.load_doc(out)
    changed = False
    for c in doc.get("clips") or []:
        if library.clip_key(c) == key:
            for k in ("title", "description", "hashtags"):
                if c.get(k) != form[k]:
                    c[k] = form[k]
                    changed = True
    if changed:
        save_clips(out / "clips.json", doc)


def apply(
    store, runner, job_id: str, key: str, action: str, edit: dict, draft_root: str = "", lan: bool = False
) -> dict:
    """save | render | draft | undo. render/draft write vN.json and queue the normal CLI on it."""
    library.check_key(key)
    parent = store.get(job_id)
    if parent.get("status") in ("running", "queued"):
        raise ToolError("This job is still running.", "wait until it has finished, then edit its clips")
    _jdir, out, edir = _dirs(store, job_id, key)
    if action == "undo":
        (edir / "pending.json").unlink(missing_ok=True)
        return {"current": context(store, job_id, key, lan)["current"]}
    if action not in ("save", "render", "draft"):
        raise ToolError("Unknown editor action.", "use Save, Render or Save as new draft")
    ctx = context(store, job_id, key, lan)
    form = validate(ctx, edit)
    plan = _load_plan(out, key)
    si = source_info(plan, lan_roots(store) if lan else None)
    _save_copy(store, job_id, key, form)
    edir.mkdir(parents=True, exist_ok=True)
    if action == "save":
        write_json_atomic(edir / "pending.json", {"editor_form": form})
        return {"saved": True}
    if action == "draft" and _env.capcut_running():
        raise library.CapCutOpen(library.CAPCUT_OPEN_MSG, "save your work in CapCut and close it")
    base = plan.get("name") or key
    hist = _history(edir)
    n = max([int(h.get("v") or 0) for h in hist] + [0]) + 1
    spec_path = edir / f"v{n}.json"
    kit = brandkit.kit_path(store.ws)
    if form.get("brand_kit") and not kit.is_file():
        raise ToolError("No brand kit yet.", "set it up in Settings > Brand kit, or turn Brand kit off")
    write_json_atomic(spec_path, clip_spec(ctx, form, plan, si["video"], base, kit))
    reserved = {v["name"] for vs in (parent.get("variants") or {}).values() for v in vs}
    name, k = base, 1
    while name in reserved or unique_output_name(out, name, suffixes=OUTPUT_SUFFIXES) != name:
        k += 1
        name = f"{base}_{k}"
    if action == "render":
        argv = ["render", "--spec", str(spec_path), "--out", str(out)]
        label = f"Edit {key} (v{n}): render"
    else:
        argv = ["build", "--spec", str(spec_path), "--out", str(out)]
        if draft_root:
            argv += ["--draft-root", str(draft_root)]
        label = f"Edit {key} (v{n}): CapCut draft"
    job = store.create(
        "render" if action == "render" else "draft", label, argv, {"clip": key, "edit": n}, parent=job_id
    )
    entry = {"v": n, "kind": action, "file": spec_path.name, "job": job["id"], "name": name, "created": time.time()}
    write_json_atomic(edir / "history.json", hist + [entry])
    (edir / "pending.json").unlink(missing_ok=True)
    if action == "render":
        variant = {
            "name": name,
            "job": job["id"],
            "caption_style": form["caption_style"],
            "hook_style": form["hook_style"],
        }
        variant["edit"] = n
        store.mutate(job_id, lambda j: j.setdefault("variants", {}).setdefault(key, []).append(variant))
    runner.submit(job["id"])
    return {"history": entry, "job": job["id"], "name": name}


def fix_history_name(store, job: dict) -> None:
    """After an editor render/draft job: the real output name from its log (it can differ from the guess)."""
    if job.get("kind") not in ("render", "draft") or not job.get("parent"):
        return
    opts = job.get("options") or {}
    key, n = opts.get("clip"), opts.get("edit")
    if not key or not n:
        return
    log = store.job_dir(job["id"]) / "log.txt"
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    found = re.findall(r"EXPORT GATE: (\S+)\.mp4", text) or re.findall(
        r"Timeline spec -> \S*/([\w\-]+)_timeline\.json", text
    )
    if not found or not library.KEY_RE.match(found[-1]):
        return
    edir = store.job_dir(job["parent"]) / "edits" / library.check_key(key)
    hist = _history(edir)
    for h in hist:
        if h.get("job") == job["id"]:
            h["name"] = found[-1]
    write_json_atomic(edir / "history.json", hist)
