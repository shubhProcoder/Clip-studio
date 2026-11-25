import json

import pytest

from streamclipper.captions.ass import build_ass
from streamclipper.captions.capcut import build_word_synced_captions
from streamclipper.captions.coverage import coverage, coverage_row, spoken_words_tl
from streamclipper.layout import layout_boxes
from streamclipper.paths import TEMPLATE_DIR

FONTS = TEMPLATE_DIR / "fonts"


def _words(text, t=0.5, dur=0.3, gap=0.05):
    out = []
    for w in text.split():
        out.append({"w": w, "s": round(t, 3), "e": round(t + dur, 3)})
        t += dur + gap
    return out


def test_words_map_through_segments_and_skip_the_tease():
    words = _words("we got him now run run go", t=1.0)  # 1.0 .. 3.45 window time
    plan = {
        "words": words,
        "segments": [
            {"src_s": 2.4, "src_e": 3.5, "tl_start": 0.0, "kind": "tease"},  # payoff flash first
            {"src_s": 0.9, "src_e": 2.0, "tl_start": 1.1, "kind": "setup"},
            {"src_s": 2.4, "src_e": 3.5, "tl_start": 2.2, "kind": "payoff"},
        ],
    }
    tl = spoken_words_tl(plan)
    assert [w for w, _, _ in tl] == ["we", "got", "him", "run", "run", "go"]  # "now" (2.05-2.35) was cut
    assert tl[0] == ["we", 1.2, 1.5]  # 1.0 -> 1.1 + (1.0 - 0.9)
    assert tl[3][1] == pytest.approx(2.2 + (2.05 + 0.35 - 2.4))  # first word of the payoff, not the tease copy


def test_tokens_without_letters_are_not_spoken_words():
    plan = {
        "words": [
            {"w": "...", "s": 0.1, "e": 0.2},
            {"w": "1,000", "s": 0.3, "e": 0.5},
            {"w": "🔥", "s": 0.6, "e": 0.7},
        ],
        "segments": [{"src_s": 0.0, "src_e": 1.0, "tl_start": 0.0, "kind": "story"}],
    }
    assert [w for w, _, _ in spoken_words_tl(plan)] == ["1,000"]


def test_gap_maths_and_missing_words():
    words = [["one", 0.0, 0.3], ["two", 0.35, 0.6], ["three", 0.65, 1.0], ["four", 3.0, 3.3]]
    events = [[0.0, 0.32, "ONE TWO"], [0.32, 0.6, "ONE TWO"], [3.0, 3.3, "FOUR"]]
    r = coverage(words, events)
    assert r["total"] == 4 and r["shown"] == 3
    assert r["missing"] == [["three", 0.82]]
    assert r["max_gap"] == pytest.approx(0.4)  # 0.6 .. 1.0 inside speech; the pause 1.0 .. 3.0 is not speech
    assert r["gap_at"] == pytest.approx(0.6)


def test_matching_ignores_case_punctuation_masks_and_romanised_hindi():
    words = [["Don't", 0.0, 0.2], ["fuck", 0.25, 0.45], ["1,000", 0.5, 0.7], ["भाई", 0.75, 0.95]]
    events = [[0.0, 1.0, "DONT f*** 1000 BHAEE"]]  # --hindi-script roman writes भाई as bhaee
    assert coverage(words, events)["shown"] == 4
    assert coverage([["shit", 0.0, 0.2]], [[0.0, 1.0, "f***"]])["shown"] == 0  # wrong first letter


def _sidecar_caption(words, events):
    return {"words_tl": words, "spans": [[s, e, 0, 0, 1, 1] for s, e, _ in events], "texts": [t for *_, t in events]}


def test_row_fails_below_97_percent():
    words = [[f"w{i}", i * 0.3, i * 0.3 + 0.25] for i in range(40)]
    events = [[s, e + 0.05, w] for w, s, e in words[:-2]] + [[words[-2][1], words[-1][2], "nope"]]
    st, detail = coverage_row(_sidecar_caption(words, events))
    assert st == "FAIL" and "95.0% of 40" in detail and "missing: w38@" in detail


def test_row_fails_on_a_long_gap_even_when_every_word_shows():
    words = [["a", 0.0, 0.2], ["b", 0.25, 1.6], ["c", 1.65, 1.8]]
    events = [[0.0, 0.25, "A"], [0.8, 1.0, "B"], [1.65, 1.8, "C"]]  # "b" shows only 0.8-1.0 of its 1.35 s
    st, detail = coverage_row(_sidecar_caption(words, events))
    assert st == "FAIL"
    assert "100.0% of 3" in detail and "longest gap 0.65 s at 1.00 s" in detail
    assert coverage(words, events)["max_gap"] == pytest.approx(0.65)


def test_row_passes_and_hooks_do_not_count():
    words = [["no", 0.1, 0.3], ["way", 0.35, 0.6]]
    cap = _sidecar_caption(words, [[0.0, 2.0, None], [0.1, 0.35, "NO WAY"], [0.35, 0.65, "NO WAY"]])
    assert coverage_row(cap)[0] == "PASS"
    cap["texts"][1] = None  # only the hook banner shows "no": that word is not captioned
    assert coverage_row(cap)[0] == "FAIL"


def test_row_is_na_on_old_sidecars_and_clips_without_words():
    assert coverage_row({"spans": [], "top_min": 100})[0] == "N/A"
    assert coverage_row({"words_tl": [], "spans": [], "texts": []})[0] == "N/A"


def test_build_ass_texts_follow_the_spans(tmp_path):
    words = _words("okay so the cops are here no way bro that was fire")
    caps = build_word_synced_captions(words, 0.0, 30.0, 0.0, style="karaoke-highlight")
    plan = {
        "caption_style": "karaoke-highlight",
        "caption_opts": {"position": "auto"},
        "hook_text": "NO WAY",
        "captions": [{"text": "NO WAY", "tl_start": 0.0, "tl_end": 2.0, "layer": 1}] + caps,
        "segments": [{"src_s": 0.0, "src_e": 30.0, "tl_start": 0.0, "kind": "story"}],
        "words": words,
    }
    info = build_ass(plan, layout_boxes("9:16", "split"), tmp_path / "c.ass", FONTS)
    assert len(info["texts"]) == len(info["spans"]) and info["texts"][0] is None
    assert all("{" not in t for t in info["texts"][1:])  # plain words, no colour markup
    info["words_tl"] = spoken_words_tl(plan)
    json.dumps(info)
    st, detail = coverage_row(info)
    assert st == "PASS", detail
