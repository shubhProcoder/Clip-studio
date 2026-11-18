"""Command implementations (cmd_*)."""

import hashlib
import json
import math
from pathlib import Path

from streamclipper import reframe
from streamclipper.asr import filler_pass, read_wav16k, rescue_words
from streamclipper.audio import (
    _mmss,
    audio_envelope,
    extract_rms_islands,
    islands_from_env,
    loudness_gain,
    measure_loudness,
)
from streamclipper.captions.styles import (
    CAPTION_FONTS,
    CATEGORY_STYLES,
    DEFAULT_STYLE,
    FONT_FILES,
    HOOK_STYLES,
    STYLE_ALIASES,
    STYLES,
    TALK_STYLE,
    clip_style,
    mask_profanity,
)
from streamclipper.chat import load_chat_users
from streamclipper.clipplan import (
    _tease_bounds,
    extract_window,
    fit_timeline,
    make_clip_plan,
    plan_clip_spec,
    restyle_plan,
    with_effects,
)
from streamclipper.discovery import _discover_settings, clips_document, discover, print_clip_table
from streamclipper.download import download_audio, download_slice, fetch_chat, youtube_heatmap
from streamclipper.draft.rootmeta import RENDER_SUFFIXES, unique_output_name
from streamclipper.draft.writer import build_from_spec_dict, guard_capcut, resolve_draft_root
from streamclipper.effects import add_capcut_zoom, effects_of, plan_zoom
from streamclipper.env import detect_env, doctor_checks, ensure_bundled_assets, find_capcut_local
from streamclipper.faces import speech_spans
from streamclipper.fillers import remove_fillers
from streamclipper.gate import _gate_summary, render_and_gate
from streamclipper.layout import (
    black_frames,
    cut_black,
    detect_player_and_webcam,
    probe_video,
    render_layout_panels,
    render_reframe_panel,
    sample_exposure,
    with_caption_anchor,
)
from streamclipper.options import clip_boxes, clip_layout, normalize_clip_spec, resolve_options
from streamclipper.paths import ENV_FILE
from streamclipper.report import rel_path, write_report
from streamclipper.state import RunState, write_json_atomic
from streamclipper.story import snap_segments, trim_pauses
from streamclipper.util import DRAFT_FPS, VIDEO_EXTS, SkipClip, ToolError, file_tag, fwd, parse_time, run_cmd


def cmd_doctor(args):
    cc_local = find_capcut_local()
    assets = ensure_bundled_assets(cc_local, install=True)
    env = detect_env(save=False, install_assets=False)
    rows = doctor_checks(env, assets)
    env["checks"] = [{"check": r[0], "status": r[1], "detail": r[2], "fix": r[3]} for r in rows]
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(json.dumps(env, indent=2, ensure_ascii=False), encoding="utf-8")
    if getattr(args, "json", False):
        print(json.dumps(env, indent=2, ensure_ascii=False))
        return
    w0 = max(len(r[0]) for r in rows)
    w2 = min(64, max(len(r[2]) for r in rows))
    print(f"{'CHECK':<{w0}} | {'STATUS':<7} | {'DETAIL':<{w2}} | FIX")
    print("-" * (w0 + w2 + 24))
    for c, s, d, f in rows:
        d = d if len(d) <= w2 else d[: w2 - 3] + "..."
        print(f"{c:<{w0}} | {s:<7} | {d:<{w2}} | {f}")
    problems = sum(1 for r in rows if r[1] == "MISSING")
    warns = sum(1 for r in rows if r[1] == "WARN")
    print(f"\n{problems} problems, {warns} warnings. Saved {fwd(ENV_FILE)}")


