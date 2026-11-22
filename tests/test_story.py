import pytest

from streamclipper.clipplan import MAX_TIMELINE, fit_timeline, plan_clip_spec
from streamclipper.story import (
    CATEGORY_HOOKS,
    _inside_word,
    _snap,
    sentence_boundaries,
    snap_segments,
    story_window,
    suggest_hook,
    trim_pauses,
)


def _speech(dur: float = 120.0):
    """Sentences of 6 words (0.3 s each, 0.1 s gaps) separated by 0.9 s pauses."""
    words, islands, t, k = [], [], 2.0, 0
    while t < dur - 5:
        s0 = t
        for i in range(6):
            words.append({"w": f"word{k}" + ("." if i == 5 else ""), "s": round(t, 3), "e": round(t + 0.3, 3)})
            t += 0.4
            k += 1
        islands.append({"speech_start": round(s0, 3), "speech_end": round(t - 0.1, 3)})
        t += 0.9
    return words, islands


def test_story_window_stays_near_payoff_and_off_words():
    dur = 120.0
    words, islands = _speech(dur)
    bounds = sentence_boundaries(words, islands, dur)
    for p in (40.0, 61.3, 87.7):
        win = story_window(p, bounds, words, islands, min_dur=15.0, max_dur=30.0, target_dur=24.0, dur=dur)
        assert p - 25 <= win["start"] < p
        assert p + 2 <= win["end"] <= p + 12
        assert _inside_word(win["start"], words) is None
        assert _inside_word(win["end"], words) is None
        assert win["peak"] == p


ISLANDS = [
    {"speech_start": 10.0, "speech_end": 20.0},
    {"speech_start": 22.0, "speech_end": 30.0},
    {"speech_start": 30.5, "speech_end": 40.0},
]


def _on_grid(t: float) -> bool:
    return abs(t * 30 - round(t * 30)) < 1e-6


def test_trim_pauses_cuts_long_silence_on_frame_grid():
    segs = trim_pauses({"start": 10.0, "end": 40.0}, ISLANDS, min_pause=0.55)
    assert len(segs) == 2
    assert segs[0]["src_s"] == 10.0 and segs[-1]["src_e"] == 40.0
    assert abs(segs[0]["src_e"] - 20.15) < 0.02 and abs(segs[1]["src_s"] - 21.85) < 0.02
    for s in segs:
        assert _on_grid(s["src_s"]) and _on_grid(s["src_e"])


def test_trim_pauses_keeps_protected_payoff():
    segs = trim_pauses({"start": 10.0, "end": 40.0, "peak": 22.0}, ISLANDS, min_pause=0.55, protect=(21.0, 23.0))
    covered = [s for s in segs if s["src_s"] <= 21.0 and s["src_e"] >= 23.0]
    assert covered, segs
    assert any(s["kind"] == "payoff" for s in segs)
    for s in segs:
        assert _on_grid(s["src_s"]) and _on_grid(s["src_e"])


# --- FEAT-001: word-safe cuts, complete story windows, Shorts-safe length, hooks ---


def _edges_off_words(segs, words, a="src_s", b="src_e"):
    for s in segs:
        assert _inside_word(s[a], words) is None, (s, a)
        assert _inside_word(s[b], words) is None, (s, b)


def test_snap_segments_moves_cuts_out_of_words():
    words, islands = _speech(60.0)
    # cut points that land inside words on purpose
    segs = [
        {"src_s": 2.15, "src_e": 6.75, "kind": "setup"},
        {"src_s": 9.05, "src_e": 14.2, "kind": "payoff"},
        {"src_s": 14.25, "src_e": 20.1, "kind": "reaction"},
    ]
    out = snap_segments(segs, words, 0.0, 60.0)
    _edges_off_words(out, words)
    assert out[0]["src_s"] <= 2.0 and out[0]["src_e"] >= 6.8 - 1e-6
    assert any(s["kind"] == "payoff" for s in out)
    for s in out:
        assert abs(s["src_s"] * 30 - round(s["src_s"] * 30)) < 1e-3
    for x, y in zip(out, out[1:], strict=False):
        assert x["src_e"] < y["src_s"]  # touching pieces were merged


def test_trimmed_discovery_segments_never_cut_inside_words():
    words, islands = _speech(120.0)
    bounds = sentence_boundaries(words, islands, 120.0)
    for p in (40.0, 61.3, 87.7):
        win = story_window(p, bounds, words, islands, 15.0, 60.0, 30.0, 120.0)
        segs = snap_segments(trim_pauses(win, islands, 0.55, (p - 3.0, p + 1.5)), words, 0.0, 120.0)
        _edges_off_words(segs, words)


