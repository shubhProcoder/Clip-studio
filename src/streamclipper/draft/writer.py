"""CapCut Desktop draft writer."""

import copy
import json
import re
import shutil
import time
from pathlib import Path

from streamclipper.captions.capcut import (
    _kf_track,
    make_pop_bounce_keyframes,
    make_text_glow_effect,
    parse_multicolor_styles,
)
from streamclipper.captions.styles import NEON_PALETTE
from streamclipper.draft.rootmeta import register_draft, unique_draft_name
from streamclipper.env import (
    TRANSITION_PRESETS,
    capcut_running,
    default_draft_root,
    ensure_bundled_assets,
    find_capcut_local,
    find_draft_root,
    find_font_fallback,
    kill_capcut,
    resolve_font,
)
from streamclipper.layout import make_crop_dict, probe_video
from streamclipper.paths import TEMPLATE_DIR
from streamclipper.util import COMPANION_TEMPLATES, DRAFT_EXTRA_BYTES, DRAFT_FPS, ToolError, fwd, new_id, sec_to_us

PORTABLE_MANIFEST = "streamclipper_portable.json"
CAPCUT_LOCAL_TOKEN = "__CAPCUT_LOCAL__"
FONTS_TOKEN = "__STREAMCLIPPER_FONTS__"


def fonts_dir_fwd() -> str:
    """Bundled font folder as written into drafts (forward slashes, resolved)."""
    return fwd((TEMPLATE_DIR / "fonts").resolve())


def load_schema(cc_local: Path) -> dict:
    txt = (TEMPLATE_DIR / "capcut_schema.json").read_text(encoding="utf-8")
    return json.loads(txt.replace("__CAPCUT_LOCAL__", fwd(cc_local)))


def resolve_draft_root(spec_root: str = "") -> Path:
    root = Path(spec_root) if spec_root else find_draft_root()
    if not root.is_dir():
        raise ToolError(
            f"CapCut draft folder not found: {root}",
            "install/open CapCut Desktop once, or pass --draft-root, or use --no-draft with --render",
        )
    return root


def guard_capcut(draft_root: Path, close_capcut: bool = False):
    """Stop when CapCut is open and we would write into its real draft folder."""
    try:
        same = draft_root.resolve() == default_draft_root().resolve()
    except OSError:
        same = True
    if not same or not capcut_running():
        return
    if not close_capcut:
        raise ToolError(
            "CapCut is open; close it first (CapCut rewrites root_meta_info.json on exit)",
            "or pass --close-capcut to force-close it (unsaved CapCut edits are lost)",
        )
    print("Closing CapCut (--close-capcut) ...")
    kill_capcut()


