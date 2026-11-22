import numpy as np

from streamclipper.audio import laughter_bursts
from streamclipper.scoring import COMPONENTS, SCORE_WEIGHTS, categorize, score_breakdown
from streamclipper.util import find_triggers


def _words(text: str, t0: float, step: float = 0.35) -> list:
    return [
        {"w": w, "s": round(t0 + i * step, 2), "e": round(t0 + i * step + 0.3, 2)} for i, w in enumerate(text.split())
    ]


def _case():
    """60 s envelope: quiet setup, loud payoff at 30 s; one clip window 12-35 s."""
    v100 = np.full(600, -40.0)
    v100[290:330] = -22.0
    env = {"v100": v100}
    win = {
        "start": 12.0,
        "end": 35.0,
        "peak": 30.0,
        "arc": {"setup": [12.0, 22.0], "tension": [22.0, 29.0], "payoff": [29.0, 31.0], "reaction": [31.0, 35.0]},
    }
    pk = {"t": 30.0, "e_peak": 16.0, "shock_contrast": 20.0}
    islands = [{"speech_start": a, "speech_end": a + 2.5} for a in (12.5, 16.0, 20.0, 24.0, 28.5, 31.5)]
    words = _words("okay we are going to the bank now", 12.5) + _words("no way bro that was insane", 29.6)
    segs = [{"src_s": 12.0, "src_e": 35.0}]
    return win, pk, env, words, islands, segs


def test_breakdown_ranges_and_renormalisation_without_chat():
    win, pk, env, words, islands, segs = _case()
    score, bd, reason, cat, sig = score_breakdown(win, pk, env, words, islands, segs, transcribed=True)
    assert set(bd) == set(COMPONENTS)
    assert bd["engagement"] is None  # no chat, no heatmap
    for k in ("hook", "story", "emotion", "pacing"):
        assert 0 <= bd[k] <= 100
    expect = sum(SCORE_WEIGHTS[k] * bd[k] for k in bd if bd[k] is not None) / sum(
        SCORE_WEIGHTS[k] for k in bd if bd[k] is not None
    )
    assert abs(score - expect) <= 1.0
    assert 0 <= score <= 100
    assert reason
    assert "no way" in sig["trigger_words"]
    assert cat == "reaction"


def test_chat_adds_engagement_and_raises_the_score():
    win, pk, env, words, islands, segs = _case()
    base, *_ = score_breakdown(win, pk, env, words, islands, segs, transcribed=True)
    hot, bd, reason, *_ = score_breakdown(
        win, pk, env, words, islands, segs, transcribed=True, chat={"score": 0.95, "ratio": 12.0}
    )
    assert bd["engagement"] == 95
    assert hot > base
    assert "chat 12.0x busier" in reason


def test_without_transcript_hook_is_unavailable():
    win, pk, env, _, islands, segs = _case()
    score, bd, *_ = score_breakdown(win, pk, env, [], islands, segs, transcribed=False)
    assert bd["hook"] is None and bd["engagement"] is None
    assert bd["story"] is not None and bd["emotion"] is not None and bd["pacing"] is not None
    assert 0 <= score <= 100


def test_llm_story_score_and_category_override():
    win, pk, env, words, islands, segs = _case()
    llm = {"story_score": 100, "category": "clutch", "hook": "HE ESCAPED", "title": "The great escape"}
    _, bd_plain, *_ = score_breakdown(win, pk, env, words, islands, segs, transcribed=True)
    _, bd, _, cat, _ = score_breakdown(win, pk, env, words, islands, segs, transcribed=True, llm=llm)
    assert bd["story"] > bd_plain["story"]
    assert cat == "clutch"


def test_category_rules_on_english_examples():
    def cat(text, **kw):
        return categorize(_words(text, 0.0), **kw)

    assert cat("oh he got wasted") == "fail"
    assert cat("let's go that was clutch") == "clutch"
    assert cat("GG") == "clutch"
    assert cat("W") == "clutch"
    assert cat("Wow nice") == "reaction"
    assert cat("are you kidding me", e_peak=15.0) == "rage"
    assert cat("are you kidding me", e_peak=8.0) == "reaction"
    assert cat("the cops are here") == "chaos"
    assert cat("look at this plot twist") == "reveal"
    assert cat("honestly this is my hot take") == "hot take"
    assert cat("thank you chat love you") == "wholesome"
    assert cat("I'm dead") == "funny"
    assert cat("okay so", laughter=True) == "funny"
    assert cat("okay so") == "reaction"


def test_english_triggers_whole_tokens_only():
    labels = [h[2] for h in find_triggers("bro clip that W".split())]
    assert labels == ["bro", "clip that", "w"]
    assert find_triggers("Wow, what a view".split()) == []
    assert [h[2] for h in find_triggers("are you kidding me".split())] == ["are you kidding me"]