def _reaction_speech():
    """Setup sentences until 40 s, then a 6.5 s reaction (fast words, one 'Bro.' with a 0.15 s gap), 1.5 s silence."""
    words, islands, t, k = [], [], 2.0, 0
    while t < 38.0:
        s0 = t
        for i in range(5):
            words.append({"w": f"w{k}" + ("." if i == 4 else ""), "s": round(t, 3), "e": round(t + 0.3, 3)})
            t += 0.4
            k += 1
        islands.append({"speech_start": round(s0, 3), "speech_end": round(t - 0.1, 3)})
        t += 0.8
    s0 = t = 40.0
    while t < 46.4:
        txt = "Bro." if abs(t - 42.0) < 0.2 else f"r{k}"
        words.append({"w": txt, "s": round(t, 3), "e": round(t + 0.25, 3)})
        t += 0.25 + (0.15 if txt == "Bro." else 0.08)
        k += 1
    islands.append({"speech_start": s0, "speech_end": round(t, 3)})
    reaction_end = t
    t += 1.5
    s0 = t
    for i in range(10):
        words.append({"w": f"x{i}", "s": round(t, 3), "e": round(t + 0.3, 3)})
        t += 0.4
    islands.append({"speech_start": round(s0, 3), "speech_end": round(t, 3)})
    return words, islands, reaction_end


def test_story_window_keeps_the_whole_reaction():
    words, islands, reaction_end = _reaction_speech()
    dur = 80.0
    bounds = sentence_boundaries(words, islands, dur)
    p = 40.5
    win = story_window(p, bounds, words, islands, 15.0, 60.0, 30.0, dur)
    assert win["end"] >= reaction_end - 0.01  # not cut at the 'Bro.' breath
    assert win["end"] <= reaction_end + 1.0
    assert win["end"] >= p + 2.0


def test_story_window_starts_on_a_sentence_boundary():
    words, islands, _ = _reaction_speech()
    dur = 80.0
    bounds = sentence_boundaries(words, islands, dur)
    p = 40.5
    win = story_window(p, bounds, words, islands, 15.0, 60.0, 30.0, dur)
    assert win["start"] <= p - 4.0
    before = [w for w in words if w["e"] <= win["start"] + 1e-6]
    after = [w for w in words if w["s"] >= win["start"] - 1e-6]
    assert after and _inside_word(win["start"], words) is None
    if before:
        gap = after[0]["s"] - before[-1]["e"]
        assert before[-1]["w"].endswith(".") or gap >= 0.6


def test_timeline_with_tease_is_shorts_safe():
    words, _ = _speech(120.0)
    segs = [{"cut_start": 70.0, "cut_end": 72.0, "kind": "tease", "words": []}]
    segs += [{"cut_start": 10.0, "cut_end": 50.0, "kind": "setup", "words": []}]
    segs += [{"cut_start": 50.5, "cut_end": 69.0, "kind": "payoff", "words": []}]
    segs += [{"cut_start": 69.0, "cut_end": 72.5, "kind": "reaction", "words": []}]
    out = fit_timeline(segs, words)
    total = sum(s["cut_end"] - s["cut_start"] for s in out)
    assert total <= MAX_TIMELINE
    assert out[0]["kind"] == "tease" and out[0]["cut_end"] - out[0]["cut_start"] >= 1.2 - 1e-6
    pay = [s for s in out if s["kind"] in ("payoff", "reaction")]
    assert [(s["cut_start"], s["cut_end"]) for s in pay] == [(50.5, 69.0), (69.0, 72.5)]  # never cut
    setup = [s for s in out if s["kind"] == "setup"][0]
    assert _inside_word(setup["cut_start"], words) is None
    short = [{"cut_start": 1.0, "cut_end": 20.0, "kind": "setup"}]
    assert fit_timeline(short, words) == short


def _w(text, t0, step=0.3, gap=0.05):
    out, t = [], t0
    for tok in text.split():
        out.append({"w": tok, "s": round(t, 3), "e": round(t + step, 3)})
        t += step + gap
    return out


def test_hook_is_a_clean_phrase_near_the_payoff():
    words = _w("so we go up here and", 10.0) + _w("Oh my god, he got me.", 16.0) + _w("um the tree", 18.5)
    win = {"start": 9.5, "end": 22.0, "peak": 16.5}
    hook = suggest_hook(words, win, "reaction")
    assert hook in ("OH MY GOD, HE GOT ME", "OH MY GOD", "HE GOT ME"), hook
    assert hook.split()[-1].lower() not in ("and", "the", "to", "my")


def test_hook_falls_back_to_a_category_line():
    win = {"start": 0.0, "end": 20.0, "peak": 10.0}
    assert suggest_hook([], win, "fail") in CATEGORY_HOOKS["fail"]
    frag = _w("of the and to", 9.5, gap=0.02)
    assert suggest_hook(frag, win, "rage") in CATEGORY_HOOKS["rage"]


