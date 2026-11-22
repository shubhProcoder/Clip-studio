"""Clip scoring: 0-100 score with a hook/story/emotion/pacing/engagement breakdown, category, selection."""

import math

from streamclipper.audio import _clamp
from streamclipper.util import CATEGORIES, CATEGORY_RULES, find_talk_triggers, find_triggers, norm_token

# Weights of the breakdown components; the total is renormalised over the components that are available
# (hook and the speech part of pacing need a transcript, engagement needs chat or a YouTube heatmap).
SCORE_WEIGHTS = {"hook": 20, "story": 25, "emotion": 25, "pacing": 10, "engagement": 20, "hud": 15}
# The breakdown always has these five; 'hud' (an on-screen game event, see streamclipper.hud) is added only for
# windows with an event, so clips without one score and print exactly as before.
COMPONENTS = ("hook", "story", "emotion", "pacing", "engagement")
HUD_MIN = 0.3  # events weaker than this never make a hud component
HUD_REASON_WEIGHT = 18.0  # reason weight per unit of event strength ('WASTED on screen' leads the reason)
# weight of the face signal (facecam movement around the payoff) inside emotion; 0 = recorded but not scored.
# Checked in the unseen-stream eval (5.3.0): 0.15 helped TUNE only with changes that lowered HOLDOUT, so 0.
FACE_MOTION_WEIGHT = 0.0
# --content talk (podcasts, interviews): what is said matters more than how loud it is
TALK_WEIGHTS = {"hook": 25, "story": 35, "emotion": 15, "pacing": 15, "engagement": 10}
CONTENTS = ("gaming", "talk")
LOUD_DB = 12.0  # voice this far above the stream's usual level counts as "loud" for the rage rule
# Quality bar (passes_bar): by default only clips above it are output. --min-score replaces min_score;
# --min-score 0 lists every candidate without the bar. Tuned on the labelled eval in tests/fixtures/eval
# (tools/eval_selection.py, tests/test_eval.py). 5.3.0 re-check on 8 unseen public streams labelled blind
# (TUNE = 5 + the 2 old ones, HOLDOUT = 3): the best TUNE values (face 0.15, min_peak_db 12.0, min_reaction 0.35,
# talk_min_rate 2.5, talk_min_words 40) raised TUNE precision 0.16 -> 0.20 but lowered HOLDOUT 0.081 -> 0.078,
# so these simpler values stay (TUNE 19/118 good picks, HOLDOUT 5/62).
DEFAULT_BAR = {
    "min_score": 50.0,  # combined score
    "cue_min_score": 40.0,  # lower score floor for a scream or a milestone line ("finally", "I did it")
    "scream_db": 24.0,  # payoff this far above the stream's usual voice level counts as a scream
    "min_peak_db": 12.5,  # the payoff must be this loud, unless a milestone line explains it
    "min_setup_s": 3.0,  # payoff at least this far into the clip, so there is a setup
    "min_reaction": 0.1,  # voiced share of the ending after the payoff (scream exempt)
    "min_pacing": 0.25,  # pacing component: not mostly dead air or slow speech
    "min_words": 6,  # with a transcript: fewer words needs a scream
    "loop_min_words": 20,  # with a transcript of at least this many words ...
    "min_distinct": 0.2,  # ... at least this share of distinct words (chants, Whisper loops fail)
    "llm_min_speech_rate": 1.2,  # words/s an LLM-only pick needs (no voice peak or chat spike behind it)
    "llm_story_min": 70.0,  # an LLM pick this confident skips min_peak_db when it has real speech
    "llm_story_min_soft": 55.0,  # ... and this confident one does too when the talk is busy (llm_speech_strong)
    "llm_speech_strong": 2.0,  # words/s that make a moderately confident LLM pick real speech, not dead air
    "llm_cued_speech_rate": 0.3,  # ... or less speech when the game/chat says it mattered (a silent clutch)
    "llm_cued_hud": 0.7,  # a hud event this strong is a cue (WASTED, a kill feed, a wanted level)
    "llm_cued_chat": 0.5,  # a chat spike this strong is a cue (0..1, chat.window_score)
    "music_max_rate": 0.6,  # below this speech rate ...
    "music_min_voiced": 0.9,  # ... with the audio this full, the window is music or game sound, not talk
    "laugh_exempt": 0.5,  # laughter this strong exempts a window from the speech-rate rules
    "hud_cue": 0.9,  # an on-screen event this strong (WASTED, BUSTED, mission passed) is a cue like a milestone
}
# Achievement lines that make a quiet payoff worth a clip (whole words, lower case; Hindi as Whisper writes it).
MILESTONE_PHRASES = (
    "finally",
    "i did it",
    "we did it",
    "beat the game",
    "beat him",
    "beat it",
    "we won",
    "i won",
    "killed him",
    "we got him",
    "first try",
    "फाइनली",
    "जीत गए",
    "जीत गया",
    "मार दिया",
    "हरा दिया",
)
NO_BAR = {
    "min_score": 50.0,
    "cue_min_score": 50.0,
    "scream_db": 999.0,
    "min_peak_db": -999.0,
    "min_setup_s": 0.0,
    "min_reaction": 0.0,
    "min_pacing": 0.0,
    "min_words": 0,
    "loop_min_words": 10**9,
    "min_distinct": 0.0,
    "llm_min_speech_rate": 0.0,
    "llm_story_min_soft": 1000.0,
    "llm_speech_strong": 1000.0,
    "music_max_rate": -1.0,
    "music_min_voiced": 2.0,
    "laugh_exempt": 0.0,
    "hud_cue": 999.0,
}  # v2 behaviour (score >= 50 only): tools/eval_selection.py --no-bar
# Quality bar for --content talk: a calm podcast has no loud payoff, so the clip needs real talk
# (enough words at a normal rate) and a reason to exist: a strong line, laughter or an LLM pick.
TALK_BAR = {
    "talk": True,
    "min_score": 50.0,
    "min_setup_s": 3.0,
    "talk_min_words": 25,
    "talk_min_rate": 1.5,  # words/s
    "talk_laugh": 0.3,  # laughter this strong is a cue on its own
    "talk_min_statement": 0.5,  # statement signal (strong line) that counts as a cue
    "loop_min_words": 20,
    "min_distinct": 0.2,
}


