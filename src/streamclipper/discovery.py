"""Clip discovery pipeline and the clips.json document."""

import bisect
import hashlib
import importlib.util
import json
import math
import time
from pathlib import Path

from streamclipper.asr import transcribe_full, transcribe_regions
from streamclipper.audio import _mmss, audio_envelope, find_peaks, islands_from_env, laughter_bursts, peak_near
from streamclipper.captions.styles import clip_style
from streamclipper.chat import chat_signal, spikes, window_score
from streamclipper.copywriter import copy_for
from streamclipper.games import contextual_hook, detect_game, merge_tags, tags_for
from streamclipper.layout import probe_video
from streamclipper.llm import DEFAULT_MODELS, analyze_transcript
from streamclipper.quality import (
    STATIC_BUCKET_S,
    STATIC_MIN_S,
    STATIC_THRESHOLD,
    assess_static,
    quality_stage,
)
from streamclipper.scoring import (
    DEFAULT_BAR,
    TALK_BAR,
    WINDOW_S,
    passes_bar,
    per_window,
    score_parts,
    select_clips,
    talk_marks,
)
from streamclipper.state import RunState, write_json_atomic
from streamclipper.story import (
    _hook_case,
    _snap,
    sentence_boundaries,
    snap_segments,
    story_window,
    suggest_hook,
    trim_pauses,
    unique_hooks,
)
from streamclipper.util import TOOL_HINTS, ToolError, file_tag, fwd, norm_token

ASR_MODES = ("auto", "full", "regions", "none")
LLM_COPY = ("title", "description", "hashtags")


def _resolve_asr(st: dict) -> str:
    """'auto' -> full when faster-whisper is installed, else none (one notice)."""
    asr = st.get("asr") or "auto"
    have_fw = importlib.util.find_spec("faster_whisper") is not None
    if asr == "auto":
        if not have_fw:
            print(
                "faster-whisper is not installed: clips use silence boundaries and a generic hook "
                "(pip install -e .[asr])."
            )
        return "full" if have_fw else "none"
    if asr in ("full", "regions") and not have_fw:
        raise ToolError(f"--asr {asr} needs faster-whisper, which is not installed.", TOOL_HINTS["faster-whisper"])
    return asr