def test_laughter_bursts_on_voice_envelope():
    vox = np.full(1000, -70.0, dtype=np.float32)  # 10 s, 10 ms hops
    for k in range(6):  # 6 bursts of 150 ms with 150 ms gaps at 2.0 s
        a = 200 + k * 30
        vox[a : a + 15] = -20.0
    vox[600:800] = -20.0  # 2 s of continuous speech: not laughter
    got = laughter_bursts({"vox": vox})
    assert len(got) == 1
    t, strength = got[0]
    assert abs(t - 2.0) < 0.05 and 0 < strength <= 1


# --content talk (podcasts)


def _talk_case(text_before: str, text_after: str, e_peak: float = 4.0):
    """Calm talk: no loud payoff (e_peak 4 dB), steady speech 20-36 s (~2 words/s), payoff line at 30 s."""
    from streamclipper.scoring import score_parts

    env = {"v100": np.full(600, -30.0)}
    win = {
        "start": 20.0,
        "end": 36.0,
        "peak": 30.0,
        "arc": {"setup": [20.0, 25.0], "tension": [25.0, 29.0], "payoff": [29.0, 31.0], "reaction": [31.0, 36.0]},
    }
    pk = {"t": 30.0, "e_peak": e_peak, "shock_contrast": 2.0}
    islands = [{"speech_start": 20.0 + 2 * k, "speech_end": 21.8 + 2 * k} for k in range(8)]
    words = _words(text_before, 20.2, 0.4) + _words(text_after, 30.0, 0.4)
    segs = [{"src_s": 20.0, "src_e": 36.0}]
    return lambda **kw: score_parts(win, pk, env, words, islands, segs, transcribed=True, **kw), win, words


def test_talk_profile_scores_a_strong_line_without_loudness():
    from streamclipper.scoring import TALK_BAR, passes_bar

    before = "so we opened the restaurant in two thousand and three and everybody said it would fail in a year"
    strong = "here's the thing nobody talks about we lost a million dollars that first year and kept going."
    flat = "and then we went home and had some dinner and watched a movie and went to bed early that night."
    score_t, win, words = _talk_case(before, strong)
    s_talk, bd, reason, cat, sig, comp = score_t(content="talk")
    s_game, *_ = score_t()
    assert sig["content"] == "talk" and sig["statement"] == 1.0
    assert "strong line" in reason and cat == "hot take"
    assert s_talk > s_game  # the gaming profile wants a loud payoff
    cand = dict(win, score=s_talk, comp=comp, signals=sig, words=words)
    assert passes_bar(cand, TALK_BAR)[0]

    score_f, win_f, words_f = _talk_case(before, flat)
    s_flat, _, _, _, sig_f, comp_f = score_f(content="talk")
    assert s_flat < s_talk
    ok, why = passes_bar(dict(win_f, score=max(s_flat, 60), comp=comp_f, signals=sig_f, words=words_f), TALK_BAR)
    assert not ok and "no strong line" in why  # steady talk without a cue is not a clip


def test_talk_bar_needs_real_talk_and_laughter_counts():
    from streamclipper.scoring import TALK_BAR, passes_bar

    score_t, win, words = _talk_case("are you serious", "no way")  # a few words only
    s, _, _, _, sig, comp = score_t(content="talk", laughs=[(32.0, 0.8)])
    ok, why = passes_bar(dict(win, score=max(s, 60), comp=comp, signals=sig, words=words), TALK_BAR)
    assert not ok and "words" in why
    before = "so I told him the sauce was fine and he tasted it and his face went completely red in a second"
    score_l, win_l, words_l = _talk_case(before, "and he just ran out of the kitchen screaming for milk.")
    s, _, _, cat, sig, comp = score_l(content="talk", laughs=[(32.0, 0.8)])
    assert cat == "funny" and sig["laughter"] == 0.8
    assert passes_bar(dict(win_l, score=max(s, 50), comp=comp, signals=sig, words=words_l), TALK_BAR) == (
        True,
        "laughter",
    )


def test_talk_llm_segment_with_wrong_times_is_dropped():
    from streamclipper.discovery import llm_fits_transcript

    words = _words("landlords win either way so fill the restaurant on a monday night", 10.0, 0.4) + _words(
        "the perfect burger is sixty percent ground beef", 180.0, 0.4
    )
    seg = {"setup_t": 14.0, "payoff_t": 18.0, "end_t": 22.0, "hook": "Ramsay's Burger", "title": "The Perfect Burger"}
    assert not llm_fits_transcript(seg, words)  # the burger talk is at 180 s, not at 14-22 s
    assert llm_fits_transcript(dict(seg, setup_t=178.0, payoff_t=181.0, end_t=184.0), words)
    assert llm_fits_transcript(dict(seg, hook="", title="Monday Night Rule"), words)