def _terms_in(words: list) -> set:
    """Normalised single tokens and the phrases of CATEGORY_RULES found in a word list."""
    toks = [norm_token(w["w"]) for w in words]
    found = {t for t in toks if t}
    for _, terms, _ in CATEGORY_RULES:
        for term in terms:
            parts = tuple(norm_token(x) for x in term.split())
            if len(parts) > 1 and any(tuple(toks[i : i + len(parts)]) == parts for i in range(len(toks))):
                found.add(term)
    if any(t.startswith("haha") for t in toks):
        found.add("haha")
    return found


def categorize(words: list, laughter: bool = False, e_peak: float = 0.0) -> str:
    """
    First matching rule of util.CATEGORY_RULES: 'laughter' rules also match on laughter bursts alone,
    'loud' rules need the voice >= LOUD_DB above the usual level. Nothing matches -> 'reaction'.
    """
    found = _terms_in(words)
    for cat, terms, cue in CATEGORY_RULES:
        hit = any((norm_token(t) if " " not in t else t) in found for t in terms)
        if cue == "laughter" and (hit or laughter):
            return cat
        if cue == "loud" and hit and e_peak >= LOUD_DB:
            return cat
        if cue is None and hit:
            return cat
    return "reaction"


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def score_parts(
    win: dict,
    pk: dict,
    env: dict,
    words: list,
    islands: list,
    segments: list = None,
    heatmap=None,
    transcribed: bool = False,
    chat: dict = None,
    laughs: list = None,
    llm: dict = None,
    content: str = "gaming",
    hud: dict = None,
) -> tuple:
    """
    (score 0-100, breakdown {hook, story, emotion, pacing, engagement} each 0-100 or None when not
    available (+ hud for a window with an on-screen event), one-line reason, category, signals,
    comp {component: 0-1, only the available ones}).
    chat = {score 0..1, ratio} from chat.window_score;
    laughs = audio.laughter_bursts(); llm = an LLM segment (story_score, category, hook, ...).
    hud = {events: [{t, event, label, reason, strength}], face_motion, face_seen} from hud.analyze (gaming only).
    content 'talk' (podcasts): talk triggers (strong statements, reactions, amounts) instead of gaming
    slang, story from a strong line + laughter + a complete ending instead of loudness, TALK_WEIGHTS.
    """
    if content == "talk":
        return _talk_parts(win, pk, env, words, islands, segments, heatmap, transcribed, chat, laughs, llm)
    v100 = env["v100"]

    def mean_db(a, b):
        i0, i1 = max(0, int(a * 10)), min(len(v100), int(b * 10) + 1)
        return float(v100[i0:i1].mean()) if i1 > i0 else -96.0

    p, s0, e, arc = win["peak"], win["start"], win["end"], win["arc"]
    segments = segments or [{"src_s": s0, "src_e": e}]
    comp, why = {}, []  # why: (weight of the evidence, text)

    # emotion: loudness above the usual level, shock contrast, laughter/scream, trigger density
    intensity = _clamp((pk["e_peak"] - 6.0) / 14.0)
    shock = _clamp(pk["shock_contrast"] / 24.0)
    laugh = max((s for t, s in (laughs or []) if p - 3.0 <= t <= e), default=0.0)
    scream = _clamp((pk["e_peak"] - 14.0) / 8.0)
    parts = [(0.5, intensity), (0.25, shock), (0.25, max(laugh, scream))]
    why.append((12.5 * intensity, f"voice {pk['e_peak']:.0f} dB above the stream's usual level"))
    if laugh > 0:
        why.append((6.25 * laugh + 3.0, "laughter"))
    labels, trig, rate = [], 0.0, None
    if transcribed:
        n_hits = 0
        hits = find_triggers([w["w"] for w in words])
        for a, _, label in sorted(hits, key=lambda h: (words[h[0]]["s"] < arc["payoff"][0], h[0])):
            n_hits += 2 if words[a]["s"] >= arc["payoff"][0] else 1
            if label not in labels:
                labels.append(label)
        trig = _clamp(n_hits / 4.0)
        parts.append((0.25, trig))
        if labels:
            why.append((15.0 * trig, "trigger words: " + ", ".join(labels[:3])))
    face_motion = (hud or {}).get("face_motion")
    if face_motion is not None:
        parts.append((FACE_MOTION_WEIGHT, float(face_motion)))
    comp["emotion"] = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
    hud_sig = _hud_component(comp, why, hud, s0, e)

    # story: payoff louder than the setup, the streamer keeps reacting, plus the LLM's story score
    contrast = mean_db(*arc["payoff"]) - mean_db(*arc["setup"])
    arc_c = _clamp(contrast / 12.0)
    r0 = p + 0.5
    voiced_r = sum(_overlap(r0, e, x["speech_start"], x["speech_end"]) for x in islands) if e > r0 else 0.0
    reaction = _clamp(voiced_r / (e - r0)) if e > r0 else 0.0
    story = 0.6 * arc_c + 0.4 * reaction
    why.append((15.0 * arc_c, f"reaction {contrast:.0f} dB above setup"))
    why.append((10.0 * reaction * 0.5, f"keeps reacting for {reaction:.0%} of the ending"))
    if llm and llm.get("story_score") is not None:
        story = 0.5 * story + 0.5 * _clamp(float(llm["story_score"]) / 100.0)
        if llm.get("title"):
            why.append((12.0, f"story: {llm['title']}"))
    comp["story"] = story

    # hook: a trigger in the first 2 s, and how strong the hook phrase around the payoff is
    if transcribed:
        first = find_triggers([w["w"] for w in words if s0 <= w["s"] < s0 + 2.0])
        zone = find_triggers([w["w"] for w in words if p - 1.0 <= w["s"] < p + 3.0])

        def strength(hits):
            if any(" " in h[2] for h in hits):
                return 1.0
            return 0.7 if hits else 0.0

        head = max(0.2, strength(zone))
        if llm and llm.get("hook"):
            head = max(head, 0.8)
        comp["hook"] = 0.4 * strength(first) + 0.6 * head

    comp["pacing"], voiced_frac, rate = _pacing(segments, islands, words, transcribed, rate)
    hm_val = _engagement(comp, why, heatmap, chat, p)

    comp = {k: round(v, 3) for k, v in comp.items()}
    breakdown = {k: (int(round(100.0 * comp[k])) if k in comp else None) for k in COMPONENTS}
    if "hud" in comp:
        breakdown["hud"] = int(round(100.0 * comp["hud"]))
    texts = [t for w, t in sorted(why, key=lambda x: x[0], reverse=True) if w > 0.5][:3]
    reason = "; ".join(texts) or "loudest moment in this part of the stream"
    zone_words = [w for w in words if w["s"] >= p - 8.0]
    category = categorize(zone_words, laugh > 0, pk["e_peak"])
    if llm and llm.get("category") in CATEGORIES:
        category = llm["category"]
    signals = {
        "intensity": round(intensity, 3),
        "arc_contrast": round(arc_c, 3),
        "reaction": round(reaction, 3),
        "shock": round(shock, 3),
        "laughter": round(laugh, 3),
        "voiced_after_trim": round(voiced_frac, 3),
        "excitement_db": pk["e_peak"],
        "shock_contrast_db": pk["shock_contrast"],
        "contrast_db": round(contrast, 1),
    }
    if transcribed:
        signals.update(triggers=round(trig, 3), speech_rate=round(rate, 2), trigger_words=labels)
    if hm_val is not None:
        signals["heatmap"] = hm_val
    if chat is not None:
        signals.update(chat=round(chat["score"], 3), chat_ratio=round(chat["ratio"], 2))
        if chat.get("unique_ratio") is not None:
            signals.update(chat_unique_ratio=round(chat["unique_ratio"], 2), chat_spam=round(chat.get("spam", 0.0), 2))
    signals.update(hud_sig)
    return combine(comp, signals), breakdown, reason, category, signals, comp


