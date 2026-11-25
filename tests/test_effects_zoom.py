import random
import shutil
import subprocess

import pytest

from streamclipper import effects
from streamclipper.effects import (
    EASE_IN,
    EASE_OUT,
    FACE_MARGIN,
    HOLD_TO,
    MIN_GAP,
    add_capcut_zoom,
    capcut_zoom_keys,
    envelope,
    face_safe,
    perspective_filter,
    view_box,
    zoom_events,
)


def _plan(words, peak=10.0, zoom="medium", tease=True):
    segs = []
    if tease:
        segs.append({"src_s": 9.4, "src_e": 11.0, "tl_start": 0.0, "kind": "tease"})
    t0 = segs[-1]["tl_start"] + 1.6 if tease else 0.0
    segs.append({"src_s": 0.0, "src_e": 20.0, "tl_start": t0, "kind": "story"})
    return {"segments": segs, "words": words, "peak": peak, "effects": {"zoom": zoom}}


def _w(text, s):
    return {"w": text, "s": s, "e": s + 0.3}


def test_zoom_off_by_default_and_for_old_plans():
    assert zoom_events({"segments": [{"src_s": 0, "src_e": 10, "tl_start": 0}], "words": [], "peak": 5}) == []
    assert effects.effects_of({}) == effects.EFFECT_DEFAULTS


def test_events_payoff_first_spacing_and_timing():
    words = [_w("bro", 2.0), _w("insane", 3.0), _w("what", 9.9), _w("crazy", 11.0), _w("no", 15.0), _w("way", 15.3)]
    evs = zoom_events(_plan(words))
    assert evs and evs[0]["why"] in ("payoff", "hype")
    payoff = next(e for e in evs if e["why"] == "payoff")
    # the payoff (window 10.0 s) sits 1.6 s later on the timeline (after the tease), minus the lead
    assert payoff["t"] == pytest.approx(1.6 + 10.0 - effects.LEAD, abs=1e-3)
    ts = [e["t"] for e in evs]
    assert all(b - a >= MIN_GAP - 1e-6 for a, b in zip(ts, ts[1:], strict=False))
    assert all(e["level"] == 1.12 for e in evs)
    assert len(evs) <= effects.MAX_EVENTS
    # 'crazy' (11.0) is within 3 s of the payoff: dropped; 'no way' (15.0) is a trigger phrase: kept
    assert not any(e["word"] == "crazy" for e in evs)


def test_envelope_eases_in_holds_and_eases_out():
    assert envelope(-0.1, 0.0) == 0.0
    assert envelope(EASE_IN, 0.0) == pytest.approx(1.0)
    assert envelope(1.0, 0.0) == pytest.approx(1.0)
    assert envelope(HOLD_TO + EASE_OUT + 0.01, 0.0) == 0.0
    assert 0.0 < envelope(EASE_IN / 2, 0.0) < 1.0


@pytest.mark.parametrize("seed", range(5))
def test_face_safe_never_lets_a_face_leave_the_view(seed):
    rnd = random.Random(seed)
    for _ in range(400):
        x0, y0 = rnd.random() * 0.9, rnd.random() * 0.9
        w, h = rnd.uniform(0.03, 0.6), rnd.uniform(0.03, 0.6)
        face = [x0, y0, min(1.0, x0 + w), min(1.0, y0 + h)]
        level = rnd.choice([1.08, 1.12, 1.2])
        fit = face_safe(level, [face])
        if fit is None:
            continue
        lv, fx, fy = fit
        assert 1.0 < lv <= level
        vx0, vy0, vx1, vy1 = view_box(lv, fx, fy)
        assert -1e-6 <= vx0 and vx1 <= 1 + 1e-6 and -1e-6 <= vy0 and vy1 <= 1 + 1e-6
        # the whole face stays in view, with the margin unless the face was already closer to the edge
        assert vx0 <= max(0.0, face[0] - FACE_MARGIN) + 1e-6 and vx1 >= min(1.0, face[2] + FACE_MARGIN) - 1e-6
        assert vy0 <= max(0.0, face[1] - FACE_MARGIN) + 1e-6 and vy1 >= min(1.0, face[3] + FACE_MARGIN) - 1e-6


