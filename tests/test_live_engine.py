from live_synth import synth
from streamclipper.live.engine import Engine, find_clip_triggers, is_ready, merge_words, shift_clip


def _clip(start, end, peak=None, score=60):
    return {"start": start, "end": end, "peak": (start + end) / 2 if peak is None else peak, "score": score}


def _feed(eng, full, vox, words, upto_s, seg_s=10.0, every=None):
    """Feed 10 s segments until upto_s; returns [(buffered_end, action, clip)] of every emit."""
    out = []
    i = int(round(eng.end / seg_s))
    while (i + 1) * seg_s <= upto_s + 1e-6:
        a, b = int(i * seg_s * 100), int((i + 1) * seg_s * 100)
        ws = [w for w in words if i * seg_s <= w["s"] < (i + 1) * seg_s]
        eng.add(i * seg_s, seg_s, full[a:b], vox[a:b], ws, i * seg_s)
        out += [(eng.end, act, c) for act, c, _ in eng.analyze()]
        i += 1
    return out


def _engine(**kw):
    eng = Engine(**kw)
    eng.transcribed = True  # words are injected, as from the live Whisper pass
    return eng


def test_spoken_clip_command_makes_a_clip_without_a_loudness_peak():
    full, vox, words = synth(240, [])  # steady talk: nothing is 6 dB above the usual voice level
    spoken = [{"w": "clip", "s": 55.0, "e": 55.2}, {"w": "that", "s": 55.25, "e": 55.5}]
    words = sorted([w for w in words if not 54.5 <= w["s"] <= 56.0] + spoken, key=lambda w: w["s"])
    eng = _engine()
    assert find_clip_triggers(words)[0][0] == 55.0
    emits = _feed(eng, full, vox, words, 240)
    assert len(emits) == 1
    action, clip = emits[0][1], emits[0][2]
    assert action == "new" and "trigger" in clip["sources"]
    assert abs(clip["peak"] - 55.0) <= 2.0


def test_creator_trigger_phrase_is_configurable():
    assert find_clip_triggers([{"w": "CLIP!", "s": 3.0, "e": 3.2}, {"w": "that", "s": 3.3, "e": 3.6}])
    assert not find_clip_triggers([{"w": "bhai", "s": 3.0, "e": 3.2}, {"w": "clip", "s": 3.3, "e": 3.6}])
    custom = find_clip_triggers(
        [{"w": "bhai", "s": 3.0, "e": 3.2}, {"w": "clip", "s": 3.3, "e": 3.6}], ("bhai clip",)
    )
    assert custom == [(3.0, "bhai clip")]


def test_readiness_rule():
    bounds = [
        {"gs": 70.0, "ge": 70.4, "pause": 0.4, "kind": "silence"},
        {"gs": 72.0, "ge": 73.0, "pause": 1.0, "kind": "silence"},
    ]
    c = _clip(40.0, 71.9, peak=65.0)
    assert not is_ready(c, bounds, buffered_end=71.95)  # end is still the buffer edge
    assert not is_ready(c, bounds, buffered_end=72.4)  # the silence is only 0.4 s so far
    assert is_ready(c, bounds, buffered_end=72.7)  # >= 0.6 s of silence buffered after the end
    sent = [{"gs": 72.0, "ge": 72.3, "pause": 0.3, "kind": "sentence"}]
    assert not is_ready(c, sent, buffered_end=72.2)
    assert is_ready(c, sent, buffered_end=72.5)  # a sentence end with speech after it
    assert is_ready(c, [], buffered_end=72.0, final=True)


def test_max_wait_20s_after_the_peak():
    c = _clip(40.0, 90.0, peak=65.0)  # reaction keeps going, no pause anywhere
    assert not is_ready(c, [], buffered_end=84.9)
    assert is_ready(c, [], buffered_end=85.0)


def test_overlap_merge_and_replace():
    eng = Engine()
    a = _clip(100.0, 130.0, score=60)
    assert eng.decide(a)[0] == "new"
    # overlaps by 20 s of 30 s (> 30%), lower score -> skipped
    assert eng.decide(_clip(110.0, 140.0, score=55))[0] == "skip"
    # small overlap (5 of 30 s) but the peaks are only 25 s apart -> the same moment (VOD: one peak per max_dur)
    assert eng.decide(_clip(125.0, 155.0, score=55))[0] == "skip"
    # small overlap (2 of 62 s) and the peaks 65 s apart -> its own clip
    assert eng.decide(_clip(128.0, 190.0, peak=180.0, score=55))[0] == "new"
    # better and overlapping a queued (not started) clip -> replaces it
    act, old = eng.decide(_clip(98.0, 128.0, score=80))
    assert act == "replace" and old.clip is a and old.cancelled
    # once a clip has started, a better overlapping one is skipped
    started = eng.moments[-1]
    started.started = True
    assert eng.decide(_clip(99.0, 129.0, score=95))[0] == "skip"


def test_rate_limit_sliding_hour():
    eng = Engine(max_per_hour=2)
    assert eng.decide(_clip(100, 120))[0] == "new"
    assert eng.decide(_clip(400, 420))[0] == "new"
    assert eng.decide(_clip(900, 920))[0] == "rate"
    assert eng.decide(_clip(3800, 3820))[0] == "new"  # the first one is more than 60 min back


def test_merge_words_and_shift():
    old = [{"w": "a", "s": 8.0, "e": 8.3}, {"w": "b", "s": 9.2, "e": 9.6}, {"w": "c", "s": 9.8, "e": 10.1}]
    new = [{"w": "b", "s": 9.21, "e": 9.6}, {"w": "c", "s": 9.81, "e": 10.2}, {"w": "d", "s": 10.5, "e": 10.9}]
    got = merge_words(old, new, cut=9.0)
    assert [w["w"] for w in got] == ["a", "b", "c", "d"]
    c = {
        "start": 1.0,
        "end": 5.0,
        "peak": 3.0,
        "arc": {"setup": [1.0, 2.0]},
        "segments": [{"src_s": 1.0, "src_e": 5.0}],
    }
    s = shift_clip(c, 100.0)
    assert s["start"] == 101.0 and s["arc"]["setup"] == [101.0, 102.0] and s["segments"][0]["src_e"] == 105.0
    assert c["start"] == 1.0  # the input is not changed


def test_rolling_analysis_emits_each_moment_once():
    full, vox, words = synth(240, [60, 150])
    eng = _engine()
    emits = _feed(eng, full, vox, words, 240)
    peaks = sorted(round(c["peak"]) for _, _, c in emits)
    assert len(emits) == 2 and abs(peaks[0] - 61) <= 2 and abs(peaks[1] - 151) <= 2
    first = min(emits, key=lambda e: e[2]["peak"])
    assert first[0] == 90.0  # nothing before 90 s are buffered
    second = max(emits, key=lambda e: e[2]["peak"])
    assert second[0] <= second[2]["peak"] + 20.0  # ready within the 20 s max wait
    for _, _, c in emits:
        assert c["start"] < c["peak"] < c["end"] and all(c["start"] <= w["s"] for w in c["words"])


def test_min_buffer_and_final_flush():
    full, vox, words = synth(60, [40])
    eng = _engine()
    assert _feed(eng, full, vox, words, 60) == []  # 60 s < 90 s buffered
    final = eng.analyze(final=True)
    assert len(final) == 1 and abs(final[0][1]["peak"] - 41) <= 2