def _hud_component(comp: dict, why: list, hud, s0: float, e: float) -> dict:
    """comp['hud'] = the strongest on-screen event in [s0, e] + 0.1 per extra one (cap 1), only when there is an
    event >= HUD_MIN; adds its reason ('WASTED on screen'). Returns the signals to add (hud, hud_reason, face)."""
    if not hud:
        return {}
    sig = {}
    if hud.get("face_motion") is not None:
        sig.update(face_motion=round(float(hud["face_motion"]), 3), face_seen=round(float(hud["face_seen"] or 0), 3))
    evs = [x for x in hud.get("events") or [] if s0 <= x["t"] <= e and x["strength"] >= HUD_MIN]
    if not evs:
        return sig
    best = max(evs, key=lambda x: x["strength"])
    comp["hud"] = min(1.0, best["strength"] + 0.1 * (len(evs) - 1))
    reason = best.get("reason") or f"{best['label']} on screen"
    why.append((HUD_REASON_WEIGHT * best["strength"], reason))
    sig.update(hud=round(best["strength"], 3), hud_reason=reason)
    return sig


def _pacing(segments: list, islands: list, words: list, transcribed: bool, rate=None) -> tuple:
    """(pacing 0-1, voiced share after trimming, speech rate or the given rate): little dead air after
    trimming, and the speech rate when there is a transcript."""
    kept = sum(max(0.0, s["src_e"] - s["src_s"]) for s in segments) or 1e-6
    voiced = sum(
        _overlap(s["src_s"], s["src_e"], x["speech_start"], x["speech_end"]) for s in segments for x in islands
    )
    voiced_frac = _clamp(voiced / kept)
    if not transcribed:
        return voiced_frac, voiced_frac, rate
    n_words = sum(1 for w in words if any(s["src_s"] <= w["s"] < s["src_e"] for s in segments))
    rate = n_words / kept
    return 0.5 * _clamp((rate - 1.0) / 2.5) + 0.5 * voiced_frac, voiced_frac, rate


