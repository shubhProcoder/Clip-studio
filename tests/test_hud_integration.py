import argparse

import numpy as np

from streamclipper.audio import find_peaks
from streamclipper.cli import build_parser
from streamclipper.discovery import _discover_settings, build_candidates, clips_document
from streamclipper.hud import analyze
from streamclipper.scoring import DEFAULT_BAR, passes_bar, score_parts
from test_scoring import _case

WASTED = {"t": 31.0, "event": "wasted", "label": "WASTED", "reason": "WASTED on screen", "strength": 1.0}


def test_window_with_wasted_gets_the_hud_component_and_reason():
    win, pk, env, words, islands, segs = _case()
    base = score_parts(win, pk, env, words, islands, segs, transcribed=True)
    got = score_parts(win, pk, env, words, islands, segs, transcribed=True, hud={"events": [WASTED]})
    score, bd, reason, _, sig, comp = got
    assert comp["hud"] == 1.0 and bd["hud"] == 100
    assert reason.startswith("WASTED on screen")
    assert sig["hud"] == 1.0 and sig["hud_reason"] == "WASTED on screen"
    assert score > base[0]
    assert "hud" not in base[5] and "hud" not in base[1]


def test_window_without_events_scores_exactly_as_before():
    win, pk, env, words, islands, segs = _case()
    base = score_parts(win, pk, env, words, islands, segs, transcribed=True)
    outside = dict(WASTED, t=80.0)  # after the window
    weak = dict(WASTED, t=31.0, strength=0.2)  # below HUD_MIN
    for hud in (None, {"events": []}, {"events": [outside, weak]}):
        got = score_parts(win, pk, env, words, islands, segs, transcribed=True, hud=hud)
        assert got[:3] == base[:3] and got[5] == base[5]


def test_more_events_raise_the_component_and_face_signal_is_recorded():
    win, pk, env, words, islands, segs = _case()
    stars = {"t": 20.0, "event": "wanted_3plus", "label": "Wanted", "reason": "wanted level 3+", "strength": 0.7}
    hud = {"events": [stars, dict(stars, t=25.0)], "face_motion": 0.6, "face_seen": 0.9}
    _, _, _, _, sig, comp = score_parts(win, pk, env, words, islands, segs, transcribed=True, hud=hud)
    assert comp["hud"] == 0.8 and sig["face_motion"] == 0.6 and sig["face_seen"] == 0.9


def test_strong_event_is_a_cue_in_the_quality_bar():
    cand = {
        "start": 0.0,
        "peak": 12.0,
        "score": 45,
        "comp": {"hook": 0.5, "story": 0.5, "emotion": 0.4, "pacing": 0.5},
        "signals": {"excitement_db": 8.0, "reaction": 0.5, "speech_rate": 2.0, "voiced_after_trim": 0.5},
        "text": "okay okay we are going in now and then this happens right here",
    }
    assert not passes_bar(cand, DEFAULT_BAR)[0]  # quiet payoff, score below 50
    hud = dict(
        cand, comp=dict(cand["comp"], hud=1.0), signals=dict(cand["signals"], hud=1.0, hud_reason="WASTED on screen")
    )
    ok, why = passes_bar(hud, DEFAULT_BAR)
    assert ok and why == "WASTED on screen"
    weak = dict(hud, signals=dict(hud["signals"], hud=0.7))
    assert not passes_bar(weak, DEFAULT_BAR)[0]


class FakeHud:
    def __init__(self, events):
        self.events = events
        self.windows = []

    def strong(self, m):
        return [e for e in self.events if e["strength"] >= m]

    def window(self, win):
        self.windows.append(win)
        return {"events": self.events, "face_motion": None, "face_seen": None}

    def save(self):
        pass


def _quiet_env(dur=200.0):
    n = int(dur * 100)
    env = {"vox": np.full(n, -40.0), "full": np.full(n, -35.0), "dur": dur}
    env["vox"][::7] = -38.0
    assert find_peaks(env, 0, 20.0) == []  # no voice peak anywhere
    return env


ST = {"min_dur": 15.0, "max_dur": 60.0, "target_dur": 30.0, "min_pause": 0.55}


