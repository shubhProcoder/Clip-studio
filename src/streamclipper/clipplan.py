"""Clip plans: one plan feeds both the CapCut draft and the MP4 renderer."""

import os
from pathlib import Path

from streamclipper.brand import load_brand_kit
from streamclipper.captions.capcut import build_word_synced_captions, hook_capcut_fields
from streamclipper.captions.styles import DEFAULT_HOOK_STYLE, clip_style, get_style
from streamclipper.effects import any_on, check_effects, effects_of
from streamclipper.layout import _norm_to_px, capcut_y, caption_anchor, layout_boxes
from streamclipper.story import _inside_word, grid_off_word, off_word
from streamclipper.util import DRAFT_FPS, file_tag, fwd, run_cmd


def plan_clip_spec(
    proj_name: str,
    panels: dict,
    segs: list,
    window_start: float,
    opts: dict,
    assets: dict,
    hook_text: str = "",
    volume: float = 1.0,
    fade_ms: float = 0,
    draft_root: str = "",
    overwrite: bool = False,
    close_capcut: bool = False,
    style: dict = None,
    payoff_t: float = None,
) -> dict:
    """
    One clip -> low-level timeline spec for build_from_spec_dict.
    segs: ordered pieces with cut_start/cut_end (seconds in the SOURCE video), optional kind
    (tease|setup|tension|payoff|reaction|story), words (source time), peak_db, shock_contrast.
    Panel files start at window_start, so draft source times are (cut_start - window_start).
    payoff_t (source seconds) puts the sub-drop exactly on the payoff. style: optional caption
    overrides (font_size, glow_preset, glow_color, glow_strength, glow_range, pos_y, anim).
    SFX (all <= 0.3): whoosh at the end of the tease, sub-drop at the payoff, pop on jump cuts
    at most every 3 s.
    """
    fps = DRAFT_FPS
    kit = opts.get("brand") or {}
    style = {k: v for k, v in (style or {}).items() if v not in (None, "")}
    hook_mode = opts["hook_mode"]
    segs = list(segs)
    climax_seg = next((s for s in segs if s.get("kind") == "payoff"), None) or max(
        segs, key=lambda s: s.get("peak_db", 0.0) + s.get("shock_contrast", 0.0)
    )

    cstyle = get_style(opts.get("caption_style") or "")
    hook_style = opts.get("hook_style") or DEFAULT_HOOK_STYLE
    caption_y = panels["caption_y"]
    position = opts.get("caption_position") or "auto"
    if position != "auto":
        main_h = int(round(1920 * float(opts.get("main_ratio") or 0.55))) & ~1
        lay = layout_boxes(opts["ratio"], panels["layout"], main_h=main_h)
        a, y_px = caption_anchor(lay, position, cstyle.anim_up)
        caption_y = capcut_y(a, y_px, lay["canvas"][1])
    caption_y = float(style.get("pos_y", caption_y))
    hook_fields = hook_capcut_fields(hook_style)
    sfx_on = bool(opts.get("sfx", True))
    sfx = assets["sfx"]
    video_track, overlay_track, captions, sfx_events = [], [], [], []
    tl_cursor = 0.0
    tease_shown = False
    last_pop, sub_done, prev = -9.0, False, None
    for s_i, s in enumerate(segs):
        frames = int(round((s["cut_end"] - s["cut_start"]) * fps))
        dur_s = frames / fps
        if dur_s < 0.14:
            continue
        src_s = max(0.0, round((s["cut_start"] - window_start) * fps) / fps)
        is_tease = s.get("kind") == "tease" or s.get("is_tease", False)
        is_climax = s is climax_seg and not is_tease
        is_detail_zoom = hook_mode == "detail-zoom" and s_i == 0

        v_entry = {
            "source": panels["main_src"],
            "src_start": round(src_s, 4),
            "src_end": round(src_s + dur_s, 4),
            "tl_start": round(tl_cursor, 4),
            "volume": volume,
            "fade_ms": fade_ms,
            "zoom": 1.18 if is_detail_zoom else 1.0,
            "pos_y": panels["main_pos_y"],
            "anim": "punch-zoom" if (is_tease or is_climax) else "none",
            "kind": "tease" if is_tease else s.get("kind", "story"),
        }
        if is_tease:
            v_entry["transition"] = "zoom-switch"
        video_track.append(v_entry)
        if panels.get("overlay_src") and not panels.get("overlay_loop"):
            overlay_track.append(
                {
                    "source": panels["overlay_src"],
                    "src_start": round(src_s, 4),
                    "src_end": round(src_s + dur_s, 4),
                    "tl_start": round(tl_cursor, 4),
                    "volume": 0.0,
                    "zoom": 1.08 if is_climax else 1.0,
                    "pos_y": panels["overlay_pos_y"],
                }
            )

        if sfx_on:
            if is_tease and "whoosh" in sfx:
                sfx_events.append(
                    {"source": sfx["whoosh"], "tl_start": max(0.0, round(tl_cursor + dur_s - 0.22, 3)), "volume": 0.3}
                )
            jump_cut = (
                prev is not None and not is_tease and not prev["tease"] and abs(s["cut_start"] - prev["cut_end"]) > 0.02
            )
            jump_cut = jump_cut and not s.get("after_filler")  # a filler cut should go unnoticed: no pop
            if jump_cut and tl_cursor - last_pop >= 3.0 and "pop" in sfx:
                sfx_events.append(
                    {"source": sfx["pop"], "tl_start": round(max(0.0, tl_cursor - 0.03), 3), "volume": 0.25}
                )
                last_pop = tl_cursor
            if not sub_done and not is_tease and "sub" in sfx:
                if payoff_t is not None and s["cut_start"] <= payoff_t < s["cut_end"]:
                    sfx_events.append(
                        {
                            "source": sfx["sub"],
                            "volume": 0.3,
                            "tl_start": round(tl_cursor + (payoff_t - s["cut_start"]), 3),
                        }
                    )
                    sub_done = True
                elif payoff_t is None and is_climax:
                    sfx_events.append({"source": sfx["sub"], "tl_start": round(tl_cursor, 3), "volume": 0.3})
                    sub_done = True
        prev = {"cut_end": s["cut_end"], "tease": is_tease}

        if is_tease and hook_text:
            tease_shown = True
            captions.append(
                {
                    "text": hook_text,
                    "tl_start": round(tl_cursor, 3),
                    "tl_end": round(tl_cursor + dur_s, 3),
                    "font_size": 12.0,
                    "pos_y": caption_y if panels["layout"] != "square-frame" else panels["banner_y"],
                    "glow": True,
                    "glow_preset": "deep",
                    "glow_color": "yellow",
                    "anim": "pop-bounce",
                    **hook_fields,
                }
            )
        elif s.get("words"):
            word_caps = build_word_synced_captions(
                s["words"],
                s["cut_start"],
                dur_s,
                tl_cursor,
                caption_mode=opts.get("caption_mode", "hormozi-pop"),
                pos_y=caption_y,
                font_size=float(style.get("font_size", 11.0)),
                hindi_script=opts.get("hindi_script", "devanagari"),
                style=cstyle.name,
                case=opts.get("caption_case") or "",
                words_per=int(opts.get("words_per_caption") or 0),
                emoji=opts.get("emoji"),
                profanity=bool(opts.get("profanity_filter")),
                highlight=opts.get("highlight_color") or "",
                font=kit.get("font") or opts.get("caption_font") or "",
            )
            for c in word_caps:
                for k in ("glow_preset", "glow_color", "anim"):
                    if k in style:
                        c[k] = style[k]
            captions.extend(word_caps)
        tl_cursor += dur_s

    if hook_text and not tease_shown and tl_cursor > 0:
        captions.append(
            {
                "text": hook_text,
                "tl_start": 0.0,
                "tl_end": round(min(2.0, tl_cursor), 3),
                "layer": 1,
                "font_size": 11.5,
                "pos_y": panels["banner_y"],
                "glow": True,
                "glow_preset": "neon",
                "glow_color": "yellow",
                "anim": "pop-bounce",
                **hook_fields,
            }
        )

    if panels.get("overlay_src") and panels.get("overlay_loop"):
        f_dur = max(0.5, float(panels.get("overlay_dur") or 10.0))
        t = 0.0
        while t < tl_cursor - 0.05:
            d = min(f_dur, tl_cursor - t)
            overlay_track.append(
                {
                    "source": panels["overlay_src"],
                    "src_start": 0.0,
                    "src_end": round(d, 4),
                    "tl_start": round(t, 4),
                    "volume": 0.0,
                    "pos_y": panels["overlay_pos_y"],
                }
            )
            t += d

    if kit.get("handle") and tl_cursor > 0:
        # the MP4 places the handle in a free corner (render.py); in CapCut it starts top-left and can be moved
        captions.append(
            {
                "text": kit["handle"],
                "tl_start": 0.0,
                "tl_end": round(tl_cursor, 3),
                "layer": 2,
                "font_size": 5.5,
                "pos_x": -0.62,
                "pos_y": 0.82 if opts["ratio"] == "9:16" else 0.86,
                "glow": False,
                "anim": "none",
                "auto_highlight": False,
                "capcut_only": True,
            }
        )
    return {
        "project_name": proj_name,
        "draft_root": draft_root or "",
        "overwrite": bool(overwrite),
        "close_capcut": bool(close_capcut),
        "canvas_ratio": opts["ratio"],
        "fps": fps,
        "layout": panels["layout"],
        "preset": opts.get("preset", ""),
        "expected_duration": round(tl_cursor, 3),
        "caption_style": {
            "name": cstyle.name,
            "hook_style": hook_style,
            "font": kit.get("font") or opts.get("caption_font") or cstyle.font,
            "font_size": float(style.get("font_size", 11.0)),
            "pos_y": caption_y,
            "stroke_width": 0.07,
            "glow": True,
            "glow_preset": style.get("glow_preset", "neon"),
            "glow_color": style.get("glow_color", "yellow"),
            "glow_strength": style.get("glow_strength"),
            "glow_range": style.get("glow_range"),
            "auto_highlight": True,
            "max_chars_per_line": 13,
            "anim": style.get("anim", "pop-bounce"),
        },
        "video_track": video_track,
        "overlay_tracks": [overlay_track] if overlay_track else [],
        "captions": captions,
        "sfx_events": sfx_events,
    }