def _engagement(comp: dict, why: list, heatmap, chat, p: float):
    """Sets comp['engagement'] from the chat spike and YouTube Most Replayed; returns the heatmap value."""
    hm_val = None
    if heatmap:
        mx = max(v for _, _, v in heatmap) or 1.0
        hm_val = round(next((v for a, b, v in heatmap if a <= p < b), 0.0) / mx, 3)
    eng = []
    if chat is not None:
        eng.append((0.6, chat["score"]))
        if chat["score"] > 0.05:
            unique = chat.get("unique_ratio")
            if unique is not None:
                why.append(
                    (
                        20.0 * chat["score"],
                        f"chat {chat['ratio']:.1f}x busier than usual; "
                        f"{unique:.1f}x more unique viewers",
                    )
                )
            else:
                why.append((20.0 * chat["score"], f"chat {chat['ratio']:.1f}x busier than usual"))
    if hm_val is not None:
        eng.append((0.4, hm_val))
        why.append((20.0 * hm_val * 0.8, f"Most Replayed {hm_val:.2f}"))
    if eng:
        comp["engagement"] = sum(w * v for w, v in eng) / sum(w for w, _ in eng)
    return hm_val


def _talk_parts(win, pk, env, words, islands, segments, heatmap, transcribed, chat, laughs, llm) -> tuple:
    """score_parts for --content talk (podcasts, interviews); same return value."""
    p, s0, e = win["peak"], win["start"], win["end"]
    segments = segments or [{"src_s": s0, "src_e": e}]
    comp, why = {}, []
    hits = find_talk_triggers([w["w"] for w in words]) if transcribed else []
    strong = [h for h in hits if h[3] in ("opener", "reaction")]
    labels = []
    for h in sorted(hits, key=lambda h: (h[3] not in ("opener", "reaction"), h[0])):
        if h[2] not in labels:
            labels.append(h[2])
    if strong:
        statement = 1.0
    elif any(h[3] == "number" for h in hits):
        statement = 0.5
    else:
        statement = 0.3 if hits else 0.0
    laugh = max((s for t, s in (laughs or []) if s0 <= t <= e + 1.0), default=0.0)
    r0 = p + 0.5
    voiced_r = sum(_overlap(r0, e, x["speech_start"], x["speech_end"]) for x in islands) if e > r0 else 0.0
    reaction = _clamp(voiced_r / (e - r0)) if e > r0 else 0.0
    last = words[-1]["w"].strip() if words else ""
    complete = 1.0 if last.endswith((".", "?", "!")) else 0.5
    intensity = _clamp((pk["e_peak"] - 6.0) / 14.0)
    trig = _clamp(sum(2 if h[3] in ("opener", "reaction") else 1 for h in hits) / 4.0)

    story = 0.35 * statement + 0.25 * laugh + 0.2 * reaction + 0.2 * complete
    if llm and llm.get("story_score") is not None:
        story = 0.5 * story + 0.5 * _clamp(float(llm["story_score"]) / 100.0)
        if llm.get("title"):
            why.append((14.0, f"story: {llm['title']}"))
    comp["story"] = story
    if strong:
        why.append((13.0, "strong line: " + ", ".join(h[2] for h in strong[:2])))
    elif labels:
        why.append((6.0, "talk cues: " + ", ".join(labels[:3])))
    if laugh > 0:
        why.append((10.0 * laugh + 3.0, "laughter"))
    if transcribed:
        comp["emotion"] = 0.3 * intensity + 0.35 * laugh + 0.35 * trig
        first = find_talk_triggers([w["w"] for w in words if w["s"] < s0 + 3.0])
        question = any(w["w"].strip().endswith("?") for w in words if w["s"] < s0 + 4.0)
        if question or any(h[3] in ("opener", "reaction") for h in first):
            f_val = 1.0
        else:
            f_val = 0.6 if first else 0.0
        head = max(0.3, statement)
        if llm and llm.get("hook"):
            head = max(head, 0.8)
        comp["hook"] = 0.5 * f_val + 0.5 * head
        if question:
            why.append((5.0, "opens with a question"))
    else:
        comp["emotion"] = 0.5 * intensity + 0.5 * laugh
    comp["pacing"], voiced_frac, rate = _pacing(segments, islands, words, transcribed)
    hm_val = _engagement(comp, why, heatmap, chat, p)

    comp = {k: round(v, 3) for k, v in comp.items()}
    breakdown = {k: (int(round(100.0 * comp[k])) if k in comp else None) for k in COMPONENTS}
    texts = [t for w, t in sorted(why, key=lambda x: x[0], reverse=True) if w > 0.5][:3]
    reason = "; ".join(texts) or "steady talk"
    if laugh >= 0.3:
        category = "funny"
    elif any(h[3] == "opener" for h in strong):
        category = "hot take"
    else:
        category = categorize([w for w in words if w["s"] >= p - 8.0], laugh > 0, pk["e_peak"])
    if llm and llm.get("category") in CATEGORIES:
        category = llm["category"]
    signals = {
        "content": "talk",
        "intensity": round(intensity, 3),
        "reaction": round(reaction, 3),
        "laughter": round(laugh, 3),
        "statement": round(statement, 3),
        "complete": complete,
        "voiced_after_trim": round(voiced_frac, 3),
        "excitement_db": pk["e_peak"],
        "shock_contrast_db": pk["shock_contrast"],
    }
    if transcribed:
        signals.update(triggers=round(trig, 3), speech_rate=round(rate, 2), trigger_words=labels)
    if hm_val is not None:
        signals["heatmap"] = hm_val
    if chat is not None:
        signals.update(chat=round(chat["score"], 3), chat_ratio=round(chat["ratio"], 2))
    return combine(comp, signals), breakdown, reason, category, signals, comp