def build_from_spec_dict(spec: dict) -> Path:
    """Write one CapCut Desktop draft from a low-level timeline spec. Returns the draft folder."""
    cc_local = find_capcut_local()
    draft_root = resolve_draft_root(spec.get("draft_root") or "")
    guard_capcut(draft_root, bool(spec.get("close_capcut", False)))
    assets = ensure_bundled_assets(cc_local, install=True)
    schema = load_schema(cc_local)
    X = schema["extra"]

    req_name = str(spec.get("project_name") or "").strip()
    if not req_name:
        raise ToolError("The spec has no project_name.", 'add "project_name": "My_Clip" to the spec')
    if re.search(r'[<>:"/\\|?*]', req_name):
        raise ToolError(
            f"Draft name '{req_name}' contains characters Windows does not allow.",
            'remove < > : " / \\ | ? * from the name',
        )
    overwrite = bool(spec.get("overwrite", False))
    portable = bool(spec.get("portable")) or not cc_local.is_dir()
    proj_name = req_name if overwrite else unique_draft_name(draft_root, req_name)
    if proj_name != req_name:
        print(f"Draft '{req_name}' already exists; writing '{proj_name}' instead (pass --overwrite to replace it).")
    proj_dir = draft_root / proj_name
    if proj_dir.exists():  # only reachable with overwrite=True
        tl_dir = proj_dir / "Timelines"
        if tl_dir.exists():
            shutil.rmtree(tl_dir, ignore_errors=True)
        if (proj_dir / ".locked").exists():
            (proj_dir / ".locked").unlink()
    proj_dir.mkdir(parents=True, exist_ok=True)

    dc = copy.deepcopy(schema["draft_content_base"])
    dc["id"] = new_id()
    dc["name"] = proj_name
    fps = float(spec.get("fps", DRAFT_FPS))
    dc["fps"] = fps

    ratio = spec.get("canvas_ratio", "9:16")
    canvas_map = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080), "4:5": (1080, 1350)}
    cw, ch = canvas_map.get(ratio, (1080, 1920))
    dc["canvas_config"] = {"ratio": "original", "width": cw, "height": ch, "background": None}

    mats = dc["materials"]
    for k in list(mats.keys()):
        if isinstance(mats[k], list):
            mats[k] = []
    dc["tracks"] = []
    probe_cache = {}

    def probe(p: Path) -> dict:
        key = str(Path(p).resolve())
        if key not in probe_cache:
            probe_cache[key] = probe_video(Path(p))
        return probe_cache[key]

    portable_media = {}  # path written into the draft -> file name in <draft>/media
    portable_src = {}  # resolved source path -> path written into the draft

    def media_path(path: str) -> str:
        """Portable drafts carry their own copy of every video/audio file in <draft>/media."""
        if not portable:
            return path
        src_p = Path(path).resolve()
        key = str(src_p)
        if key not in portable_src:
            media_dir = proj_dir / "media"
            media_dir.mkdir(exist_ok=True)
            fname, k = src_p.name, 2
            while fname in portable_media.values():
                fname = f"{src_p.stem}_{k}{src_p.suffix}"
                k += 1
            shutil.copy2(src_p, media_dir / fname)
            portable_src[key] = fwd(media_dir / fname)
            portable_media[portable_src[key]] = fname
        return portable_src[key]

    def add_mat(list_name: str, tpl: dict, **fields) -> str:
        m = copy.deepcopy(tpl)
        m["id"] = new_id()
        m.update(fields)
        mats.setdefault(list_name, []).append(m)
        return m["id"]

    def new_track(ttype: str) -> dict:
        t = copy.deepcopy(schema[f"track_{ttype}_base"])
        t["id"] = new_id()
        t["type"] = ttype
        t["segments"] = []
        return t

    def add_video_seg(clip_item: dict, track_idx: int):
        src_path = Path(clip_item["source"])
        meta = probe(src_path)
        media_us = int(round(meta["dur"] * 1_000_000))
        s_us = sec_to_us(clip_item.get("src_start", 0.0), fps)
        e_us = min(media_us, sec_to_us(clip_item.get("src_end", meta["dur"]), fps))
        dur_us = max(33333, e_us - s_us)

        vid_id = add_mat(
            "videos",
            schema["mat_video_base"],
            path=media_path(meta["path"]),
            material_name=meta["file"],
            duration=media_us,
            width=meta["width"],
            height=meta["height"],
            has_audio=meta["has_audio"],
            crop=make_crop_dict(clip_item.get("crop")),
            crop_ratio="custom" if clip_item.get("crop") else "free",
        )
        speed = float(clip_item.get("speed", 1.0))
        # Same companion materials and order as drafts CapCut Desktop accepted.
        extra_refs = [
            add_mat("speeds", X["speed"], speed=speed),
            add_mat("placeholder_infos", X["placeholder_info"]),
            add_mat("canvases", X["canvas"]),
            add_mat("material_animations", X["vid_anim"]),
            add_mat("sound_channel_mappings", X["sound_channel_mapping"]),
            add_mat("material_colors", X["material_color"]),
            add_mat("vocal_separations", X["vocal_separation"]),
        ]
        fade_us = int(round(float(clip_item.get("fade_ms", 0) or 0) * 1000))
        if fade_us > 0 and float(clip_item.get("volume", 1.0)) > 0:
            extra_refs.append(
                add_mat("audio_fades", X["audio_fade"], fade_in_duration=fade_us, fade_out_duration=fade_us)
            )

        seg = copy.deepcopy(schema["seg_video_base"])
        seg["id"] = new_id()
        seg["material_id"] = vid_id
        seg["source_timerange"] = {"start": s_us, "duration": dur_us}
        tl_s = sec_to_us(clip_item["tl_start"], fps) if "tl_start" in clip_item else 0
        seg["target_timerange"] = {"start": tl_s, "duration": dur_us}
        seg["speed"] = speed
        vol = float(clip_item.get("volume", 1.0))
        seg["volume"] = vol
        seg["last_nonzero_volume"] = vol if vol > 0 else 1.0
        seg["hdr_settings"] = {"intensity": 1.0, "mode": 1, "nits": 1000}
        seg["render_index"] = track_idx
        zm = float(clip_item.get("zoom", 1.0))
        seg["clip"]["scale"] = {"x": zm, "y": zm}
        seg["clip"]["transform"] = {"x": float(clip_item.get("pos_x", 0.0)), "y": float(clip_item.get("pos_y", 0.0))}

        tr_name = clip_item.get("transition")
        if tr_name and tr_name in TRANSITION_PRESETS and tr_name in assets["transitions"]:
            tinfo = TRANSITION_PRESETS[tr_name]
            tr_id = new_id()
            mats["transitions"].append(
                {
                    "id": tr_id,
                    "effect_id": tinfo["effect_id"],
                    "resource_id": tinfo["effect_id"],
                    "name": tinfo["name"],
                    "duration": tinfo["duration"],
                    "is_overlap": tinfo["is_overlap"],
                    "path": assets["transitions"][tr_name],
                    "platform": "all",
                    "type": "transition",
                    "category_id": "",
                    "category_name": "",
                    "source_platform": 1,
                }
            )
            extra_refs.append(tr_id)
        seg["extra_material_refs"] = extra_refs
        if clip_item.get("zoom_keys"):  # punch-in zoom (effects.add_capcut_zoom): scale + position keyframes
            pts = clip_item["zoom_keys"]
            seg["common_keyframes"] = [
                _kf_track(prop, [(p[0], p[k]) for p in pts])
                for prop, k in (
                    ("KFTypeScaleX", 1),
                    ("KFTypeScaleY", 1),
                    ("KFTypePositionX", 2),
                    ("KFTypePositionY", 3),
                )
            ]
        elif clip_item.get("anim") == "punch-zoom":
            seg["common_keyframes"] = make_pop_bounce_keyframes(dur_us, base_scale=zm, anim_style="punch-zoom")
        return seg, dur_us

    max_end_us = 0

    # 1. Main video track
    if spec.get("video_track"):
        v_track = new_track("video")
        cursor_us = 0
        for item in spec["video_track"]:
            seg, dur_us = add_video_seg(item, 0)
            if "tl_start" not in item or seg["target_timerange"]["start"] < cursor_us:
                seg["target_timerange"]["start"] = cursor_us
            cursor_us = seg["target_timerange"]["start"] + dur_us
            max_end_us = max(max_end_us, cursor_us)
            v_track["segments"].append(seg)
        dc["tracks"].append(v_track)

    # 2. Overlay tracks (facecam panel, filler, b-roll)
    overlay_tracks = list(spec.get("overlay_tracks", []))
    if spec.get("broll_track"):
        overlay_tracks = [spec["broll_track"]] + overlay_tracks
    for t_i, o_list in enumerate(overlay_tracks, start=1):
        if not o_list:
            continue
        o_track = new_track("video")
        prev_end = 0
        for item in sorted(o_list, key=lambda x: float(x.get("tl_start", 0.0))):
            item_copy = dict(item)
            item_copy.setdefault("volume", 0.0)
            seg, dur_us = add_video_seg(item_copy, t_i)
            tgt_us = max(prev_end, seg["target_timerange"]["start"])
            seg["target_timerange"]["start"] = tgt_us
            prev_end = tgt_us + dur_us
            max_end_us = max(max_end_us, prev_end)
            o_track["segments"].append(seg)
        dc["tracks"].append(o_track)

    # 3. Text tracks (one per caption "layer"; layer 0 = word captions, layer 1 = hook banner)
    if spec.get("captions"):
        style_cfg = spec.get("caption_style", {})
        font_choice = style_cfg.get("font", "barlow")
        sys_font_path = find_font_fallback(cc_local)
        default_sz = float(style_cfg.get("font_size", 11.5))
        default_py = float(style_cfg.get("pos_y", -0.25))
        default_stroke = float(style_cfg.get("stroke_width", 0.07))
        default_glow = bool(style_cfg.get("glow", True))
        default_glow_preset = style_cfg.get("glow_preset", "neon")
        default_glow_col = style_cfg.get("glow_color", "yellow")
        auto_hl = bool(style_cfg.get("auto_highlight", True))
        max_cpl = int(style_cfg.get("max_chars_per_line", 13))
        base_style_tpl = json.loads(schema["mat_text_base"]["content"])["styles"][0]

        layers = {}
        for cap in spec["captions"]:
            layers.setdefault(int(cap.get("layer", 0)), []).append(cap)
        render_base = 14000
        for layer in sorted(layers):
            t_track = new_track("text")
            prev_end_us = 0
            for idx, cap in enumerate(sorted(layers[layer], key=lambda c: float(c["tl_start"]))):
                s_us = max(prev_end_us, sec_to_us(cap["tl_start"], fps))
                e_us = sec_to_us(cap["tl_end"], fps)
                if e_us <= s_us + 33333:
                    e_us = s_us + 66666
                dur_us = e_us - s_us
                prev_end_us = e_us
                max_end_us = max(max_end_us, e_us)

                raw_txt = str(cap["text"]).strip()
                sz = float(cap.get("font_size", default_sz))
                py = float(cap.get("pos_y", default_py))
                px = float(cap.get("pos_x", 0.0))
                use_glow = bool(cap.get("glow", default_glow))
                g_preset = cap.get("glow_preset", default_glow_preset)
                g_col_name = cap.get("glow_color", default_glow_col)
                _, req_glow_hex = NEON_PALETTE.get(g_col_name, NEON_PALETTE["yellow"])

                font_path, font_id = resolve_font(cap.get("font", font_choice), raw_txt, assets, cc_local)
                clean_txt, styles_list, detected_hex = parse_multicolor_styles(
                    raw_txt,
                    base_style_tpl,
                    font_path,
                    font_id,
                    sz,
                    float(cap.get("stroke_width", default_stroke)),
                    auto_highlight=cap.get("auto_highlight", auto_hl),
                    max_chars_per_line=int(cap.get("max_chars_per_line", max_cpl)),
                    keep_devanagari=(Path(font_path).name == "Baloo2-ExtraBold.ttf"),
                    keep_emoji=bool(cap.get("emoji")),
                    colour_map=cap.get("colour_map"),
                    stroke_rgb=cap.get("stroke_rgb"),
                )

                tm = copy.deepcopy(schema["mat_text_base"])
                tid = new_id()
                tm["id"] = tid
                tm["font_size"] = sz
                tm["font_path"] = font_path or sys_font_path
                tm["font_id"] = font_id
                tm["use_effect_default_color"] = False
                tm["line_max_width"] = float(cap.get("line_max_width", 0.82))
                if cap.get("background"):  # highlight-box / bold-box / top-bar
                    tm["background_color"] = cap["background"]
                    tm["background_alpha"] = 1.0
                    tm["background_style"] = 1
                    tm["background_round_radius"] = 0.12
                tm["has_shadow"] = True
                tm["shadow_color"] = "#000000"
                tm["shadow_alpha"] = 0.88
                tm["shadow_smoothing"] = 0.025
                tm["shadow_distance"] = 4.5
                tm["shadow_angle"] = -45.0
                tm["border_alpha"] = 1.0
                tm["border_color"] = "#0A0A12"
                tm["border_width"] = float(cap.get("stroke_width", default_stroke))
                tm["check_flag"] = 71 if use_glow else 15
                tm["content"] = json.dumps({"text": clean_txt, "styles": styles_list}, ensure_ascii=False)
                mats["texts"].append(tm)

                extra_refs = [add_mat("material_animations", X["txt_anim"])]
                if use_glow:
                    glow_hex = detected_hex if detected_hex != "#FFFFFF" else req_glow_hex
                    geff = make_text_glow_effect(
                        g_preset,
                        glow_hex,
                        assets,
                        strength=cap.get("glow_strength", style_cfg.get("glow_strength")),
                        rng=cap.get("glow_range", style_cfg.get("glow_range")),
                    )
                    if geff is not None:
                        mats["effects"].append(geff)
                        extra_refs.append(geff["id"])

                tseg = copy.deepcopy(schema["seg_text_base"])
                tseg["id"] = new_id()
                tseg["material_id"] = tid
                tseg["target_timerange"] = {"start": s_us, "duration": dur_us}
                tseg["clip"]["transform"] = {"x": px, "y": py}
                tseg["clip"]["scale"] = {"x": 1.0, "y": 1.0}
                if cap.get("rotation"):
                    tseg["clip"]["rotation"] = float(cap["rotation"])
                tseg["render_index"] = render_base + idx
                tseg["extra_material_refs"] = extra_refs
                anim_mode = cap.get("anim", style_cfg.get("anim", "pop-bounce"))
                tseg["common_keyframes"] = make_pop_bounce_keyframes(
                    dur_us, base_scale=1.0, anim_style=anim_mode, base_y=py, base_x=px
                )
                t_track["segments"].append(tseg)
            render_base += 1000
            dc["tracks"].append(t_track)

    # 4. Audio: music + SFX. Items keep their exact start; an overlapping item opens a new track.
    audio_items = list(spec.get("audio_tracks", [])) + list(spec.get("sfx_events", []))
    a_tracks = []
    for item in sorted(audio_items, key=lambda x: float(x.get("tl_start", 0.0))):
        apath = Path(item["source"])
        if not apath.is_file():
            print(f"WARN: audio file not found, skipped: {apath}")
            continue
        ameta = probe(apath)
        s_us = sec_to_us(item.get("src_start", 0.0), fps)
        e_us = sec_to_us(item.get("src_end", ameta["dur"]), fps)
        dur_us = max(33333, e_us - s_us)
        tl_s = sec_to_us(item.get("tl_start", 0.0), fps)
        if max_end_us > 0 and item.get("trim_to_video", True):
            if tl_s >= max_end_us:
                continue
            dur_us = min(dur_us, max(33333, max_end_us - tl_s))

        aid = add_mat(
            "audios",
            schema["mat_audio_base"],
            path=media_path(ameta["path"]),
            name=item.get("name", apath.stem),
            type=item.get("audio_type", "sound" if dur_us < 5_000_000 else "extract_music"),
            duration=int(round(ameta["dur"] * 1_000_000)),
        )
        aseg = copy.deepcopy(schema["seg_audio_base"])
        aseg["id"] = new_id()
        aseg["material_id"] = aid
        aseg["source_timerange"] = {"start": s_us, "duration": dur_us}
        aseg["target_timerange"] = {"start": tl_s, "duration": dur_us}
        vol = float(item.get("volume", 0.35))
        aseg["volume"] = vol
        aseg["last_nonzero_volume"] = vol if vol > 0 else 1.0
        aseg["extra_material_refs"] = [
            add_mat("speeds", X["speed"]),
            add_mat("sound_channel_mappings", X["sound_channel_mapping"]),
            add_mat("vocal_separations", X["vocal_separation"]),
        ]
        slot = next((t for t in a_tracks if t["end"] <= tl_s), None)
        if slot is None:
            slot = {"track": new_track("audio"), "end": 0}
            a_tracks.append(slot)
        slot["track"]["segments"].append(aseg)
        slot["end"] = tl_s + dur_us
    for t in a_tracks:
        dc["tracks"].append(t["track"])

    dc["duration"] = max_end_us

    # Draft files (same set CapCut Desktop writes)
    dc_str = json.dumps(dc, ensure_ascii=False, separators=(",", ":"))
    if portable:  # machine-specific folders become tokens that import-draft fills in on the target PC
        dc_str = dc_str.replace(fwd(cc_local), CAPCUT_LOCAL_TOKEN).replace(fonts_dir_fwd(), FONTS_TOKEN)
    (proj_dir / "draft_content.json").write_text(dc_str, encoding="utf-8")
    (proj_dir / "draft_content.json.bak").write_text(dc_str, encoding="utf-8")

    now_us = int(time.time() * 1_000_000)
    now_sec = int(time.time())
    dmi = copy.deepcopy(schema["draft_meta_base"])
    dmi["draft_id"] = dc["id"]
    dmi["draft_name"] = proj_name
    dmi["draft_fold_path"] = fwd(proj_dir)
    dmi["draft_root_path"] = fwd(draft_root)
    dmi["tm_duration"] = max_end_us
    dmi["tm_draft_create"] = now_us
    dmi["tm_draft_modified"] = now_us
    (proj_dir / "draft_meta_info.json").write_text(json.dumps(dmi, ensure_ascii=False), encoding="utf-8")

    (proj_dir / "draft.extra").write_bytes(DRAFT_EXTRA_BYTES)
    (proj_dir / "draft_settings").write_text(
        f"[General]\ndraft_create_time={now_sec}\ndraft_last_edit_time={now_sec}\n"
        f"real_edit_seconds=0\nreal_edit_keys=0\ncloud_last_modify_platform=windows\n",
        encoding="utf-8",
    )
    layout = {
        "dockItems": [{"dockIndex": 0, "ratio": 1, "timelineIds": [new_id()], "timelineNames": ["Timeline 01"]}],
        "layoutOrientation": 0,
    }
    (proj_dir / "timeline_layout.json").write_text(json.dumps(layout), encoding="utf-8")
    for fn in COMPANION_TEMPLATES:
        src = TEMPLATE_DIR / fn
        if src.is_file():
            shutil.copyfile(src, proj_dir / fn)

    if portable:
        manifest = {
            "type": "streamclipper_portable",
            "version": 1,
            "draft_name": proj_name,
            "media": portable_media,
            "capcut_local_token": CAPCUT_LOCAL_TOKEN,
            "fonts_token": FONTS_TOKEN,
        }
        (proj_dir / PORTABLE_MANIFEST).write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")

    # Register in root_meta_info.json (backup first; keep every other draft entry)
    register_draft(draft_root, proj_dir, proj_name, dc["id"], max_end_us, schema["root_entry_base"])

    print(
        f"BUILT: {proj_name} | ratio={ratio} | dur={max_end_us / 1e6:.2f}s | tracks={len(dc['tracks'])} "
        f"| dir={fwd(proj_dir)}" + (f" | portable ({len(portable_media)} media files in media/)" if portable else "")
    )
    return proj_dir
