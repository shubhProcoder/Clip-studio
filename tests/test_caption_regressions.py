"""One regression test per caption root cause found in the 5.3.0 caption audit (synthetic words, no media)."""

import re

import pytest

from streamclipper.asr import merge_rescued
from streamclipper.captions.ass import build_ass
from streamclipper.captions.capcut import build_word_synced_captions
from streamclipper.captions.coverage import coverage_row, spoken_words_tl
from streamclipper.layout import layout_boxes
from streamclipper.paths import TEMPLATE_DIR

FONTS = TEMPLATE_DIR / "fonts"


def _coverage(words, style, tmp_path):
    caps = build_word_synced_captions(words, 0.0, 10.0, 0.0, style=style)
    plan = {
        "caption_style": style,
        "caption_opts": {"position": "auto"},
        "captions": caps,
        "segments": [{"src_s": 0.0, "src_e": 10.0, "tl_start": 0.0, "kind": "story"}],
        "words": words,
    }
    info = build_ass(plan, layout_boxes("9:16", "split"), tmp_path / "c.ass", FONTS)
    info["words_tl"] = spoken_words_tl(plan)
    return caps, coverage_row(info)


def _shown(caps):
    text = " ".join(c["text"] for c in caps).upper()
    return re.sub(r"\{[A-Z]+:([^{}]+)\}", r"\1", text)  # colour markup off


@pytest.mark.parametrize("style", ["word-by-word", "bold-pop", "karaoke-highlight"])
def test_h1_zero_length_words_are_captioned(tmp_path, style):
    # chunk-edge word from the audit: "what to do." with "to" at 21.363-21.363
    words = [
        {"w": "I", "s": 1.0, "e": 1.2},
        {"w": "know", "s": 1.2, "e": 1.3},
        {"w": "what", "s": 1.3, "e": 1.4},
        {"w": "to", "s": 1.4, "e": 1.4},
        {"w": "do.", "s": 1.4, "e": 1.58},
    ]
    caps, (st, detail) = _coverage(words, style, tmp_path)
    assert " TO" in _shown(caps)
    assert st == "PASS", detail


@pytest.mark.parametrize("style", ["word-by-word", "bold-pop", "impact-shake"])
def test_h2_a_word_followed_within_50_ms_is_not_dropped(tmp_path, style):
    # audit: "I" 5.45 followed by "got" 5.49; "Oh" 22.80 followed by "my" 22.82
    words = [
        {"w": "wait", "s": 0.5, "e": 0.9},
        {"w": "I", "s": 1.45, "e": 1.49},
        {"w": "got", "s": 1.49, "e": 1.8},
        {"w": "him", "s": 1.85, "e": 2.1},
        {"w": "Oh", "s": 2.8, "e": 2.82},
        {"w": "my", "s": 2.82, "e": 3.0},
        {"w": "God", "s": 3.0, "e": 3.4},
    ]
    caps, (st, detail) = _coverage(words, style, tmp_path)
    text = _shown(caps)
    assert "I GOT" in text and "OH MY" in text
    assert all(c["tl_end"] - c["tl_start"] >= 0.1 for c in caps if not c.get("capcut_only"))
    assert st == "PASS", detail


@pytest.mark.parametrize("style", ["beast-bold", "bold-pop", "word-by-word", "karaoke-highlight"])
def test_h13_many_words_at_one_instant_share_a_visible_caption(tmp_path, style):
    # Linux/Colab-style clip end (Docker ubuntu:22.04 run): Whisper put "we are heading" and "to" at 17.88 and
    # "the bank now." at 18.0; the 14-char merge cap split them and gave "we are heading" a 0 s caption
    words = [
        {"w": "insane.", "s": 7.34, "e": 7.66},
        {"w": "Ok", "s": 7.66, "e": 7.68},
        {"w": "so", "s": 7.68, "e": 7.88},
        {"w": "we", "s": 7.88, "e": 7.88},
        {"w": "are", "s": 7.88, "e": 7.88},
        {"w": "heading", "s": 7.88, "e": 7.88},
        {"w": "to", "s": 7.88, "e": 8.0},
        {"w": "the", "s": 8.0, "e": 8.0},
        {"w": "bank", "s": 8.0, "e": 8.0},
        {"w": "now.", "s": 8.0, "e": 8.0},
    ]
    caps, (st, detail) = _coverage(words, style, tmp_path)
    assert "HEADING" in _shown(caps)
    assert all(c["tl_end"] - c["tl_start"] > 0.05 for c in caps if not c.get("capcut_only"))
    assert st == "PASS", detail


def test_h11_words_only_the_no_vad_pass_hears_are_merged():
    # audit: a shouted line kept 0 words in the stream pass; the no-VAD pass heard it with p >= 0.5
    words = [{"w": "okay", "s": 1.0, "e": 1.3}, {"w": "go", "s": 4.0, "e": 4.2}]
    heard = [
        {"w": "okay", "s": 1.02, "e": 1.28, "p": 0.9},  # already known: not added twice
        {"w": "Jesus", "s": 2.0, "e": 2.4, "p": 0.78},
        {"w": "Christ", "s": 2.45, "e": 2.8, "p": 0.91},
        {"w": "the", "s": 3.2, "e": 3.3, "p": 0.2},  # low probability: noise
        {"w": "go!", "s": 4.05, "e": 4.15, "p": 0.95},  # inside a known word
        {"w": "swear", "s": 0.5, "e": 5.0, "p": 0.9},  # stretched over 4.5 s of noise (seen in the audit)
    ]
    merged, added = merge_rescued(words, heard)
    assert added == 2
    assert [w["w"] for w in merged] == ["okay", "Jesus", "Christ", "go"]
    assert all("p" not in w for w in merged)


def test_h10_a_poster_face_behind_the_streamer_does_not_fail_the_face_check(tmp_path, monkeypatch):
    # audit: a full-cam clip failed captions_off_face because a poster face (BlazeFace score 0.50-0.56) sat in
    # the top caption zone while the streamer's face (4x bigger) was below it
    from streamclipper import gate

    panel = tmp_path / "panel.mp4"
    panel.write_bytes(b"")
    tl = {
        "canvas": [1080, 1920],
        "rects": {"main": [0, 0, 1080, 1920]},
        "panels": {"main": str(panel), "overlay": None},
        "segments": [{"src_s": 0.0, "src_e": 10.0, "tl_start": 0.0, "zoom": 1.0}],
        "caption": {"spans": [[0.0, 10.0, 250, 260, 830, 350]]},
    }
    streamer, poster = [0.1, 0.42, 0.85, 0.82], [0.25, 0.07, 0.54, 0.23]
    monkeypatch.setattr(gate, "detect_faces", lambda src, times, w, h: [[streamer, poster] for _ in times])
    st, detail = gate.check_captions_off_faces(panel, tl)
    assert st == "PASS", detail
    monkeypatch.setattr(gate, "detect_faces", lambda src, times, w, h: [[[0.25, 0.1, 0.6, 0.3]] for _ in times])
    assert gate.check_captions_off_faces(panel, tl)[0] == "FAIL"  # the only (main) face under the caption