def score_breakdown(*args, **kw) -> tuple:
    """(score, breakdown, reason, category, signals): score_parts without the raw components."""
    return score_parts(*args, **kw)[:5]


def combine(comp: dict, signals: dict = None) -> int:
    """
    0-100 score from the components that are available (values 0-1; missing or None ones are left out
    and the weights renormalised). signals['content'] == 'talk' uses TALK_WEIGHTS.
    """
    weights = TALK_WEIGHTS if (signals or {}).get("content") == "talk" else SCORE_WEIGHTS
    have = {k: float(v) for k, v in (comp or {}).items() if v is not None and k in weights}
    total_w = sum(weights[k] for k in have)
    if not total_w:
        return 0
    return int(round(100.0 * sum(weights[k] * v for k, v in have.items()) / total_w))


def milestone(tokens: list) -> str:
    """The first MILESTONE_PHRASES entry found in a list of normalised tokens, else ''."""
    for ph in MILESTONE_PHRASES:
        parts = ph.split()
        n = len(parts)
        if any(tokens[i : i + n] == parts for i in range(len(tokens) - n + 1)):
            return ph
    return ""


def passes_bar(cand: dict, bar: dict = None) -> tuple:
    """
    (ok, why) for one candidate against the quality bar (DEFAULT_BAR keys; missing keys = default).
    cand needs score (or comp), comp, signals (excitement_db, reaction), start, peak and text (or words).
    A clip is post-worthy when it has a setup, a payoff that stands out, a reaction that keeps going and,
    with a transcript, real words (not a chant or a Whisper loop). A scream or a milestone line
    ("finally", "I did it") lowers the score floor to cue_min_score. An LLM pick with no voice peak
    behind it (sources llm, not voice) skips min_peak_db when the model scored it >= llm_story_min
    and the window has real speech (>= llm_min_speech_rate), or when it scored >= llm_story_min_soft
    and the talk is busy (>= llm_speech_strong). Quiet-story recall is not tied to --content gaming.
    """
    comp, sig = cand.get("comp") or {}, cand.get("signals") or {}
    if (bar or {}).get("talk") or sig.get("content") == "talk":
        return _passes_talk_bar(cand, dict(TALK_BAR, **(bar or {})))
    b = dict(DEFAULT_BAR, **(bar or {}))
    score = cand.get("score")
    if score is None:
        score = combine(comp, sig)
    exc = float(sig.get("excitement_db") or 0.0)
    scream = exc >= b["scream_db"]
    transcribed = comp.get("hook") is not None
    text = cand.get("text")
    if text is None:
        text = " ".join(w["w"] for w in cand.get("words") or [])
    toks = [t for t in (norm_token(w) for w in text.split()) if t]
    cue = milestone(toks) if transcribed else ""
    hud_cue = comp.get("hud") is not None and float(sig.get("hud") or 0.0) >= b.get("hud_cue", 999.0)
    if hud_cue and not cue:  # a strong on-screen event explains the moment like a milestone line
        cue = str(sig.get("hud_reason") or "on-screen event")
    else:
        hud_cue = False
    floor = b["cue_min_score"] if (scream or cue) else b["min_score"]
    if score < floor:
        return False, f"score {score} < {floor:g}"
    if cand["peak"] - cand["start"] < b["min_setup_s"]:
        return False, f"payoff {cand['peak'] - cand['start']:.1f}s into the clip: no setup"
    rate = sig.get("speech_rate")
    llm = cand.get("llm") or {}
    srcs = set(cand.get("sources") or [])
    # A strong LLM pick explains a quiet payoff: the model judged the story, the loudness floor is only a proxy
    # for one (discovery makes these candidates from the LLM's own payoffs). It still needs real speech, unless
    # the game or the chat already says it mattered: in a tactical clutch the streamer goes quiet while aiming.
    # Only the speech-rate floor moves; the word count and the music/game-sound rules still apply.
    hud_hot = comp.get("hud") is not None and float(sig.get("hud") or 0.0) >= b["llm_cued_hud"]
    chat_hot = float(sig.get("chat") or 0.0) >= b["llm_cued_chat"]
    cued = hud_hot or chat_hot
    need_rate = b["llm_cued_speech_rate"] if cued else b["llm_min_speech_rate"]
    story = float(llm.get("story_score") or 0.0)
    soft = story >= b.get("llm_story_min_soft", 1000.0) and rate is not None and rate >= b.get("llm_speech_strong", 0.0)
    llm_pick = (
        "llm" in srcs
        and "voice" not in srcs  # no loudness peak behind it: the model's own pickup
        and rate is not None
        and rate >= need_rate
        and (story >= b["llm_story_min"] or soft)
    )

    if exc < b["min_peak_db"] and not cue and not llm_pick:
        return False, f"payoff only {exc:.0f} dB above the usual voice level"
    if float(sig.get("reaction") or 0.0) < b["min_reaction"] and not scream:
        return False, "no reaction after the payoff"
    if float(comp.get("pacing") or 0.0) < b["min_pacing"]:
        return False, "mostly dead air"
    if transcribed:
        if len(toks) < b["min_words"] and not scream:
            return False, f"only {len(toks)} words and no scream"
        if len(toks) >= b["loop_min_words"] and len(set(toks)) / len(toks) < b["min_distinct"]:
            return False, "the transcript repeats one phrase (chant or ASR loop)"
        laughing = float(sig.get("laughter") or 0.0) >= b["laugh_exempt"] > 0
        if rate is not None and not scream and not laughing and not cue:  # a milestone line explains a quiet clip
            if set(cand.get("sources") or []) == {"llm"} and rate < need_rate:
                return False, f"LLM-only pick with {rate:.2f} words/s: no real speech"
            if rate < b["music_max_rate"] and float(sig.get("voiced_after_trim") or 0.0) >= b["music_min_voiced"]:
                return False, f"music or game sound ({rate:.2f} words/s)"
    if hud_cue:
        return True, cue
    if llm_pick:
        return True, "LLM pick" + (f": {llm['title']}" if llm.get("title") else "")
    return True, ("milestone: " + cue) if cue else ("scream" if scream else "passes the quality bar")