def test_face_safe_lowers_the_level_then_skips():
    big = [0.02, 0.05, 0.95, 0.9]  # 93% wide: only ~1.07x fits with no margin left
    fit = face_safe(1.2, [big])
    assert fit is None or fit[0] < 1.2
    assert face_safe(1.2, [[0.0, 0.0, 1.0, 1.0]]) is None
    lv, fx, fy = face_safe(1.2, [])
    assert lv == 1.2 and fx == pytest.approx(0.5) and fy == pytest.approx(0.5)


def test_perspective_filter_shape():
    assert perspective_filter([]) == ""
    f = perspective_filter(
        [{"t": 1.0, "level": 1.12, "fx": 0.4, "fy": 0.3}, {"t": 5.0, "level": 1.2, "fx": 0.5, "fy": 0.5}]
    )
    assert f.startswith("perspective=x0='") and ":eval=frame" in f and "sense=source" in f
    assert "enable='between(n,30," in f and "between(n,150," in f
    for k in ("x0", "y0", "x1", "y1", "x2", "y2", "x3", "y3"):
        assert f"{k}='" in f
    assert f.count("clip((on-30)") > 0 and f.count("clip((on-150)") > 0  # every corner carries both events


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_perspective_filter_runs_in_ffmpeg():
    f = perspective_filter([{"t": 0.2, "level": 1.12, "fx": 0.3, "fy": 0.6}])
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=320x368:r=30:d=2", "-vf", f]
        + ["-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


def test_capcut_keys_match_the_curve_and_keep_the_fixed_point():
    ev = {"t": 2.0, "level": 1.2, "fx": 0.25, "fy": 0.5}
    keys = capcut_zoom_keys(ev, seg_tl=1.0, seg_dur=5.0, base_y=0.1, panel_h=0.65)
    assert [k[0] for k in keys] == [1_000_000, 1_250_000, 2_200_000, 2_500_000]
    assert keys[0][1] == 1.0 and keys[1][1] == pytest.approx(1.2) and keys[-1][1] == 1.0
    # the fixed point at x=0.25 stays put: scaled around the centre, the segment moves right by (0.5-0.25)*0.2*2
    assert keys[1][2] == pytest.approx(0.1) and keys[1][3] == pytest.approx(0.1)
    # an event crossing the segment end gets a key on the edge
    cut = capcut_zoom_keys(ev, seg_tl=0.0, seg_dur=3.0)
    assert cut[-1][0] == 3_000_000 and cut[-1][1] > 1.0


def test_add_capcut_zoom_keys_the_right_segments():
    spec = {
        "video_track": [
            {"src_start": 0, "src_end": 2, "tl_start": 0.0, "anim": "punch-zoom", "zoom": 1.0, "pos_y": 0.0},
            {"src_start": 5, "src_end": 9, "tl_start": 2.0, "anim": "none", "zoom": 1.0, "pos_y": 0.0},
        ],
        "overlay_tracks": [[{"src_start": 0, "src_end": 6, "tl_start": 0.0, "pos_y": 0.6}]],
    }
    zp = {"main": [{"t": 3.0, "level": 1.12, "fx": 0.5, "fy": 0.5}], "overlay": []}
    assert add_capcut_zoom(spec, zp, {"main": 0.65}) == 1
    assert "zoom_keys" not in spec["video_track"][0] and spec["video_track"][0]["anim"] == "punch-zoom"
    keys = spec["video_track"][1]["zoom_keys"]
    assert keys[0][0] == 1_000_000 and max(k[1] for k in keys) == pytest.approx(1.12)
    assert "zoom_keys" not in spec["overlay_tracks"][0][0]


def test_plan_zoom_skips_face_panels_without_a_detector(monkeypatch):
    import streamclipper.faces as faces

    monkeypatch.setattr(faces, "get_detector", lambda: None)
    plan = _plan([_w("insane", 4.0)])
    plan.update(layout="full", panels={"main_src": "x.mp4"})
    zp = effects.plan_zoom(plan)
    assert zp["main"] == [] and any("no face detector" in n for n in zp["notes"])
    plan.update(layout="split", panels={"main_src": "x.mp4", "overlay_src": "cam.mp4"})
    zp = effects.plan_zoom(plan)  # the gameplay panel zooms centred, the cam panel stays
    assert zp["main"] and zp["overlay"] == []
    assert all(e["fx"] == pytest.approx(0.5) for e in zp["main"])