def test_strong_event_without_a_voice_peak_adds_a_candidate():
    env = _quiet_env()
    islands = [{"speech_start": a, "speech_end": a + 3.0} for a in range(5, 195, 6)]
    ev = dict(WASTED, t=120.0)
    hud = FakeHud([ev])
    cands = build_candidates(env, [], islands, [], [], None, [], [], None, ST, 200.0, transcribed=False, hud=hud)
    assert len(cands) == 1 and cands[0]["sources"] == ["hud"]
    c = cands[0]
    assert abs(c["peak"] - 118.0) <= 2.0 and c["start"] <= 120.0 <= c["end"]
    assert c["comp"]["hud"] == 1.0 and c["hud_events"] == [ev] and "WASTED on screen" in c["reason"]
    assert build_candidates(env, [], islands, [], [], None, [], [], None, ST, 200.0, transcribed=False) == []
    weak = FakeHud([dict(ev, strength=0.7)])
    assert build_candidates(env, [], islands, [], [], None, [], [], None, ST, 200.0, transcribed=False, hud=weak) == []


def _args(**kw):
    base = dict(min_dur=15, max_dur=60, target_dur=30, min_pause=0.55, db=-48, whisper_model="auto", language=None)
    return argparse.Namespace(**{**base, **kw})


def test_hud_flags_and_settings(tmp_path):
    ap = build_parser()
    assert ap.parse_args(["discover-clips", "--video", "s.mp4", "--out", "o"]).hud == "auto"
    assert ap.parse_args(["clip-stream", "--input", "s.mp4", "--out", "o", "--hud", "gta6"]).hud == "gta6"
    assert ap.parse_args(["discover-clips", "--video", "s.mp4", "--out", "o", "--hud", "off"]).hud == "off"
    assert _discover_settings(_args(), None)["hud"] == "off"  # callers without the flag: HUD off
    assert _discover_settings(_args(hud="gta5"), None)["hud"] == "gta5"
    import pytest

    from streamclipper.util import ToolError

    bad = tmp_path / "bad.json"
    bad.write_text('{"name": "x", "events": []}', encoding="utf-8")
    with pytest.raises(ToolError):
        _discover_settings(_args(hud_profile=str(bad)), None)


def test_auto_without_the_vision_extra_is_off_with_one_notice(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(analyze, "ocr_available", lambda: False)
    analyze._NOTICE.clear()
    for _ in range(2):
        assert analyze.keyframe_stage(tmp_path / "s.mp4", "", tmp_path, None, {"hud": "auto"}, "GTA V", 60.0) is None
    assert capsys.readouterr().out.count("vision extra") == 1
    assert analyze.keyframe_stage(tmp_path / "s.mp4", "", tmp_path, None, {"hud": "off"}, "GTA V", 60.0) is None


def test_auto_profile_choice():
    assert [p["name"] for p in analyze.choose_profiles({"hud": "auto"}, "GTA 5 Thug Life")[0]] == ["gta5"]
    assert [p["name"] for p in analyze.choose_profiles({"hud": "auto"}, "my stream")[0]] == ["gta5", "generic"]
    profs, forced = analyze.choose_profiles({"hud": "gta6"}, "anything")
    assert profs[0]["experimental"] and forced
    assert analyze.choose_profiles({"hud": "off"}, "GTA V") == (None, False)


def test_face_signal():
    still = [(t / 3, [0.4, 0.2, 0.5, 0.4]) for t in range(12)]
    assert analyze.face_signal(still) == (0.0, 1.0)
    moving = [(t / 3, [0.4 + 0.02 * t, 0.2, 0.5 + 0.02 * t, 0.4 + 0.01 * t]) for t in range(12)]
    motion, seen = analyze.face_signal(moving)
    assert motion > 0.5 and seen == 1.0
    assert analyze.face_signal([(0, None), (1, None)]) == (0.0, 0.0)


def test_clips_json_gets_hud_events_and_the_profile():
    clip = {
        "rank": 1,
        "start": 10.0,
        "end": 40.0,
        "peak": 30.0,
        "score": 70,
        "breakdown": {"hud": 100},
        "reason": "WASTED on screen",
        "category": "fail",
        "signals": {},
        "heatmap": None,
        "words": [],
        "segments": [],
        "arc": {},
        "hook_headline": "WAIT",
        "llm": None,
        "hud_events": [dict(WASTED, t=31.004)],
    }
    res = {"clips": [clip], "transcribed": False, "duration": 99.0, "language": "en", "elapsed": 1.0}
    st = {"min_dur": 15, "max_dur": 60, "min_pause": 0.5}
    doc = clips_document(dict(res, hud={"profile": "gta5"}), "x.mp4", "x", st)
    assert doc["clips"][0]["hud_events"] == [{"t": 31.0, "event": "wasted", "label": "WASTED"}]
    assert doc["settings"]["hud"] == "gta5"
    plain = dict(clip)
    plain.pop("hud_events")
    doc = clips_document(dict(res, clips=[plain]), "x.mp4", "x", st)
    assert "hud_events" not in doc["clips"][0] and doc["settings"]["hud"] == "off"