def _result_key(st: dict, heatmap) -> str:
    """Everything that shapes the clip list (the resume hash covers only the expensive stages)."""
    raw = json.dumps({k: v for k, v in st.items() if k != "fresh"}, sort_keys=True, default=str)
    raw += json.dumps(heatmap or [], default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


class _Near:
    """Items (sorted by key) within +-radius seconds of t, via bisect: keeps every-clip mode fast on 10 h streams."""

    def __init__(self, items: list, key: str, radius: float):
        self.items = sorted(items, key=lambda x: x[key])
        self.keys = [x[key] for x in self.items]
        self.radius = radius

    def __call__(self, t: float) -> list:
        a = bisect.bisect_left(self.keys, t - self.radius)
        b = bisect.bisect_right(self.keys, t + self.radius)
        return self.items[a:b]


def _llm_provider(st: dict) -> tuple:
    prov = st.get("llm") or "none"
    return prov, (st.get("llm_model") or DEFAULT_MODELS.get(prov, ""))


def llm_fits_transcript(seg: dict, words: list, pad: float = 5.0) -> bool:
    """
    True when the LLM's hook or title shares a content word (5+ letters) with the transcript at the
    times it gives (setup_t - pad .. end_t + pad). Small local models sometimes give the right story with
    the wrong times; such a segment would put a wrong title on another clip.
    """
    a = float(seg.get("setup_t", seg["payoff_t"])) - pad
    b = float(seg.get("end_t", seg["payoff_t"])) + pad
    near = {norm_token(w["w"]).removesuffix("s") for w in words if a <= w["s"] <= b}
    copy = f"{seg.get('hook') or ''} {seg.get('title') or ''}".replace("'s ", " ").split()
    keys = {t.removesuffix("s") for t in (norm_token(x) for x in copy) if len(t) >= 5 and t.isalpha()}
    return not keys or bool(keys & near)


def _llm_span(win: dict, seg: dict, st: dict, dur: float, words: list = None) -> dict:
    """
    The clip window the LLM described, for a candidate it created: the model's setup_t and end_t are snapped
    onto transcript line starts and sentence ends (story._snap), and those boundaries are kept instead of
    being replaced by the audio-only story window; only a window outside min_dur..max_dur, or one that would
    leave the payoff it named outside the clip, is moved. The audio still supplies the score.
    """
    p = win["peak"]
    try:
        lo = float(seg.get("setup_t"))
    except (TypeError, ValueError):
        lo = win["start"]
    try:
        hi = float(seg.get("end_t"))
    except (TypeError, ValueError):
        hi = win["end"]
    start, end = _snap(lo, hi, words, st["min_dur"], st["max_dur"]) if words else (lo, hi)
    start = max(0.0, min(start, p - 1.0))  # the story must contain the payoff it named
    end = max(end, p + 2.0)
    if end - start > st["max_dur"]:
        if p - start > end - p:  # more setup than reaction: trim the front
            start = end - st["max_dur"]
        else:
            end = start + st["max_dur"]
    if end - start < st["min_dur"]:
        end = min(dur, max(end, start + st["min_dur"]))
        start = max(0.0, min(start, end - st["min_dur"]))
    start = max(0.0, min(start, p - 1.0))
    end = max(end, p + 2.0)
    pay0, pay1 = max(start + 0.5, p - 1.0), min(end, p + 1.0)
    t_t = start + 0.6 * (pay0 - start)
    return {
        "start": start,
        "end": end,
        "peak": p,
        "arc": {"setup": [start, t_t], "tension": [t_t, pay0], "payoff": [pay0, pay1], "reaction": [pay1, end]},
    }


def build_candidates(
    env: dict,
    words: list,
    islands: list,
    laughs: list,
    peaks: list,
    chat_sig,
    chat_spikes: list,
    llm_segs: list,
    heatmap,
    st: dict,
    dur: float,
    transcribed: bool = None,
    hud=None,
    static_motion=None,
    static_ranges=None,
    static_bucket: float = STATIC_BUCKET_S,
    game=None,
) -> list:
    """
    Every candidate clip: voice peaks, chat spikes without a voice peak (at spike - 6 s), strong on-screen
    game events without a voice peak (at event - 2 s; hud = hud.analyze.Hud) and LLM payoffs outside every
    of them. Each gets a story window, trimmed segments and a score breakdown (scoring.score_parts).
    An LLM segment attaches to at most one candidate (the one whose peak is nearest its payoff). In gaming
    a payoff outside every story window becomes its own candidate at the model's payoff, cut to the setup
    and end it described (_llm_span), so the LLM finds clips instead of only decorating loudness peaks.
    st keys used: min_dur, max_dur, target_dur, min_pause. Shared by VOD discovery and live mode.
    """
    transcribed = bool(words) if transcribed is None else transcribed
    sep = max(st["max_dur"], 20.0)
    # a peak that carries a spoken clip command (live "clip that") stays a trigger candidate
    marks = [(pk, ["voice"] + (["trigger"] if pk.get("trigger") else [])) for pk in peaks]
    # chat lags the moment: a spike at t belongs to a payoff in [t-15, t+5]
    for sp in chat_spikes if "exc" in env else []:
        if not any(sp["t"] - 15.0 <= pk["t"] <= sp["t"] + 5.0 for pk, _ in marks):
            marks.append((peak_near(env, max(0.0, sp["t"] - 6.0), 2.0), ["chat"]))
    content = st.get("content") or "gaming"
    if hud is not None and content != "talk" and "exc" in env:
        # WASTED appears just after the death: the payoff is the moment before the screen
        for ev in hud.strong(DEFAULT_BAR["hud_cue"]):
            if not any(ev["t"] - 15.0 <= pk["t"] <= ev["t"] + 5.0 for pk, _ in marks):
                marks.append((peak_near(env, max(0.0, ev["t"] - 2.0), 2.0), ["hud"]))
    if content == "talk":
        llm_segs = [g for g in llm_segs or [] if llm_fits_transcript(g, words)]
        # podcasts: strong lines in the transcript are moments too, even when nobody gets loud
        for t, _ in talk_marks(words):
            if all(abs(t - pk["t"]) >= sep / 2 for pk, _ in marks):
                marks.append((peak_near(env, t, 0.5), ["text"]))
    bounds = sentence_boundaries(words, islands, dur)
    near_r = st["max_dur"] + 40.0  # story_window/trim_pauses only look this far from the payoff
    w_idx, i_idx, b_idx = _Near(words, "s", near_r), _Near(islands, "speech_start", near_r), _Near(bounds, "ge", near_r)

    def window(pk):
        p = pk["t"]
        return story_window(p, b_idx(p), w_idx(p), i_idx(p), st["min_dur"], st["max_dur"], st["target_dur"], dur)

    wins = [window(pk) for pk, _ in marks]
    attached = [None] * len(marks)
    for seg in sorted(llm_segs or [], key=lambda s: -(s.get("story_score") or 0)):
        t = seg["payoff_t"]
        hits = [i for i, w in enumerate(wins) if w["start"] <= t <= w["end"] and attached[i] is None]
        if hits:
            attached[min(hits, key=lambda i: abs(wins[i]["peak"] - t))] = seg
        elif not any(w["start"] <= t <= w["end"] for w in wins) and (
            content != "talk" or all(abs(t - w["peak"]) >= sep / 2 for w in wins)
        ):
            # a story the model found where no loudness peak is: its own candidate, at the payoff it names
            # (the excitement there still comes from the audio, so a quiet pick stays quiet). The model owns
            # the boundaries in both contents: _llm_span snaps them to the transcript instead of the audio.
            pk = dict(peak_near(env, t, 1.5), t=round(t, 2))
            marks.append((pk, ["llm"]))
            wins.append(_llm_span(window(pk), seg, st, dur, words))
            attached.append(seg)

    cands = []
    for (pk, srcs), win, seg in zip(marks, wins, attached, strict=True):
        p = pk["t"]
        isl = i_idx(p)
        near_w = w_idx(p)
        segs = snap_segments(trim_pauses(win, isl, st["min_pause"], (p - 3.0, p + 1.5)), near_w, 0.0, dur)
        cw = [w for w in near_w if w["e"] > win["start"] and w["s"] < win["end"]]
        chat = window_score(chat_sig, p) if chat_sig is not None else None
        hsig = hud.window(win) if hud is not None and content != "talk" else None
        score, breakdown, reason, category, signals, comp = score_parts(
            win, pk, env, cw, isl, segs, heatmap, transcribed, chat, laughs, seg, content, hsig
        )
        static_quality = None
        if static_ranges is not None:
            static_quality = assess_static(
                win["start"],
                win["end"],
                p,
                static_ranges,
                static_motion,
                static_bucket,
            )
            if static_quality["decision"] == "reject":
                score = 0
                reason = f"{static_quality['rejection_reason']}; {reason}"
            elif static_quality["decision"] == "penalize":
                score = max(0, score - static_quality["penalty"])
                reason = f"{static_quality['rejection_reason']}; {reason}"
        hm = signals.get("heatmap")
        if chat is not None and chat["score"] > 0.05 and "chat" not in srcs:
            srcs = srcs + ["chat"]
        candidate = {
                **win,
                "score": score,
                "comp": comp,
                "breakdown": breakdown,
                "reason": reason,
                "category": category,
                "signals": signals,
                "heatmap": hm,
                "chat": chat,
                "shock_contrast": pk["shock_contrast"],
                "segments": segs,
                "words": cw,
                "hook_headline": (
                    _hook_case(seg["hook"])
                    if seg and seg.get("hook")
                    else contextual_hook(game, category, p) if not cw
                    else suggest_hook(cw, win, category)
                ),
                "llm": seg,
                "sources": srcs + (["llm"] if seg and "llm" not in srcs else []),
            }
        if static_quality is not None:
            candidate["quality"] = static_quality
        cands.append(candidate)
        if hsig is not None:
            cands[-1]["hud_events"] = [x for x in hsig["events"] if win["start"] <= x["t"] <= win["end"]]
    if hud is not None:
        hud.save()
    return cands


def select(cands: list, st: dict, counts: bool = False):
    """
    Precision-first selection: candidates that pass the quality bar (scoring.passes_bar with
    min_score = st['min_score']; 0 = every candidate, no bar), best first, never overlapping;
    st['max_clips'] > 0 is a cap. An overlapping candidate that lost may hand its LLM copy to the clip
    that won, but every LLM segment ends up on at most one clip. Sets rank 1..n.
    Returns the picked list, or (picked, n_below_bar, n_overlapping) with counts=True.
    """
    min_score = float(st.get("min_score") if st.get("min_score") is not None else DEFAULT_BAR["min_score"])
    if min_score <= 0:
        pool = list(cands)
    else:
        base = TALK_BAR if st.get("content") == "talk" else DEFAULT_BAR
        bar = dict(base, min_score=min_score)
        pool = [c for c in cands if passes_bar(c, bar)[0]]
    key = (lambda c: (c["shock_contrast"], c["score"])) if st.get("rank_by") == "shock" else None
    max_clips = int(st.get("max_clips") or 0)
    span = max((float(c["end"]) for c in cands), default=0.0)
    cap = per_window(max_clips, math.ceil(span / WINDOW_S))
    picked = select_clips(pool, 0, max_clips, key, cap=cap)
    used = {id(c["llm"]) for c in picked if c.get("llm")}
    for c in picked:
        if c.get("llm"):
            continue
        donor = next(
            (
                d
                for d in cands
                if d.get("llm") and id(d["llm"]) not in used and d["start"] < c["end"] and c["start"] < d["end"]
            ),
            None,
        )
        if donor:
            c["llm"] = donor["llm"]
            c["sources"] = c["sources"] + ["llm"]
            used.add(id(donor["llm"]))
    for r, c in enumerate(picked, 1):
        c["rank"] = r
    unique_hooks(picked)  # no hook headline twice in one run
    if not counts:
        return picked
    n_overlap = 0 if max_clips else len(pool) - len(picked)
    return picked, len(cands) - len(pool), n_overlap


def discover(
    source: Path,
    work_dir: Path,
    st: dict,
    heatmap=None,
    source_key: str = None,
    chat_msgs=None,
    url: str = "",
    title: str = "",
) -> dict:
    """
    Story-first discovery on one audio/video file. st keys: max_clips (0 = all), min_score, min_dur,
    max_dur, target_dur, min_pause, db, asr (auto|full|regions|none), whisper_model, device, language,
    rank_by, llm (none|ollama|gemini), llm_model, chat (tag of the chat source), fresh,
    hud (auto|off|gta5|gta6|generic; missing = off), hud_profile (a profile JSON path).
    chat_msgs = [(t, text)] chat replay. url/title: the stream link (HUD frames come from a 360p video-only
    download when source is audio only) and its title (game auto-detection).
    Candidates: every voice peak >= 6 dB, chat spikes without a voice peak (at spike - 6 s), strong HUD
    events and LLM payoffs; each gets a story window and a score breakdown; the
    best non-overlapping ones with score >= min_score are returned (rank order).
    Progress goes to <work_dir>/state.json, so an interrupted run resumes (st['fresh'] starts over).
    Returns {source, duration, transcribed, language, clips, elapsed, meta, asr, state, candidates}.
    """
    t_start = time.time()
    source, work_dir = Path(source), Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    meta = probe_video(source)
    if not meta["has_audio"]:
        raise ToolError(f"{source.name} has no audio track.", "story selection needs the stream audio")
    state = RunState(
        work_dir / "state.json", source_key or f"file:{source.name}:{file_tag(source)}", st, st.get("fresh")
    )
    rkey = _result_key(st, heatmap)
    saved = state.stages.get("discovery") or {}
    saved_p = work_dir / str(saved.get("file", ""))
    if state.resumed and saved.get("key") == rkey and saved_p.is_file():
        res = json.loads(saved_p.read_text(encoding="utf-8"))
        print(f"Resuming: discovery of {source.name} already finished; using the saved result (--fresh redoes it)")
        res.update(source=source, meta=meta, state=state)
        return res
    state.set_stage("audio", fwd(source))
    print(f"Analysing audio of {source.name} ({_mmss(meta['dur'])}) ...")
    env = audio_envelope(source, work_dir)
    dur = env["dur"]
    state.set_stage("envelope", {"dur": round(dur, 2)})
    islands = islands_from_env(env, st["db"], 0.20)
    static_motion, static_ranges = None, None
    if st.get("static_analysis", True):
        static_motion, static_ranges = quality_stage(
            source,
            work_dir,
            state,
            bucket=float(st.get("static_bucket", STATIC_BUCKET_S)),
            threshold=float(st.get("static_threshold", STATIC_THRESHOLD)),
            min_seconds=float(st.get("static_min", STATIC_MIN_S)),
        )
    hud = None
    if (st.get("hud") or "off") != "off" or st.get("hud_profile"):
        from streamclipper.hud.analyze import keyframe_stage  # late import: HUD is optional

        hud = keyframe_stage(source, url, work_dir, state, st, title or source.stem, dur)
    asr = _resolve_asr(st)
    max_clips = int(st.get("max_clips") or 0)
    # every peak is a candidate, except with --asr regions (each candidate costs a transcription)
    keep = max(max_clips, 10) if asr == "regions" else 0
    peaks = find_peaks(env, keep, max(st["max_dur"], 20.0))
    sig, chat_spikes = None, []
    if chat_msgs:
        sig = chat_signal(chat_msgs, dur)
        chat_spikes = spikes(sig)
        state.set_stage("chat", {"messages": len(chat_msgs), "spikes": len(chat_spikes)})
        print(f"Chat replay: {len(chat_msgs)} messages, {len(chat_spikes)} spikes")
    provider, model = _llm_provider(st)
    # with an LLM the run continues: it may still find stories where nothing is loud (it needs the envelope)
    if not peaks and not chat_spikes and not (hud is not None and hud.strong(DEFAULT_BAR["hud_cue"])) and (
        provider == "none" or "exc" not in env
    ):
        raise ToolError(
            "No reaction peaks found (no moment is 6 dB above the stream's usual voice level).",
            "check that the commentary is audible in this video",
        )
    laughs = laughter_bursts(env, st["db"])
    do_asr = asr != "none"
    words, lang, asr_info = [], st.get("language") or "auto", None
    if asr == "full":
        words, lang, asr_info = transcribe_full(
            source,
            work_dir,
            state,
            islands,
            dur,
            model_name=st.get("whisper_model") or "auto",
            device=st.get("device") or "auto",
            language=st.get("language"),
        )
    elif asr == "regions":
        regions = [(max(0.0, pk["t"] - 30.0), min(dur, pk["t"] + 8.0)) for pk in peaks]
        print(f"Transcribing {len(regions)} candidate regions with faster-whisper '{st['whisper_model']}' ...")
        words, lang = transcribe_regions(
            source, regions, st["whisper_model"], st.get("language"), work_dir, st.get("device") or "auto"
        )
    llm_segs = []
    game_profile = detect_game(title or source.stem, manual=st.get("game"))
    if provider != "none":
        if words:
            llm_segs = analyze_transcript(
                words,
                provider,
                model,
                lang,
                state,
                work_dir,
                st.get("content"),
                game_profile["name"],
                title=title,
                min_len=st["min_dur"],
                max_len=st["max_dur"],
                peaks=peaks,
                chat_spikes=chat_spikes,
            )
        else:
            print(f"--llm {provider} needs a transcript (--asr full); skipped.")

    cands = build_candidates(
        env,
        words,
        islands,
        laughs,
        peaks,
        sig,
        chat_spikes,
        llm_segs,
        heatmap,
        st,
        dur,
        transcribed=do_asr,
        hud=hud,
        static_motion=static_motion,
        static_ranges=static_ranges,
        static_bucket=float(st.get("static_bucket", STATIC_BUCKET_S)),
        game=game_profile,
    )
    if not cands:
        raise ToolError(
            "No candidate moment found (no reaction peak, chat spike, on-screen event or LLM story).",
            "check that the commentary is audible in this video",
        )
    if hud is not None and hud.dense_n:
        print(f"HUD dense pass: {hud.dense_n} clip windows in {hud.dense_s:.0f} s")
    picked, n_bar, n_overlap = select(cands, st, counts=True)
    if not picked:
        best = max((c["score"] for c in cands), default=0)
        raise ToolError(
            f"No clip passed the quality bar (--min-score {st.get('min_score'):g}, best score {best}).",
            f"lower the threshold, e.g. --min-score {max(1, best - 5)}, or --min-score 0 to list every candidate",
        )
    elapsed = time.time() - t_start
    bar_txt = "every candidate (--min-score 0)" if not st.get("min_score") else f"score >= {st['min_score']:g}"
    print(
        f"Analysis time: {elapsed:.1f}s ({len(cands)} candidates, {len(picked)} clips passed the quality bar "
        f"[{bar_txt}], {n_bar} below the bar, {n_overlap} overlapping dropped, asr={asr})"
    )
    res = {
        "duration": dur,
        "transcribed": do_asr,
        "language": lang,
        "clips": picked,
        "elapsed": round(elapsed, 1),
        "asr": asr,
        "asr_info": asr_info,
        "candidates": len(cands),
        "chat_messages": len(chat_msgs or []),
        "llm": f"{provider}:{model}" if provider != "none" else "none",
    }
    if hud is not None:
        res["hud"] = {
            "profile": hud.name,
            "experimental": bool(hud.data.get("experimental")),
            "ocr": bool(hud.data.get("ocr")),
            "events": len(hud.events),
            "keyframe_seconds": hud.data.get("seconds"),
            "dense_seconds": round(hud.dense_s, 1),
        }
    write_json_atomic(work_dir / "discovery_result.json", res)
    state.set_stage("discovery", {"file": "discovery_result.json", "key": rkey})
    res.update(source=source, meta=meta, state=state)
    return res


def clips_document(res: dict, source_label: str, title: str, st: dict) -> dict:
    """discover() result -> the clips.json document, version 4.0 (times rounded to 0.01 s)."""

    def r2(t):
        return round(float(t), 2)

    clips, taken = [], set()
    game_profile = detect_game(title, manual=st.get("game"))
    # the game's tags are always in the post, whatever the LLM wrote in its own hashtags
    canonical = tags_for(game_profile, limit=6) if (st.get("content") or "gaming") != "talk" else []
    for c in res["clips"]:
        llm = c.get("llm") or {}
        auto = copy_for(c, taken, title, game_profile)  # plain English copy when the LLM gave nothing
        c_title = llm.get("title") if llm.get("title") and llm["title"].lower() not in taken else auto["title"]
        taken.add(c_title.lower())
        item = {
            "rank": c["rank"],
            "start": r2(c["start"]),
            "end": r2(c["end"]),
            "duration": r2(c["end"] - c["start"]),
            "duration_after_trim": r2(sum(s["src_e"] - s["src_s"] for s in c["segments"])),
            "score": c["score"],
            "breakdown": c["breakdown"],
            "reason": c["reason"],
            "category": c["category"],
            # the caption style this clip is (or will be) rendered with; auto = from the category
            "caption_style": c.get("caption_style")
            or clip_style(st.get("caption_style"), c["category"], st.get("content") or "gaming"),
            "title": c_title,
            "description": llm.get("description") or auto["description"],
            "hashtags": merge_tags(canonical, llm.get("hashtags") or auto["hashtags"]),
            "hook_headline": c["hook_headline"],
            "thumb": None,
            "mp4": None,
            "peak": r2(c["peak"]),
            "arc": {k: [r2(a), r2(b)] for k, (a, b) in c["arc"].items()},
            "sources": c.get("sources") or ["voice"],
            "heatmap": c["heatmap"],
            "chat": c.get("chat"),
            "signals": c["signals"],
            "segments": [{"src_s": r2(s["src_s"]), "src_e": r2(s["src_e"]), "kind": s["kind"]} for s in c["segments"]],
        }
        if res["transcribed"]:
            item["words"] = [{"w": w["w"], "s": r2(w["s"]), "e": r2(w["e"])} for w in c["words"]]
        item["text"] = " ".join(w["w"] for w in c["words"])
        if "hud_events" in c:
            item["hud_events"] = [
                {"t": r2(x["t"]), "event": x["event"], "label": x.get("label") or x["event"]} for x in c["hud_events"]
            ]
        if "quality" in c:
            item["quality"] = c["quality"]
        clips.append(item)
    return {
        "tool": "streamclipper",
        "version": "4.0",
        "source": source_label,
        "title": title,
        "duration": r2(res["duration"]),
        "transcribed": res["transcribed"],
        "language": res["language"],
        "analysis_seconds": res["elapsed"],
        "settings": {
            "min_score": st.get("min_score"),
            "max_clips": st.get("max_clips"),
            "min_dur": st["min_dur"],
            "max_dur": st["max_dur"],
            "min_pause": st["min_pause"],
            "asr": res.get("asr", "auto"),
            "llm": res.get("llm", "none"),
            "chat": bool(st.get("chat")),
            "hud": (res.get("hud") or {}).get("profile") or "off",
            "static_analysis": bool(st.get("static_analysis", True)),
            "static_threshold": st.get("static_threshold", STATIC_THRESHOLD),
            "static_min": st.get("static_min", STATIC_MIN_S),
            "game": game_profile["name"],
        },
        "candidates": res.get("candidates"),
        "clips": clips,
    }


def print_clip_table(clips: list):
    print(f"{'RANK':<5}{'START-END':<18}{'SCORE':<7}{'CATEGORY':<11}{'HOOK':<30}REASON")
    for c in clips:
        print(
            f"{c['rank']:<5}{_mmss(c['start']) + '-' + _mmss(c['end']):<18}{c['score']:<7}"
            f"{str(c.get('category') or '-'):<11}{c['hook_headline'][:28]:<30}{c['reason']}"
        )


def _asr_mode(asr: str, transcribe) -> str:
    """--asr wins unless an old flag says otherwise: --no-transcribe -> none, --transcribe -> full."""
    asr = asr or "auto"
    if transcribe is False:
        return "none"
    if transcribe is True and asr in ("auto", "none"):
        return "full"
    return asr


def _discover_settings(args, transcribe) -> dict:
    st = {
        "max_clips": max(0, int(getattr(args, "max_clips", 0) or 0)),
        "min_score": DEFAULT_BAR["min_score"] if getattr(args, "min_score", None) is None else float(args.min_score),
        "min_dur": float(args.min_dur),
        "max_dur": float(args.max_dur),
        "target_dur": float(args.target_dur),
        "min_pause": float(args.min_pause),
        "db": float(args.db),
        "asr": _asr_mode(getattr(args, "asr", "auto"), transcribe),
        "whisper_model": args.whisper_model or "auto",
        "device": getattr(args, "device", "auto") or "auto",
        "language": args.language,
        "rank_by": "score",
        "llm": getattr(args, "llm", "none") or "none",
        "llm_model": getattr(args, "llm_model", None) or None,
        "chat": None,
        "content": getattr(args, "content", None) or "gaming",
        "fresh": bool(getattr(args, "fresh", False)),
        "hud": getattr(args, "hud", None) or "off",
        "hud_profile": str(Path(args.hud_profile).resolve()) if getattr(args, "hud_profile", None) else None,
        "static_analysis": getattr(args, "static_analysis", "on") == "on",
        "static_bucket": float(getattr(args, "static_bucket", STATIC_BUCKET_S)),
        "static_min": float(getattr(args, "static_min", STATIC_MIN_S)),
        "static_threshold": float(getattr(args, "static_threshold", STATIC_THRESHOLD)),
        "game": str(getattr(args, "game", "") or "").strip()[:60],
    }
    if st["min_dur"] <= 0 or st["max_dur"] < st["min_dur"]:
        raise ToolError(
            f"--min-dur {st['min_dur']} / --max-dur {st['max_dur']} do not make sense.",
            "use e.g. --min-dur 15 --max-dur 45",
        )
    if not 0 <= st["min_score"] <= 100:
        raise ToolError(
            f"--min-score {st['min_score']:g} is outside 0-100.", f"use e.g. --min-score {DEFAULT_BAR['min_score']:g}"
        )
    st["target_dur"] = min(st["max_dur"], max(st["min_dur"], st["target_dur"]))
    if st["static_bucket"] <= 0:
        raise ToolError("Static bucket must be above 0 seconds.", "use e.g. --static-bucket 2")
    if st["static_min"] < 0:
        raise ToolError("Static minimum duration cannot be negative.", "use e.g. --static-min 6")
    if st["static_threshold"] < 0:
        raise ToolError("Static threshold cannot be negative.", "use e.g. --static-threshold 0.4")
    if st["hud_profile"]:
        from streamclipper.hud.profiles import load_profile

        load_profile(st["hud_profile"])  # a broken profile stops here, before any analysis
    return st