def extract_window(src: Path, start: float, end: float, out_dir: Path) -> Path:
    """Clip window file [start, end] at the source resolution (CRF 16 veryfast, AAC 48 kHz); cached."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{Path(src).stem}_win_{int(start * 1000)}_{int(end * 1000)}_{file_tag(src, start, end, 'w1')}.mp4"
    if out.is_file():
        return out
    tmp = out.with_name(out.stem + ".part.mp4")
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(src),
            "-t",
            f"{end - start:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(tmp),
        ],
        "clip window extraction",
    )
    os.replace(tmp, out)
    return out


def _tease_bounds(clip: dict, words: list, lo: float, hi: float) -> tuple:
    """Payoff flash [p-0.6, p+1.0] snapped to word boundaries, 1.2-2.0 s long, on the 1/30 s grid."""
    p = clip["peak"]
    a = max(lo, off_word(max(lo, p - 0.6), words, -1))
    if _inside_word(a, words):  # the word starts before the window: start after it instead
        a = off_word(a, words, +1)
    b = off_word(min(hi, p + 1.0), words, +1)
    if b - a > 2.0:
        ends = [x["e"] for x in words if a + 1.2 <= x["e"] <= a + 2.0]
        b = max(ends) + 0.04 if ends else off_word(a + 2.0, words, -1)
    if b - a < 1.2:
        b = off_word(a + 1.2, words, +1)
    a, b = grid_off_word(a, words, -1), grid_off_word(min(hi, b), words, +1)
    if b - a < 1.2:
        a = max(lo, grid_off_word(b - 1.2, words, -1))
    return max(lo, a), min(hi, b)


MAX_TIMELINE = 59.5  # Shorts-safe: YouTube Shorts / Reels limit is 60 s
MIN_TEASE = 1.2


def fit_timeline(segs: list, words: list, limit: float = MAX_TIMELINE) -> list:
    """
    Pieces [{cut_start, cut_end, kind, ...}] (source seconds) trimmed so the timeline is <= limit:
    first the tease is shortened (to >= 1.2 s, its end pulled back off a word), then the setup is
    cut from its start (whole leading pieces, then the first piece's start moved forward to a word
    edge). The payoff and reaction are never cut.
    """
    segs = [dict(s) for s in segs]

    def total():
        return sum(s["cut_end"] - s["cut_start"] for s in segs)

    aim = limit - 0.1  # grid rounding and word edges may add a frame or two
    if total() <= limit:
        return segs
    for s in segs:
        if s.get("kind") == "tease" and total() > aim:
            room = s["cut_end"] - s["cut_start"] - MIN_TEASE
            if room > 0:
                b = grid_off_word(s["cut_end"] - min(room, total() - aim), words, -1)
                if b - s["cut_start"] >= MIN_TEASE - 1e-6:
                    s["cut_end"] = b
    for _ in range(50):
        if total() <= limit:
            break
        k = next((i for i, s in enumerate(segs) if s.get("kind") not in ("tease", "payoff", "reaction")), None)
        if k is None:
            break
        s = segs[k]
        over = total() - aim
        if s["cut_end"] - s["cut_start"] <= over + 0.25:
            del segs[k]
            continue
        # + one frame so the grid rounding can never leave the start where it was
        a = grid_off_word(s["cut_start"] + over + 1 / 30, words, +1)
        if a >= s["cut_end"] - 0.14:
            del segs[k]
            continue
        s["cut_start"] = a
        s["words"] = [w for w in s.get("words") or [] if w["e"] > a]
    return segs


def make_clip_plan(
    name: str,
    draft_name: str,
    source_label: str,
    win_file: Path,
    ws: float,
    we: float,
    wmeta: dict,
    opts: dict,
    cam_box,
    game_box,
    panels: dict,
    spec: dict,
    words: list,
    peak: float,
    hook: str,
    spec_path: Path,
    win_offset: float = 0.0,
) -> dict:
    """
    ClipPlan for the MP4 render. All src times are seconds in the panel files (window time); tl times
    are timeline seconds. Built from the same timeline spec as the CapCut draft, so both cut identically.
    win_offset: where window time 0 sits inside source_window.path (0 for a window file).
    """
    vw, vh = wmeta["width"], wmeta["height"]

    def px(box):
        return list(_norm_to_px(box, vw, vh)) if box else None

    return {
        "tool": "capcut_studio",
        "version": "3.1",
        "type": "clip_plan",
        "name": name,
        "draft": draft_name,
        "source": source_label,
        "source_window": {
            "path": fwd(win_file),
            "start": round(ws, 3),
            "end": round(we, 3),
            "offset": round(win_offset, 3),
            "width": vw,
            "height": vh,
            "fps": wmeta["fps"],
        },
        "ratio": opts["ratio"],
        "layout": panels["layout"],
        "boxes_px": {"cam": px(cam_box), "game": px(game_box), "person": px(opts.get("person_box"))},
        "panels": {
            k: panels.get(k)
            for k in (
                "main_src",
                "main_pos_y",
                "overlay_src",
                "overlay_pos_y",
                "overlay_loop",
                "caption_y",
                "banner_y",
                "caption_anchor",
            )
        },
        "segments": [
            {
                "src_s": v["src_start"],
                "src_e": v["src_end"],
                "tl_start": v["tl_start"],
                "kind": v.get("kind", "story"),
                "fade_ms": v.get("fade_ms", 0),
                "zoom": v.get("zoom", 1.0),
            }
            for v in spec["video_track"]
        ],
        "peak": round(peak - ws, 3),
        "words": [{"w": w["w"], "s": round(w["s"] - ws, 3), "e": round(w["e"] - ws, 3)} for w in words],
        "hook_text": hook,
        "hook_mode": opts["hook_mode"],
        "caption_mode": opts["caption_mode"],
        "caption_style": spec["caption_style"].get("name") or opts.get("caption_style") or "",
        "caption_opts": {
            "font": opts.get("caption_font") or "",
            "position": opts.get("caption_position") or "auto",
            "highlight": opts.get("highlight_color") or "",
            "case": opts.get("caption_case") or "",
            "words": int(opts.get("words_per_caption") or 0),
            "emoji": opts.get("emoji"),
            "profanity_filter": bool(opts.get("profanity_filter")),
        },
        "hook_style": spec["caption_style"].get("hook_style") or opts.get("hook_style") or DEFAULT_HOOK_STYLE,
        "font": opts["font"],
        "hindi_script": opts.get("hindi_script", "devanagari"),
        "caption_y": spec["caption_style"]["pos_y"],
        "volume": spec["video_track"][0]["volume"] if spec["video_track"] else 1.0,
        "captions": spec["captions"],
        "sfx": [
            {
                "name": Path(e["source"]).stem,
                "source": fwd(e["source"]),
                "tl_start": e["tl_start"],
                "volume": e["volume"],
            }
            for e in spec["sfx_events"]
        ],
        "expected_duration": spec["expected_duration"],
        "timeline_spec": fwd(spec_path),
        # plans without effects render exactly as before 5.3 (the key is only written when one is on)
        **({"effects": dict(opts["effects"])} if any_on(opts.get("effects")) else {}),
    }


def with_effects(plan: dict, args) -> dict:
    """A copy of a clip plan with the effect flags of `render --spec` applied (zoom, enhance_speech, brand_kit).
    Filler removal changes the cuts, so it needs the clip to be rebuilt (clip-stream/build) and is ignored here."""
    raw = dict(plan.get("effects") or {})
    for k in ("zoom", "enhance_speech"):
        if getattr(args, k, None):
            raw[k] = getattr(args, k)
    if getattr(args, "brand_kit", None):
        kit = load_brand_kit(args.brand_kit)
        raw["brand_kit"], raw["brand_sha1"] = kit["path"], kit["sha1"]
    if getattr(args, "remove_fillers", None) and not raw.get("remove_fillers"):
        print("NOTE: --remove-fillers needs the clip to be rebuilt (clip-stream or build); the cuts stay as they are.")
    eff = check_effects(raw)
    if eff == effects_of(plan):
        return plan
    out = dict(plan, effects=eff)
    if eff["zoom"] != effects_of(plan)["zoom"]:
        out.pop("zoom_plan", None)
    return out


def restyle_plan(plan: dict, caption_style: str = "", hook_style: str = "") -> dict:
    """
    A copy of a clip plan with another caption and/or hook style, for `render --spec <name>.clip.json
    --caption-style NAME`. Word captions are rebuilt per piece with the new style's grouping; the hook
    banner, cuts and audio stay as they are. Only the MP4 changes (the CapCut draft is not touched).
    """
    out = dict(plan)
    if hook_style:
        out["hook_style"] = hook_style
    if not caption_style:
        return out
    style = clip_style(caption_style, plan.get("category"), plan.get("content") or "gaming")
    out["caption_style"] = style
    copts = plan.get("caption_opts") or {}
    hook = plan.get("hook_text") or ""
    old = plan.get("captions") or []
    sample = next((c for c in old if c.get("text") != hook), {})
    caps = [c for c in old if hook and c.get("text") == hook]
    words = plan.get("words") or []
    for s in plan.get("segments") or []:
        if s.get("kind") == "tease":
            continue
        a, b = s["src_s"], s["src_e"]
        seg_words = [w for w in words if w["e"] > a and w["s"] < b]
        caps += build_word_synced_captions(
            seg_words,
            a,
            b - a,
            s["tl_start"],
            caption_mode=plan.get("caption_mode") or "hormozi-pop",
            pos_y=float(sample.get("pos_y", plan.get("caption_y", 0.18))),
            font_size=float(sample.get("font_size", 11.0)),
            hindi_script=plan.get("hindi_script", "devanagari"),
            style=style,
            case=copts.get("case") or "",
            words_per=int(copts.get("words") or 0),
            emoji=copts.get("emoji"),
            profanity=bool(copts.get("profanity_filter")),
            highlight=copts.get("highlight") or "",
            font=copts.get("font") or "",
        )
    out["captions"] = sorted(caps, key=lambda c: (c["tl_start"], c.get("layer", 0)))
    return out