@pytest.mark.parametrize(
    "context, weak",
    [
        # the words around four weak hooks of a real run, as Whisper wrote them
        ("move We know if we know it never learn from", "IF WE KNOW IT NEVER"),
        ("trees? Go, go, go, go.", "TREES? GO"),
        ("in the chat yo bro. I don't", "THE CHAT YO BRO"),
        ("Oh my... Hey,", "OH MY... HEY"),
    ],
)
def test_weak_literal_hooks_fall_back_to_a_template(context, weak):
    words = _w(context, 8.0)
    win = {"start": 0.0, "end": 30.0, "peak": 10.0}
    hook = suggest_hook(words, win, "reaction")
    assert hook != weak and hook in CATEGORY_HOOKS["reaction"], hook
    assert suggest_hook(words, win, "reaction", exclude={hook}) != weak  # also when the template is taken


def test_hook_completeness_rules():
    from streamclipper.story import hook_complete

    assert hook_complete("Bro what is that?!".split())  # a complete short exclamation
    assert hook_complete("I got it bro.".split())  # a whole sentence with a content word
    assert hook_complete("his cameras broke,".split())
    assert hook_complete("Oh my god,".split())  # a trigger phrase
    for weak in ("if we know it never", "trees? go", "the chat yo bro", "oh my... hey", "go, go.", "wait wait"):
        assert not hook_complete(weak.split()), weak
    assert not hook_complete("we know what to do".split(), False, False)  # cut from inside a longer line


def test_hook_banner_shows_in_the_first_two_seconds():
    panels = {"main_src": "m.mp4", "main_pos_y": 0.0, "caption_y": -0.45, "banner_y": 0.55, "layout": "full"}
    opts = {"hook_mode": "tease-climax", "ratio": "9:16", "sfx": False, "caption_style": ""}
    for mode, segs in (
        (
            "tease-climax",
            [
                {"cut_start": 30.0, "cut_end": 31.6, "kind": "tease"},
                {"cut_start": 5.0, "cut_end": 32.0, "kind": "payoff"},
            ],
        ),
        ("story-stitch", [{"cut_start": 5.0, "cut_end": 32.0, "kind": "payoff"}]),
    ):
        spec = plan_clip_spec("t", panels, segs, 4.0, dict(opts, hook_mode=mode), {"sfx": {}}, hook_text="HE GOT ME")
        hooks = [c for c in spec["captions"] if c["text"] == "HE GOT ME"]
        assert hooks and hooks[0]["tl_start"] == 0.0 and hooks[0]["tl_end"] >= 1.2


def test_snap_puts_the_llm_boundaries_on_lines_and_sentence_ends():
    words, _ = _speech(120.0)  # sentences of 6 words: line starts every 3.3 s from 2.0, ends +2.3 s
    start, end = _snap(8.9, 30.0, words, 15.0, 45.0)
    assert start == 8.6  # nearest line start within 4 s
    assert end == 24.45  # first sentence end inside [23.6, 53.6] plus the 0.35 s tail
    assert _inside_word(start, words) is None and _inside_word(end, words) is None
    # a setup far from any line start falls back to the whole-word edge instead of drifting
    start2, _ = _snap(9.5, 30.0, words, 15.0, 45.0)
    assert start2 == 8.6
    far, _ = _snap(2.1, 30.0, words, 15.0, 45.0)
    assert far <= 2.0  # inside a word: moved to its outer edge


def test_snap_without_a_sentence_end_keeps_the_models_end_on_word_edges():
    words = [{"w": f"w{i}", "s": 10.0 + i * 0.5, "e": 10.3 + i * 0.5} for i in range(40)]  # no punctuation
    start, end = _snap(20.2, 29.6, words, 8.0, 45.0)
    assert start == 19.92  # no line start near: the word start minus the 0.08 s pad
    assert end == 29.88  # 29.6 was inside the last word: its end plus the pad
    assert _inside_word(start, words) is None and _inside_word(end, words) is None
    # the model's end is short of min_len: the fallback still honours min_len
    _, short = _snap(20.0, 21.0, words, 15.0, 45.0)
    assert short - 20.0 >= 15.0


def test_hooks_are_unique_within_a_run():
    from streamclipper.story import unique_hooks

    def clip(p, words, hook):
        return {"start": p - 10, "end": p + 5, "peak": p, "category": "reaction", "words": words, "hook_headline": hook}

    line = [("bro", 49.0, 49.3), ("what", 49.4, 49.7), ("is", 49.8, 49.9), ("that?!", 50.0, 50.4)]
    line += [("no", 51.0, 51.2), ("way.", 51.3, 51.6)]
    a = [{"w": w, "s": s, "e": e} for w, s, e in line]
    clips = [clip(10, [], "HE DID NOT SEE THAT COMING") for _ in range(3)]
    clips.append(clip(50, a, "HE DID NOT SEE THAT COMING"))
    unique_hooks(clips)
    hooks = [c["hook_headline"] for c in clips]
    assert len(set(hooks)) == len(hooks), hooks
    assert "WHAT IS THAT" in hooks[3] or "NO WAY" in hooks[3]  # the streamer's own line beats a template
    many = [clip(10, [], "") for _ in range(40)]  # more clips than templates: still never a repeat
    unique_hooks(many)
    assert len({c["hook_headline"] for c in many}) == 40
