"""Selection regression on the labelled eval (tests/fixtures/eval): precision-first with the shipped defaults."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from streamclipper.scoring import CONTENTS, DEFAULT_BAR, NO_BAR, milestone
from streamclipper.util import norm_token

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = sorted((ROOT / "tests" / "fixtures" / "eval").glob("*.json"))
OLD = [p for p in FIXTURES if p.stem.startswith("eval_stream_")]  # the two 5.2 streams (tune split)
V53 = [p for p in FIXTURES if p.stem.startswith("v53_")]  # 5.3: 5 tune + 3 held-out streams, labelled blind
_spec = importlib.util.spec_from_file_location("eval_selection", ROOT / "tools" / "eval_selection.py")
eval_selection = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_selection)


def _doc(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixtures_exist_and_hold_no_media():
    assert {p.stem for p in FIXTURES} >= {"eval_stream_a", "eval_stream_b"}
    assert {p.stem for p in V53} == {f"v53_tune_{i}" for i in range(1, 6)} | {f"v53_holdout_{i}" for i in (1, 2, 3)}
    for p in FIXTURES:
        raw = p.read_text(encoding="utf-8").lower()
        assert "http" not in raw and ".mp4" not in raw and ".m4a" not in raw and "youtu" not in raw
        assert ".jpg" not in raw and ".png" not in raw and "watch?v" not in raw
        assert not {"video_id", "title", "channel", "url"} & set(_doc(p))  # nothing that names the stream
        for c in _doc(p)["candidates"]:
            assert set(c) >= {"start", "end", "peak", "text", "comp", "signals", "category", "label", "big", "reason"}
            assert not c["big"] or c["label"]
    for p in V53:
        doc = _doc(p)
        assert doc["split"] == p.stem.split("_")[1] and doc["content"] in CONTENTS
        for c in doc["candidates"]:
            toks = c["text"].split()
            assert len(toks) <= 12 and all(re.fullmatch(r"[a-z']+", t) for t in toks), c["text"]
            assert c["tokens"]["n"] >= c["tokens"]["distinct"] >= len(set(toks))
            assert len(c["reason"]) <= 120 and set(c) >= {"sources", "hud_events"}
            assert all(set(x) == {"t", "event"} for x in c["hud_events"])


def test_bar_text_keeps_what_the_bar_reads():
    c = {"text": "finally lets go", "tokens": {"n": 30, "distinct": 9}}
    toks = [norm_token(t) for t in eval_selection.bar_text(c).split()]
    assert len(toks) == 30 and len(set(toks)) == 9 and milestone(toks) == "finally"
    assert eval_selection.bar_text({"text": "w1 w2"}) == "w1 w2"  # old fixtures keep their coded text


@pytest.mark.parametrize("path", OLD, ids=lambda p: p.stem)
def test_shipped_defaults_are_precise_and_keep_big_moments(path):
    r = eval_selection.evaluate(_doc(path), DEFAULT_BAR)
    assert r["selected"] >= 1
    assert r["precision"] >= 0.8, [c["reason"] for c in r["picked"] if not c["label"]]
    assert r["big_recall"] == 1.0, [c["reason"] for c in r["missed_big"]]


def test_the_bar_beats_score_only_selection():
    for path in OLD:
        before = eval_selection.evaluate(_doc(path), NO_BAR)
        after = eval_selection.evaluate(_doc(path), DEFAULT_BAR)
        assert after["precision"] > before["precision"]
        assert after["big_recall"] >= before["big_recall"]


def test_llm_only_window_without_speech_is_rejected():
    """User feedback 03: the LLM-only pick over game intro music (0.48 words/s) must never pass the bar."""
    from streamclipper.scoring import combine, passes_bar

    doc = _doc(ROOT / "tests" / "fixtures" / "eval" / "eval_stream_a.json")
    c = next(c for c in doc["candidates"] if c.get("sources") == ["llm"])
    c = dict(c, score=combine(c["comp"], c["signals"]))
    ok, why = passes_bar(c, DEFAULT_BAR)
    assert not ok and "LLM-only" in why
    assert passes_bar(dict(c, signals=dict(c["signals"], speech_rate=2.0)), DEFAULT_BAR)[1] != why


def _pooled(split, before=False):
    return eval_selection.pooled([eval_selection.evaluate(d, before=before) for _, d in eval_selection.load(split)])


def test_unseen_streams_regression_guard():
    """FEAT-005: 5 TUNE + 3 HOLDOUT public streams labelled blind. Tuning on TUNE (0.16 -> 0.20 precision) lowered
    HOLDOUT precision (0.081 -> 0.078), so the simpler shipped values stayed; these are the exact numbers."""
    tune, hold, hold_before = _pooled("tune"), _pooled("holdout"), _pooled("holdout", before=True)
    assert (tune["streams"], tune["selected"], tune["good"], tune["big_hit"], tune["big"]) == (7, 118, 19, 5, 6)
    assert (hold["streams"], hold["selected"], hold["good"], hold["big_hit"], hold["big"]) == (3, 62, 5, 0, 1)
    assert hold["precision"] >= hold_before["precision"] and hold["big_recall"] >= hold_before["big_recall"]


def test_before_drops_hud_and_face_signals():
    base = {"start": 0.0, "end": 20.0, "peak": 10.0, "text": "w1 w2 w3 w4 w5 w6 w7 w8", "category": "fail"}
    sig = {"excitement_db": 20.0, "reaction": 0.9, "speech_rate": 2.0, "hud": 1.0, "hud_reason": "WASTED on screen"}
    comp = {"hook": 0.5, "story": 0.6, "emotion": 0.6, "pacing": 0.6, "hud": 1.0}
    doc = {
        "candidates": [
            dict(base, comp=comp, signals=dict(sig, face_motion=1.0), label=True, big=False, reason="a",
                 sources=["voice"]),
            dict(base, start=30.0, end=50.0, peak=40.0, comp=comp, signals=sig, label=False, big=False, reason="b",
                 sources=["hud"]),
        ]
    }  # fmt: skip
    after = eval_selection.evaluate(doc, face_w=0.25)
    before = eval_selection.evaluate(doc, before=True)
    assert after["candidates"] == 2 and before["candidates"] == 1
    assert "hud" not in before["rows"][0]["comp"] and "face_motion" not in before["rows"][0]["signals"]
    assert after["rows"][0]["score"] > before["rows"][0]["score"]