TALK_MARK_STRENGTH = {"opener": 1.0, "reaction": 0.9, "number": 0.6}


def talk_marks(words: list, max_ahead: float = 15.0) -> list:
    """
    [(t, strength)] candidate payoff times from talk triggers, strongest first. An opener ('here's the
    thing') or an amount is paid off where its sentence ends (at most max_ahead s later); a reaction
    ('are you serious') is the payoff itself. Single talk words are too weak to start a candidate.
    """
    hits = find_talk_triggers([w["w"] for w in words])
    out = []
    for a, b, _, kind in hits:
        strength = TALK_MARK_STRENGTH.get(kind)
        if strength is None:
            continue
        if kind == "reaction":
            t = words[a]["s"]
        else:
            t = words[b]["e"]
            for w in words[b:]:
                if w["s"] - words[b]["s"] > max_ahead:
                    break
                t = w["e"]
                if w["w"].strip().endswith((".", "?", "!")):
                    break
        out.append((round(t, 2), strength))
    return sorted(out, key=lambda x: (-x[1], x[0]))


def _passes_talk_bar(cand: dict, b: dict) -> tuple:
    """passes_bar for --content talk (TALK_BAR keys): real talk plus a strong line, laughter or an LLM pick."""
    comp, sig = cand.get("comp") or {}, cand.get("signals") or {}
    score = cand.get("score")
    if score is None:
        score = combine(comp, sig)
    if score < b["min_score"]:
        return False, f"score {score} < {b['min_score']:g}"
    if cand["peak"] - cand["start"] < b["min_setup_s"]:
        return False, f"payoff {cand['peak'] - cand['start']:.1f}s into the clip: no setup"
    laugh = float(sig.get("laughter") or 0.0)
    if comp.get("hook") is None:
        return (True, "laughter") if laugh >= b["talk_laugh"] else (False, "talk mode needs a transcript")
    text = cand.get("text")
    if text is None:
        text = " ".join(w["w"] for w in cand.get("words") or [])
    toks = [t for t in (norm_token(w) for w in text.split()) if t]
    if len(toks) < b["talk_min_words"]:
        return False, f"only {len(toks)} words"
    if len(toks) >= b["loop_min_words"] and len(set(toks)) / len(toks) < b["min_distinct"]:
        return False, "the transcript repeats one phrase (ASR loop)"
    rate = sig.get("speech_rate")
    if rate is not None and rate < b["talk_min_rate"]:
        return False, f"slow talk ({rate:.2f} words/s)"
    if float(sig.get("statement") or 0.0) >= b["talk_min_statement"]:
        return True, "strong line"
    if laugh >= b["talk_laugh"]:
        return True, "laughter"
    if "llm" in (cand.get("sources") or []) or cand.get("llm"):
        return True, "LLM pick"
    return False, "no strong line, laughter or LLM pick"