def cmd_analyze(args):
    src = Path(args.input)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "pcm_raw"
    raw_dir.mkdir(exist_ok=True)

    if not src.exists():
        raise ToolError(f"Input not found: {src}", "check the path; put quotes around paths with spaces")
    files = sorted([f for f in src.iterdir() if f.suffix.lower() in VIDEO_EXTS]) if src.is_dir() else [src]
    if not files:
        raise ToolError(f"No video files found in {src}", f"supported: {', '.join(sorted(VIDEO_EXTS))}")
    language = getattr(args, "language", "auto")
    language = None if not language or language == "auto" else language

    whisper_model = None
    if args.transcribe and not getattr(args, "no_transcribe", False):
        try:
            from faster_whisper import WhisperModel

            whisper_model = WhisperModel(args.whisper_model, device="cpu", compute_type="int8")
            print(f"Loaded faster-whisper ({args.whisper_model})")
        except Exception as e:
            print(f"[INFO] faster-whisper unavailable ({e}); trying openai-whisper...")
            try:
                import whisper

                whisper_model = ("oai", whisper.load_model(args.whisper_model))
            except Exception as e2:
                print(f"[WARN] Whisper not available ({e2}). Skipping transcription.")

    results = []
    for fpath in files:
        meta = probe_video(fpath)
        print(f"Analyzing {fpath.name} ({meta['dur']}s, {meta['width']}x{meta['height']}@{meta['fps']}fps)...")
        cache_stem = f"{fpath.stem}_{file_tag(fpath)}"
        raw_p = raw_dir / f"{cache_stem}.raw"
        islands = (
            extract_rms_islands(fpath, raw_p, silence_db=args.db, min_pause_s=args.min_pause)
            if meta["has_audio"]
            else []
        )
        exp = sample_exposure(fpath, meta["dur"], step_s=args.exp_step) if args.exposure else []
        stream_layout = detect_player_and_webcam(fpath, meta["dur"]) if meta["width"] >= meta["height"] else None

        words = []
        if whisper_model is not None and meta["has_audio"]:
            wav_tmp = raw_dir / f"{cache_stem}_16k_vox.wav"
            if not wav_tmp.is_file():
                # Vocal bandpass before Whisper prevents game SFX hallucinations!
                run_cmd(
                    [
                        "ffmpeg",
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(fpath),
                        "-vn",
                        "-af",
                        "highpass=f=180,lowpass=f=3800",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        str(wav_tmp),
                    ],
                    "audio extraction for Whisper",
                )
            if isinstance(whisper_model, tuple) and whisper_model[0] == "oai":
                tr = whisper_model[1].transcribe(str(wav_tmp), word_timestamps=True, language=language)
                for seg in tr.get("segments", []):
                    for w in seg.get("words", []):
                        words.append({"w": w["word"].strip(), "s": round(w["start"], 3), "e": round(w["end"], 3)})
            else:
                segs, _ = whisper_model.transcribe(read_wav16k(wav_tmp), word_timestamps=True, language=language)
                for seg in segs:
                    for w in seg.words or []:
                        words.append({"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3)})

        if words and islands:
            for isl in islands:
                isl_words = [
                    w for w in words if w["e"] > isl["speech_start"] - 0.08 and w["s"] < isl["speech_end"] + 0.08
                ]
                isl["words"] = isl_words
                isl["text"] = " ".join(w["w"] for w in isl_words)

        results.append(
            {
                "meta": meta,
                "stream_layout": stream_layout,
                "islands": islands,
                "words": words,
                "exposure": exp,
            }
        )

    out_json = out_dir / "analysis.json"
    out_json.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved analysis -> {out_json}")
    return results


def cmd_build(args):
    spec_path = Path(args.spec).resolve()
    if not spec_path.is_file():
        raise ToolError(f"Spec file not found: {spec_path}", "check the --spec path")
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))  # Notepad/PowerShell may add a BOM
    if "video_track" in spec or "project_name" in spec:
        if args.overwrite:
            spec["overwrite"] = True
        if args.close_capcut:
            spec["close_capcut"] = True
        if args.draft_root:
            spec["draft_root"] = args.draft_root
        if getattr(args, "portable_drafts", False):
            spec["portable"] = True
        if args.no_draft:
            print("--no-draft: nothing to do for a low-level timeline spec.")
        else:
            build_from_spec_dict(spec)
        if args.render:
            print(
                "NOTE: --render needs a clip plan; a low-level timeline spec has none. "
                "Use a README-style clip spec, or 'render --spec <name>.clip.json'."
            )
        return
    build_high_level_clip(spec, spec_path.parent, args)