def test_strong_llm_pick_skips_the_loudness_floor_only_with_real_speech():
    from streamclipper.scoring import DEFAULT_BAR, passes_bar

    base = {
        "start": 0.0,
        "end": 30.0,
        "peak": 12.0,
        "score": 62,
        "comp": {"hook": 0.8, "story": 0.9, "emotion": 0.4, "pacing": 0.6},
        "signals": {"excitement_db": 5.0, "reaction": 0.4, "speech_rate": 2.1, "voiced_after_trim": 0.8},
        "text": "he walked in and the whole chat went quiet then the alarm went off",
        "sources": ["llm"],
        "llm": {"story_score": 84, "title": "The Alarm Trick"},
    }
    assert passes_bar(base, DEFAULT_BAR) == (True, "LLM pick: The Alarm Trick")
    assert not passes_bar(dict(base, llm={"story_score": 40, "title": "x"}), DEFAULT_BAR)[0]  # weak pick, quiet
    assert not passes_bar(dict(base, sources=["voice"]), DEFAULT_BAR)[0]  # only the model's own candidate
    quiet = dict(base, signals=dict(base["signals"], speech_rate=0.5))
    assert not passes_bar(quiet, DEFAULT_BAR)[0]  # music or game sound, not speech


def test_quiet_llm_pick_needs_less_speech_when_the_game_or_the_chat_is_hot():
    from streamclipper.scoring import DEFAULT_BAR, passes_bar

    base = {
        "start": 0.0,
        "end": 30.0,
        "peak": 12.0,
        "score": 62,
        "comp": {"hook": 0.8, "story": 0.9, "emotion": 0.4, "pacing": 0.6},
        "signals": {"excitement_db": 6.0, "reaction": 0.4, "speech_rate": 0.35, "voiced_after_trim": 0.5},
        "text": "he walked in and the whole chat went quiet then the alarm went off",
        "sources": ["llm"],
        "llm": {"story_score": 84, "title": "The Silent Clutch"},
    }
    assert not passes_bar(base, DEFAULT_BAR)[0]  # 0.35 words/s and nothing says it mattered
    hud = dict(
        base, comp=dict(base["comp"], hud=0.8), signals=dict(base["signals"], hud=0.8, hud_reason="kill feed")
    )
    assert passes_bar(hud, DEFAULT_BAR) == (True, "LLM pick: The Silent Clutch")
    # a cue-strength event (>= hud_cue) is the bar's own cue, and the reason says so
    wasted = dict(
        hud, comp=dict(hud["comp"], hud=1.0), signals=dict(hud["signals"], hud=1.0, hud_reason="WASTED on screen")
    )
    assert passes_bar(wasted, DEFAULT_BAR) == (True, "WASTED on screen")
    chat = dict(base, signals=dict(base["signals"], chat=0.8, chat_ratio=6.0))
    assert passes_bar(chat, DEFAULT_BAR)[0]  # chat proves the moment while the streamer is quiet
    assert not passes_bar(dict(hud, signals=dict(hud["signals"], hud=0.4)), DEFAULT_BAR)[0]  # weak event: no excuse
    # only the speech-rate floor moves: the word count and the music/game-sound rules are unchanged
    assert not passes_bar(dict(hud, text="he got him now"), DEFAULT_BAR)[0]  # 4 words
    loud_bed = dict(hud, signals=dict(hud["signals"], voiced_after_trim=0.95))
    assert not passes_bar(loud_bed, DEFAULT_BAR)[0]  # continuous full-band audio is music, not a clutch


def test_a_moderately_confident_llm_pick_needs_busy_talk_to_skip_the_loudness_floor():
    from streamclipper.scoring import DEFAULT_BAR, passes_bar

    base = {
        "start": 0.0,
        "end": 30.0,
        "peak": 12.0,
        "score": 62,
        "comp": {"hook": 0.8, "story": 0.9, "emotion": 0.4, "pacing": 0.6},
        "signals": {"excitement_db": 6.0, "reaction": 0.4, "speech_rate": 2.4, "voiced_after_trim": 0.6},
        "text": "he walked in and the whole chat went quiet then the alarm went off",
        "sources": ["llm"],
        "llm": {"story_score": 60, "title": "The Alarm Trick"},
    }
    assert passes_bar(base, DEFAULT_BAR) == (True, "LLM pick: The Alarm Trick")  # soft tier, real talk
    slow = dict(base, signals=dict(base["signals"], speech_rate=1.4))
    assert not passes_bar(slow, DEFAULT_BAR)[0]  # below llm_speech_strong: no excuse for a quiet payoff
    weak = dict(base, llm={"story_score": 45, "title": "x"})
    assert not passes_bar(weak, DEFAULT_BAR)[0]
    before = {**DEFAULT_BAR, "llm_story_min": 1000.0, "llm_story_min_soft": 1000.0}
    assert not passes_bar(base, before)[0]  # --before-llm behaviour


def test_talk_marks_put_the_payoff_at_the_end_of_the_statement():
    from streamclipper.scoring import talk_marks
    from streamclipper.util import find_talk_triggers

    words = _words("the truth is we never had a plan. are you serious", 0.0, 0.5)
    kinds = {h[2]: h[3] for h in find_talk_triggers([w["w"] for w in words])}
    assert kinds["the truth is"] == "opener" and kinds["are you serious"] == "reaction"
    marks = talk_marks(words)
    assert marks[0] == (words[7]["e"], 1.0)  # 'plan.' ends the opener's sentence
    assert (words[8]["s"], 0.9) in marks  # a reaction is its own payoff
    assert [h[3] for h in find_talk_triggers("we made $500 and 90% of it".split())].count("number") == 2