WINDOW_S = 900.0  # stream-coverage window: 15 min, the LLM chunk size (llm.CHUNK_S)


def per_window(max_clips: int, n_windows: int = 1) -> int:
    """
    Clips one WINDOW_S stretch of the stream may take, so a loud 20-minute run cannot use up the whole
    clip budget and leave hour 2 empty: max(2, min(8, ceil(max_clips * 1.5 / windows) + 1)). 0 = no cap
    (every-clip mode, or no --max-clips).
    """
    if max_clips <= 0:
        return 0
    return max(2, min(8, math.ceil(max_clips * 1.5 / max(1, n_windows)) + 1))


def select_clips(
    cands: list,
    min_score: float = 50,
    max_clips: int = 0,
    key=None,
    cap: int = 0,
    window_s: float = WINDOW_S,
) -> list:
    """
    Every-clip selection: candidates with score >= min_score, best first (key, default the score),
    greedy non-overlapping; max_clips > 0 stops after that many. cap > 0 is the per-window limit
    (per_window) on candidates whose start lies in the same window_s stretch of the stream.
    Returns the picked list in rank order.
    """
    key = key or (lambda c: c["score"])
    picked, counts = [], {}
    for c in sorted((c for c in cands if c["score"] >= min_score), key=key, reverse=True):
        if any(c["start"] < q["end"] and q["start"] < c["end"] for q in picked):
            continue
        if cap:
            w = int(float(c["start"]) // window_s)
            if counts.get(w, 0) >= cap:
                continue
            counts[w] = counts.get(w, 0) + 1
        picked.append(c)
        if max_clips and len(picked) >= max_clips:
            break
    return picked