def build_high_level_clip(spec: dict, base_dir: Path, args) -> dict:
    norm = normalize_clip_spec(spec, base_dir)
    opts = resolve_options(args, norm)
    src = norm["source_video"]
    meta = probe_video(src)
    start = max(0.0, norm["start"] or 0.0)
    end = min(meta["dur"], norm["end"] if norm["end"] is not None else meta["dur"])
    if end - start < 0.5:
        raise ToolError(f"Clip range {start:.2f}-{end:.2f}s is too short.", "check start/end in the spec")
    draft_root = args.draft_root or spec.get("draft_root", "")
    if not args.no_draft:
        guard_capcut(resolve_draft_root(draft_root), args.close_capcut or bool(spec.get("close_capcut")))
    work_dir = Path(args.out).resolve() if args.out else (base_dir / "capcut_studio_work")
    work_dir.mkdir(parents=True, exist_ok=True)
    assets = ensure_bundled_assets(find_capcut_local(), install=not args.no_draft)
    overwrite = args.overwrite or bool(spec.get("overwrite"))
    name = norm["draft_name"]
    if not overwrite:
        name = unique_output_name(work_dir, name, None if args.no_draft else resolve_draft_root(draft_root))
        if name != norm["draft_name"]:
            print(f"'{norm['draft_name']}' already exists; writing '{name}' instead (pass --overwrite to replace it).")

    cam_box, game_box = clip_boxes(src, meta, opts)
    ws, we = max(0.0, start - 1.0), min(meta["dur"], end + 1.0)
    anchor = reframe.validate_anchor(norm.get("caption_anchor"), opts["ratio"])
    rsegs = norm.get("reframe_segments")
    if reframe.uses_reframe(rsegs):
        # reframe editor: per-segment layouts and panned crops in one composite panel; contiguous frame-exact
        # pieces with the continuous source audio (no fades), each word captioned once
        rctx = {"width": meta["width"], "height": meta["height"], "ratio": opts["ratio"]}
        rsegs = reframe.validate_segments(rsegs, rctx, start, end)
        panels = render_reframe_panel(src, work_dir / "panels", meta, rsegs, ws, we, opts["ratio"], anchor)
        pieces = reframe.frame_pieces(rsegs, ws)
        piece_words = reframe.assign_words(norm["words"], pieces)
    else:
        rsegs = None
        if opts.get("layout") == "speaker" and norm["words"]:  # the speaker panel covers the source from 0 s
            opts = dict(opts, speech_spans=speech_spans(norm["words"]))
        panels = render_layout_panels(src, meta, opts, cam_box, game_box, ws, we - ws, work_dir / "panels")
        panels = with_caption_anchor(panels, anchor, opts["ratio"])
        pieces = [(start, end)]
        if norm["trim_silence"] and meta["has_audio"]:
            env = audio_envelope(src, work_dir / "analysis")
            islands = islands_from_env(env, -48.0, 0.20)
            trimmed = trim_pauses({"start": start, "end": end}, islands, 0.55)
            pieces = [(s["src_s"], s["src_e"]) for s in snap_segments(trimmed, norm["words"], ws, we)]
        piece_words = [[w for w in norm["words"] if w["e"] > a and w["s"] < b] for a, b in pieces]
    segs = [
        {
            "cut_start": a,
            "cut_end": b,
            "dur": b - a,
            "peak_db": 0.0,
            "shock_contrast": 0.0,
            "words": pw,
        }
        for (a, b), pw in zip(pieces, piece_words, strict=True)
    ]
    plan_words, removed = norm["words"], []
    if rsegs and (opts.get("effects") or {}).get("remove_fillers"):
        print("Fillers: not removed with reframe segments (their pieces must stay frame-contiguous).")
    elif not rsegs:
        verbatim = (src, 0.0, ws, we, None, work_dir / "analysis")
        segs, plan_words, removed = _apply_fillers(segs, norm["words"], None, opts, "Clip", verbatim, ws)
    volume = loudness_gain(measure_loudness(Path(panels["main_src"])))
    style = {
        "font_size": norm.get("font_size"),
        "glow_preset": norm.get("glow_preset"),
        "glow_color": norm.get("glow_color"),
        "glow_strength": norm.get("glow_strength"),
        "glow_range": norm.get("glow_range"),
        "pos_y": norm.get("caption_y"),
        "anim": norm.get("animation"),
    }
    hook = str(norm.get("hook_headline") or "").replace("\\n", "\n")
    ll = plan_clip_spec(
        name,
        panels,
        segs,
        ws,
        opts,
        assets,
        hook_text=hook,
        volume=volume,
        fade_ms=12 if len(segs) > 1 and not rsegs else 0,
        draft_root=draft_root,
        overwrite=overwrite,
        close_capcut=args.close_capcut,
        style=style,
    )
    if getattr(args, "portable_drafts", False):
        ll["portable"] = True
    spec_out = work_dir / f"{name}_timeline.json"
    plan = make_clip_plan(
        name,
        name,  # the draft name is set once the draft is written
        fwd(src),
        src,
        ws,
        we,
        meta,
        opts,
        cam_box,
        game_box,
        panels,
        ll,
        plan_words,
        start,
        hook,
        spec_out,
        win_offset=ws,
    )
    _finish_effects(plan, ll, panels, removed, ws, "Clip")
    spec_out.write_text(json.dumps(ll, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"Timeline spec -> {fwd(spec_out)} (layout={opts['layout']}, preset={opts['preset'] or '-'}, volume x{volume})"
    )
    if not args.no_draft:
        plan["draft"] = build_from_spec_dict(ll).name
    if rsegs:
        plan["reframe_segments"] = rsegs  # source time; the panel above already holds them
    plan_out = work_dir / f"{name}.clip.json"
    plan_out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    if getattr(args, "render", False):
        ok = render_and_gate(plan, work_dir / f"{name}.mp4")
        _gate_summary([(name, ok)])
    return ll


def clip_stream(args) -> dict:
    """
    Stream -> story clips -> one CapCut draft + ClipPlan JSON per clip:
      0) --url: analyse the audio only (YouTube Most Replayed heatmap blended in); only the picked
         windows are downloaded
      1) story-first discovery: payoff peak -> setup/tension/payoff/reaction window, pauses trimmed
      2) per clip: window file [start-1, end+1] -> layout panels -> timeline (hook mode) -> draft
    """
    opts = resolve_options(args, {})
    if opts["hook_mode"] == "uncut-16x9-stack" and opts["layout"] == "split":
        opts["layout"] = "uncut-stack"
    if not args.no_draft:
        guard_capcut(resolve_draft_root(args.draft_root), args.close_capcut)
    assets = ensure_bundled_assets(find_capcut_local(), install=not args.no_draft)
    work_dir = Path(args.out).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    transcribe = True if args.transcribe else (False if args.no_transcribe else None)
    st = _discover_settings(args, transcribe)
    st["content"] = opts["content"]
    st["caption_style"] = opts["caption_style"]
    if opts["hook_mode"] == "split-shock":
        st["rank_by"] = "shock"

    heat = None
    if args.url:
        _, title, heat = youtube_heatmap(args.url)
        print(f"Source: {title or args.url} | heatmap points: {len(heat)}")
        src = download_audio(args.url, work_dir / "analysis")
    elif args.input:
        src = Path(args.input).resolve()
    else:
        raise ToolError(
            "Pass --input <video file> or --url <stream link>.", "example: clip-stream --input stream.mp4 --out work"
        )
    n_make = st["max_clips"]
    st["max_clips"] = 0  # every clip goes to clips.json; drafts only for the first n_make
    msgs = _chat_messages(args, work_dir / "analysis", st)
    res = discover(
        src,
        work_dir / "analysis",
        st,
        heatmap=heat,
        source_key=args.url or None,
        chat_msgs=msgs,
        url=args.url,
        title=title if args.url else src.stem,
    )
    print_clip_table(res["clips"])
    out_json = work_dir / "clips.json"
    doc = clips_document(res, args.url or fwd(src), title if args.url else src.stem, st)
    save_clips(out_json, doc, None if args.url else src)
    make = res["clips"][:n_make] if n_make else res["clips"]
    if len(make) < len(res["clips"]):
        print(
            f"{len(res['clips']) - len(make)} more clips above the threshold are in {fwd(out_json)}; "
            "pass --max-clips 0 for all."
        )
    boxes = {}  # filled once when --cam-crop is given; otherwise every clip detects its own layout
    hook_override = (args.hook_text or "").replace("\\n", "\n")
    asr = None
    if res.get("transcribed") and res.get("asr") in ("full", "regions"):
        asr = {
            "model": st.get("whisper_model") or "auto",
            "device": st.get("device") or "auto",
            "language": res.get("language"),
            "duration": res.get("duration"),
        }
    results, plans = [], []
    make_key = _make_key(opts, args, hook_override, len(make) > 1)
    # a clip that cannot be made clean (blank screen) is skipped and the next one in clips.json is used
    for idx, clip in enumerate(res["clips"]):
        if n_make and len(plans) >= n_make:
            break
        n = len(plans)
        proj_name = f"{args.prefix}_{n + 1:02d}" if len(make) > 1 else args.prefix
        done = None if args.overwrite else _made_before(res["state"], clip["rank"], make_key, work_dir, args)
        if done is not None:  # a re-run with the same inputs (e.g. after a Colab disconnect) keeps finished clips
            plan, ok = done
            print(
                f"Resuming: clip rank {clip['rank']} was already made as {plan['name']}; kept (--fresh makes it again)"
            )
            plans.append(plan)
            if args.render:
                results.append((plan["name"], ok))
                _link_outputs(doc["clips"][idx], work_dir / f"{plan['name']}.mp4", work_dir)
            continue
        try:
            plan = make_stream_clip(
                clip,
                n,
                proj_name,
                src,
                args.url,
                res["duration"],
                work_dir,
                opts,
                assets,
                boxes,
                hook_override,
                draft_root=args.draft_root,
                overwrite=args.overwrite,
                close_capcut=args.close_capcut,
                no_draft=args.no_draft,
                portable=getattr(args, "portable_drafts", False),
                asr=asr,
            )
        except SkipClip as e:
            print(f"Clip rank {clip['rank']} ({_mmss(clip['start'])}-{_mmss(clip['end'])}) skipped: {e}")
            continue
        plans.append(plan)
        if args.render:
            mp4 = work_dir / f"{plan['name']}.mp4"
            results.append((plan["name"], render_and_gate(plan, mp4)))
            _link_outputs(doc["clips"][idx], mp4, work_dir)
        res["state"].mark_rendered(clip["rank"], plan["name"], key=make_key)
    if results:
        save_clips(out_json, doc)
    return {"out_json": out_json, "doc": doc, "plans": plans, "gate": results}


def _make_key(opts: dict, args, hook_override: str, numbered: bool) -> str:
    """Fingerprint of everything that shapes a made clip, so a resume reuses a clip only when nothing changed."""
    keep = {
        "opts": opts,
        "prefix": args.prefix,
        "numbered": numbered,
        "hook": hook_override,
        "render": bool(args.render),
        "draft": not args.no_draft,
        "draft_root": str(args.draft_root or ""),
    }
    return hashlib.sha1(json.dumps(keep, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _made_before(state, rank, key: str, work_dir: Path, args):
    """(plan, gate_ok) of a clip made before with the same settings whose files are all there, else None."""
    name = state.made(rank, key)
    if not name:
        return None
    plan_p = work_dir / f"{name}.clip.json"
    try:
        plan = json.loads(plan_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = True
    if args.render:
        try:
            ok = bool(json.loads((work_dir / f"{name}.gate.json").read_text(encoding="utf-8")).get("pass"))
        except (OSError, ValueError):
            return None
        if not (work_dir / f"{name}.mp4").is_file():
            return None
    if not args.no_draft and plan.get("draft"):
        if not (Path(resolve_draft_root(args.draft_root)) / plan["draft"]).is_dir():
            return None
    return plan, ok


def cmd_clip_stream(args):
    _gate_summary(clip_stream(args)["gate"])


def make_stream_clip(
    clip: dict,
    idx: int,
    proj_name: str,
    src: Path,
    url: str,
    total_dur: float,
    work_dir: Path,
    opts: dict,
    assets: dict,
    boxes: dict,
    hook_override: str = "",
    draft_root: str = "",
    overwrite: bool = False,
    close_capcut: bool = False,
    no_draft: bool = True,
    portable: bool = False,
    asr: dict = None,
) -> dict:
    """
    One discovered clip -> window file -> layout panels -> timeline (hook mode) -> CapCut draft
    (unless no_draft) + <name>.clip.json. The layout is detected per clip (vision.classify_layout);
    boxes caches the --cam-crop boxes across clips. asr ({model, device, language, duration}, when the
    stream was transcribed): the clip window gets a second Whisper pass without the VAD filter.
    """
    if not overwrite:
        name = unique_output_name(work_dir, proj_name, None if no_draft else resolve_draft_root(draft_root))
        if name != proj_name:
            print(f"'{proj_name}' already exists; writing '{name}' instead (pass --overwrite to replace it).")
            proj_name = name
    # the window starts on the 1/30 s grid, so cut points snapped in source time stay where they are
    ws = max(0.0, math.floor((clip["start"] - 1.0) * DRAFT_FPS) / DRAFT_FPS)
    we = min(total_dur, clip["end"] + 1.0)
    if url:
        url_key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        win_file = work_dir / "windows" / f"{url_key}_{int(ws * 1000)}_{int(we * 1000)}.mp4"
        if not win_file.is_file():
            download_slice(url, ws, we, win_file)
    else:
        win_file = extract_window(src, ws, we, work_dir / "windows")
    wmeta = probe_video(win_file)
    if opts.get("cam_crop"):
        if "cam" not in boxes:
            boxes["cam"], boxes["game"] = clip_boxes(win_file, wmeta, opts)
        cam_box, game_box = boxes["cam"], boxes["game"]
        print(f"Clip {idx + 1} layout: {opts['layout']} (--cam-crop)")
    else:
        opts, cam_box, game_box, note = clip_layout(
            win_file, wmeta, opts, clip["start"] - ws, min(wmeta["dur"], clip["end"] - ws)
        )
        print(f"Clip {idx + 1} {note}")
    # --caption-style auto (default): the style comes from this clip's category
    style = clip_style(opts.get("caption_style"), clip.get("category"), opts.get("content") or "gaming")
    if style != opts.get("caption_style"):
        opts = dict(opts, caption_style=style)
    clip["caption_style"] = style
    words = clip.get("words") or []
    if asr:
        words = _rescue_clip_words(words, win_file, ws, wmeta["dur"], asr, work_dir / "analysis", idx)
    if words and opts.get("llm") not in (None, "", "none"):
        # --llm: the guarded caption fix; only near-identical spellings change, so the timings stay valid
        from streamclipper.llm import fix_caption_words

        fixed = fix_caption_words(words, opts["llm"], opts.get("llm_model") or "", opts.get("language") or "auto")
        n_fixed = sum(1 for a, b in zip(words, fixed, strict=True) if a.get("w") != b.get("w"))
        if n_fixed:
            print(f"Clip {idx + 1}: fixed {n_fixed} caption word(s)")
        words = fixed
    if opts.get("layout") == "speaker" and words:  # the active speaker switches only while someone talks
        opts = dict(opts, speech_spans=speech_spans(words, ws))
    panels = render_layout_panels(win_file, wmeta, opts, cam_box, game_box, None, None, work_dir / "panels")
    if panels.get("speaker_note"):
        print(f"Clip {idx + 1} {panels['speaker_note']}")
    if panels.get("caption_note"):
        print(f"Clip {idx + 1} {panels['caption_note']}")
    volume = loudness_gain(measure_loudness(Path(panels["main_src"])))

    def piece(a, b, kind):
        ws_in = [] if kind == "tease" else [w for w in words if w["e"] > a and w["s"] < b]
        return {"cut_start": a, "cut_end": b, "kind": kind, "words": ws_in}

    segs = []
    if opts["hook_mode"] == "tease-climax":
        segs.append(piece(*_tease_bounds(clip, words, ws, we), "tease"))
    segs += [piece(s["src_s"], s["src_e"], s["kind"]) for s in clip["segments"]]
    if panels.get("speaker_note"):  # podcasts: B-roll often fades through black between sentences
        spans = [(a + ws, b + ws) for a, b in black_frames(Path(panels["main_src"]))]
        if spans:
            segs = cut_black(segs, spans, words)
    verbatim = (win_file, ws, 0.0, wmeta["dur"], asr, work_dir / "analysis")
    segs, words, removed = _apply_fillers(segs, words, clip["peak"], opts, f"Clip {idx + 1}", verbatim, ws)
    segs = fit_timeline(segs, words)
    hook = hook_override or clip["hook_headline"]
    if opts.get("profanity_filter"):
        hook = " ".join(mask_profanity(w) for w in hook.split(" "))
    spec = plan_clip_spec(
        proj_name,
        panels,
        segs,
        ws,
        opts,
        assets,
        hook_text=hook,
        volume=volume,
        fade_ms=12,
        draft_root=draft_root,
        overwrite=overwrite,
        close_capcut=close_capcut,
        payoff_t=clip["peak"],
    )
    spec["source_window"] = {
        "source": url or fwd(src),
        "window_file": fwd(win_file),
        "start": round(ws, 3),
        "end": round(we, 3),
    }
    if portable:
        spec["portable"] = True
    spec_out = work_dir / f"{proj_name}_spec.json"
    plan = make_clip_plan(
        proj_name,
        proj_name,  # the draft name is set once the draft is written
        url or fwd(src),
        win_file,
        ws,
        we,
        wmeta,
        opts,
        cam_box,
        game_box,
        panels,
        spec,
        words,
        clip["peak"],
        hook,
        spec_out,
    )
    _finish_effects(plan, spec, panels, removed, ws, f"Clip {idx + 1}")
    spec_out.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    if not no_draft:
        plan["draft"] = build_from_spec_dict(spec).name
    plan_out = work_dir / f"{proj_name}.clip.json"
    plan_out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"Clip {idx + 1}: {_mmss(clip['start'])}-{_mmss(clip['end'])} score {clip.get('score', '-')} | "
        f"hook '{hook}' | captions {style} | {len(spec['video_track'])} pieces, {spec['expected_duration']:.2f}s | "
        f"volume x{volume} -> {fwd(plan_out)}"
    )
    return plan


FILLER_ASR = {"model": "auto", "device": "auto", "language": None, "duration": 0.0}


def _apply_fillers(
    segs: list, words: list, peak, opts: dict, label: str, verbatim: tuple = None, origin: float = None
) -> tuple:
    """
    --remove-fillers: (pieces, kept words, removed words); unchanged when the effect is off or nothing is cut.
    verbatim = (media file, its offset in source seconds, a, b, asr dict, cache dir): a Whisper pass over [a, b]
    of the file with a disfluent prompt adds the um/uh the normal transcript leaves out (asr.filler_pass).
    """
    if not (opts.get("effects") or {}).get("remove_fillers") or not words:
        return segs, words, []
    heard = words
    if verbatim:
        path, base, a, b, asr, cache = verbatim
        try:
            shifted = [dict(w, s=w["s"] - base, e=w["e"] - base) for w in words]
            merged, added, secs = filler_pass(Path(path), a, b, shifted, asr or FILLER_ASR, Path(cache))
            if added:
                heard = [dict(w, s=round(w["s"] + base, 3), e=round(w["e"] + base, 3)) for w in merged]
                print(f"{label} fillers: a verbatim Whisper pass heard {added} um/uh ({secs:.1f}s)")
        except (ToolError, RuntimeError, OSError, ValueError, ImportError) as e:
            print(f"{label} fillers: verbatim Whisper pass skipped ({str(e).splitlines()[0][:120]})")
    if heard is not words:  # the pieces get the um/uh too, so the cuts can find them
        segs = [
            dict(s, words=[w for w in heard if w["e"] > s["cut_start"] and w["s"] < s["cut_end"]])
            if s.get("kind") != "tease"
            else s
            for s in segs
        ]
    new, removed = remove_fillers(segs, heard, peak, origin)
    if not removed:
        print(f"{label} fillers: none to remove")
        return segs if heard is words else _strip_added(segs, words), words, []
    gone = {(w["w"], round(w["s"], 3)) for w in removed}
    kept = [w for w in words if (w["w"], round(w["s"], 3)) not in gone]
    shown = ", ".join(str(w["w"]).strip() for w in removed[:8])
    print(f"{label} fillers: removed {len(removed)} word(s) in {len(new) - len(segs)} extra cut(s): {shown}")
    return _strip_added(new, kept), kept, removed


def _strip_added(segs: list, words: list) -> list:
    """Pieces whose words are only the transcript's words (a protected um/uh from the verbatim pass is not
    captioned, as before)."""
    keep = {(w["w"], round(w["s"], 3)) for w in words}
    return [dict(s, words=[w for w in s.get("words") or [] if (w["w"], round(w["s"], 3)) in keep]) for s in segs]


def _finish_effects(plan: dict, spec: dict, panels: dict, removed: list, ws: float, label: str) -> None:
    """After make_clip_plan: the removed fillers on the plan, the face-safe zoom placement (cached on the plan,
    reused by the MP4 render) and the matching CapCut zoom keyframes on the timeline spec."""
    if removed:
        plan["fillers_removed"] = [
            {"w": w["w"], "s": round(w["s"] - ws, 3), "e": round(w["e"] - ws, 3)} for w in removed
        ]
    if effects_of(plan)["zoom"] == "off":
        return
    zp = plan_zoom(plan)
    plan["zoom_plan"] = zp
    canvas_h = 1920 if plan["ratio"] == "9:16" else 1080
    heights = {"main": probe_video(Path(panels["main_src"]))["height"] / canvas_h}
    if panels.get("overlay_src") and not panels.get("overlay_loop"):
        heights["overlay"] = probe_video(Path(panels["overlay_src"]))["height"] / canvas_h
    keyed = add_capcut_zoom(spec, zp, heights)
    notes = f" ({'; '.join(zp['notes'][:3])})" if zp["notes"] else ""
    print(
        f"{label} zoom {zp['level']}: {len(zp['main'])} punch-in(s) on the main panel, {len(zp['overlay'])} on "
        f"the cam panel, {keyed} CapCut segment(s) keyed{notes}"
    )


def _rescue_clip_words(words: list, win_file: Path, ws: float, win_dur: float, asr: dict, cache: Path, idx: int):
    """Words (source s) + the words a no-VAD Whisper pass over the window file hears and the stream pass missed."""
    wwords = [dict(w, s=w["s"] - ws, e=w["e"] - ws) for w in words]
    try:
        merged, added, secs = rescue_words(win_file, 0.0, win_dur, wwords, asr, cache)
    except (ToolError, RuntimeError, OSError, ValueError) as e:
        print(f"Clip {idx + 1} captions: second Whisper pass skipped ({str(e).splitlines()[0][:120]})")
        return words
    if not added:
        return words
    print(f"Clip {idx + 1} captions: +{added} words heard by a second Whisper pass without VAD ({secs:.1f}s)")
    return [dict(w, s=round(w["s"] + ws, 3), e=round(w["e"] + ws, 3)) for w in merged]


def cmd_render(args):
    """MP4 only (no CapCut needed): --spec <clip.json | README-style spec> or --clips clips.json --video SRC."""
    if args.spec:
        sp = Path(args.spec).resolve()
        if not sp.is_file():
            raise ToolError(f"Spec file not found: {sp}", "check the --spec path")
        spec = json.loads(sp.read_text(encoding="utf-8-sig"))
        if spec.get("type") == "clip_plan":
            out_dir = Path(args.out).resolve() if args.out else sp.parent
            if getattr(args, "caption_style", None) or getattr(args, "hook_style", None):
                spec = restyle_plan(spec, args.caption_style, args.hook_style)
            spec = with_effects(spec, args)
            name = spec["name"]
            if not args.overwrite:
                name = unique_output_name(out_dir, name, suffixes=RENDER_SUFFIXES)
                if name != spec["name"]:
                    print(f"{spec['name']}.mp4 already exists; writing {name}.mp4 (pass --overwrite to replace it).")
            _gate_summary([(name, render_and_gate(spec, out_dir / f"{name}.mp4"))])
            return
        if "video_track" in spec or "project_name" in spec:
            raise ToolError(
                "This is a low-level timeline spec; it has no clip plan to render.",
                "render the <name>.clip.json written next to it, or use a README-style clip spec",
            )
        args.no_draft, args.render = True, True
        build_high_level_clip(spec, sp.parent, args)
        return
    if not args.clips:
        raise ToolError(
            "Nothing to render.", "pass --spec <clip.json or clip spec> or --clips clips.json --video <stream>"
        )
    cp = Path(args.clips).resolve()
    if not cp.is_file():
        raise ToolError(f"clips.json not found: {cp}", "run discover-clips first")
    if not args.video:
        raise ToolError("--clips needs the source video.", "add --video <the stream file discover-clips analysed>")
    doc = json.loads(cp.read_text(encoding="utf-8-sig"))
    clips = sorted(doc.get("clips") or [], key=lambda c: c.get("rank", 99))
    if args.max_clips:
        clips = clips[: args.max_clips]
    if not clips:
        raise ToolError(f"No clips in {cp.name}.", "run discover-clips again with other settings")
    src = Path(args.video).resolve()
    meta = probe_video(src)
    opts = resolve_options(args, {})
    if opts["hook_mode"] == "uncut-16x9-stack" and opts["layout"] == "split":
        opts["layout"] = "uncut-stack"
    assets = ensure_bundled_assets(find_capcut_local(), install=False)
    work_dir = Path(args.out).resolve() if args.out else (cp.parent / "render")
    work_dir.mkdir(parents=True, exist_ok=True)
    boxes = {}  # filled once when --cam-crop is given; otherwise every clip detects its own layout
    hook_override = (args.hook_text or "").replace("\\n", "\n")
    state = RunState.open_existing(cp.parent / f"{cp.stem}_work" / "state.json")
    results = []
    for i, clip in enumerate(clips):
        try:
            plan = make_stream_clip(
                clip,
                i,
                f"{args.prefix}_{i + 1:02d}",
                src,
                "",
                float(doc.get("duration") or meta["dur"]),
                work_dir,
                opts,
                assets,
                boxes,
                hook_override,
                overwrite=args.overwrite,
                no_draft=True,
            )
        except SkipClip as e:
            print(f"Clip rank {clip.get('rank', i + 1)} skipped: {e}")
            continue
        name = plan["name"]
        results.append((name, render_and_gate(plan, work_dir / f"{name}.mp4")))
        _link_outputs(clip, work_dir / f"{name}.mp4", cp.parent)
        if state is not None:
            state.mark_rendered(clip.get("rank", i + 1), name)
    # the input clips.json stays as it is; the MP4 links go to a sibling <stem>_rendered.json (+ csv/html)
    out_json = cp.with_name(f"{cp.stem}_rendered.json")
    save_clips(out_json, doc, html_name=f"{cp.stem}_rendered.html")
    _gate_summary(results)


def _chat_messages(args, work_dir: Path, st: dict):
    """--chat FILE, else the chat replay of --url (when the site has one), else None. Sets st['chat']."""
    path = None
    if getattr(args, "chat", ""):
        path = Path(args.chat).resolve()
    elif getattr(args, "url", ""):
        path = fetch_chat(args.url, work_dir)
    if path is None:
        return None
    try:
        msgs = load_chat_users(path)
    except ToolError as e:
        if getattr(args, "chat", ""):
            raise
        print(f"Chat replay could not be read ({e}); scoring without chat.")
        return None
    st["chat"] = f"{path.name}:{file_tag(path)}"
    return msgs


def save_clips(out_json: Path, doc: dict, video: Path = None, html_name: str = "index.html") -> None:
    """clips.json (atomic) + clips.csv + index.html (+ previews from a local video) in the same folder."""
    csv_p, html_p = write_report(out_json.parent, doc, video, out_json.stem, html_name)
    write_json_atomic(out_json, doc)
    print(f"Report: {fwd(html_p)} | {fwd(csv_p)}")


def _link_outputs(doc_clip: dict, mp4: Path, base: Path) -> None:
    """Point a clips.json entry at its rendered MP4 and thumbnail (relative to the clips.json folder)."""
    if mp4.is_file():
        doc_clip["mp4"] = rel_path(mp4, base)
    thumb = mp4.with_name(f"{mp4.stem}_thumb.jpg")
    if thumb.is_file():
        doc_clip["thumb"] = rel_path(thumb, base)


def cmd_discover_clips(args):
    """Rank story clips (setup -> tension -> payoff -> reaction) and write clips.json, clips.csv, index.html."""
    out = Path(args.out).resolve()
    out_json = out if out.suffix.lower() == ".json" else out / "clips.json"
    work_dir = out_json.parent / f"{out_json.stem}_work"
    st = _discover_settings(args, args.transcribe)
    heat, title = None, ""
    if args.url:
        _, title, heat = youtube_heatmap(args.url)
        print(f"Source: {title or args.url} | heatmap points: {len(heat)}")
        src, label = download_audio(args.url, work_dir), args.url
    elif args.video:
        src = Path(args.video).resolve()
        label, title = fwd(src), src.stem
    else:
        raise ToolError(
            "Pass --video <file> or --url <link>.", "example: discover-clips --video stream.mp4 --out clips.json"
        )
    msgs = _chat_messages(args, work_dir, st)
    res = discover(
        src, work_dir, st, heatmap=heat, source_key=args.url or None, chat_msgs=msgs, url=args.url, title=title
    )
    doc = clips_document(res, label, title, st)
    save_clips(out_json, doc, None if args.url else src)
    print_clip_table(res["clips"])
    print(f"Saved {len(doc['clips'])} clips -> {fwd(out_json)}")


def cmd_styles(args):
    """Print the caption styles (name, look, font, words), the hook banner styles and the caption fonts."""
    if getattr(args, "preview", False):
        from streamclipper.gallery import make_gallery

        make_gallery(args.input, args.out)
        return
    print(f"CAPTION STYLES ({len(STYLES)})  --caption-style NAME  (default auto)")
    print(f"  {'NAME':<19}{'FONT':<15}{'WORDS':<7}LOOK")
    for st in STYLES.values():
        print(f"  {st.name:<19}{st.font:<15}{st.words:<7}{st.look}")
    print("  Old names: " + ", ".join(f"{a} = {b}" for a, b in STYLE_ALIASES.items()))
    print(
        "  auto (default) picks per clip: "
        + ", ".join(f"{cat} -> {name}" for cat, name in CATEGORY_STYLES.items())
        + f", podcasts (--content talk) -> {TALK_STYLE}, other -> {DEFAULT_STYLE}"
    )
    print("  See every style: streamclipper styles --preview --out style_previews (writes styles_gallery.jpg)")
    print(f"\nHOOK STYLES ({len(HOOK_STYLES)})  --hook-style NAME")
    for name, look in HOOK_STYLES.items():
        print(f"  {name:<19}{look}")
    print("\nCAPTION FONTS  --caption-font KEY (Devanagari words always use baloo)")
    for key in CAPTION_FONTS:
        print(f"  {key:<15}{FONT_FILES[key]}")
    print(
        "\nOptions: --caption-case upper|title|as-is, --words-per-caption 1-4, "
        "--caption-position auto|top|center|lower,\n  --highlight-color COLOUR, --emoji/--no-emoji, "
        "--profanity-filter. Captions use at most 2 lines."
    )


def cmd_calibrate_hud(args):
    """Profile skeleton for a game's HUD (regions + words read by OCR), or --check an edited profile."""
    from streamclipper.hud import calibrate
    from streamclipper.hud.ocr import get_recognizer
    from streamclipper.hud.profiles import load_profile

    video = Path(args.video).resolve()
    if not video.is_file():
        raise ToolError(f"Video not found: {video}", "pass a local video file with --video")
    ocr = get_recognizer()
    if args.check:
        calibrate.check(video, load_profile(args.check), ocr)
        return
    if ocr is None:
        raise ToolError(
            "calibrate-hud needs the OCR model (onnxruntime + a one-time 7.7 MB download).",
            'python -m pip install -e ".[vision]"',
        )
    times = [parse_time(x) for x in args.times.split(",") if x.strip()] if args.times else None
    out = Path(args.out).resolve() if args.out else video.with_name(f"{video.stem}_hud.json")
    calibrate.calibrate(video, load_profile(args.profile), ocr, out, times, args.every)
    print(f"Next: edit {out.name} if needed, then: streamclipper calibrate-hud --video <file> --check {out.name}")


def cmd_publish(args):
    """publish youtube|instagram|pack|schedule: post with the user's own credentials (or --dry-run)."""
    from streamclipper.publish.run import cmd

    cmd(args)


def cmd_download_vod(args):
    start, end = parse_time(args.start), parse_time(args.end)
    out = Path(args.out)
    if out.suffix.lower() != ".mp4":
        out = out / f"slice_{int(start)}s_{int(end)}s.mp4"
    download_slice(args.url, start, end, out, max_height=args.max_height)
