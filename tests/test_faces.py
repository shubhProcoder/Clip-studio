import sys
import types

import pytest

from streamclipper import faces, vision
from streamclipper.faces import (
    DEAD_ZONE,
    SWITCH_LEAD_S,
    _dedupe,
    map_tile,
    pick_speaker,
    smooth_track,
    speech_spans,
    tiles,
    voiced_at,
)


class _Fake:
    def __init__(self, *a, **k):
        self.args = a
        self.cascades = ["x"]


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(faces, "_DETECTOR", {})
    monkeypatch.setattr(faces, "MediaPipeFaces", type("MP", (_Fake,), {"name": "MediaPipe"}))
    monkeypatch.setattr(faces, "HaarFaces", type("Haar", (_Fake,), {"name": "Haar (OpenCV)"}))


def test_backend_order_mediapipe_then_haar_then_none(fresh, monkeypatch):
    monkeypatch.setattr(faces, "_mediapipe_ok", lambda: True)
    monkeypatch.setattr(faces, "_haar_cv2", lambda: object())
    assert faces.get_detector().name == "MediaPipe"
    monkeypatch.setattr(faces, "_DETECTOR", {})
    monkeypatch.setattr(faces, "_mediapipe_ok", lambda: False)  # mediapipe hidden
    assert faces.get_detector().name == "Haar (OpenCV)"
    monkeypatch.setattr(faces, "_DETECTOR", {})
    monkeypatch.setattr(faces, "_haar_cv2", lambda: None)  # neither installed: callers keep the old behaviour
    assert faces.get_detector() is None
    assert vision.detect_faces("x.mp4", [1.0]) is None
    assert vision.track_faces("x.mp4", 0.0, 2.0) is None


def test_a_broken_mediapipe_falls_back_to_haar(fresh, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no tflite runtime")

    monkeypatch.setattr(faces, "_mediapipe_ok", lambda: True)
    monkeypatch.setattr(faces, "MediaPipeFaces", boom)
    monkeypatch.setattr(faces, "_haar_cv2", lambda: object())
    assert faces.get_detector().name == "Haar (OpenCV)"


def test_doctor_status_names(monkeypatch):
    monkeypatch.setattr(faces, "_mediapipe_ok", lambda: False)
    monkeypatch.setattr(faces, "_haar_cv2", lambda: None)
    assert faces.detector_status()[0] == "none"
    monkeypatch.setattr(faces, "_haar_cv2", lambda: object())
    assert faces.detector_status()[0] == "Haar fallback"


def test_doctor_says_when_mediapipe_cannot_start(fresh, monkeypatch):
    def boom(*a, **k):
        raise OSError("libEGL.so.1: cannot open shared object file")

    monkeypatch.setattr(faces, "_mediapipe_ok", lambda: True)
    monkeypatch.setattr(faces, "MediaPipeFaces", boom)
    monkeypatch.setattr(faces, "_haar_cv2", lambda: object())
    monkeypatch.setitem(sys.modules, "mediapipe", types.SimpleNamespace(__version__="x"))
    name, detail = faces.detector_status()
    assert name == "Haar fallback" and "libEGL" in detail and "libegl1" in detail


def test_tiles_overlap_by_half_and_cover_the_frame():
    ts = tiles(960, 540)
    assert len(ts) == 6
    assert {(x, y) for x, y, _, _ in ts} == {(0, 0), (240, 0), (480, 0), (0, 180), (240, 180), (480, 180)}
    assert all(tw == 480 and th == 360 for _, _, tw, th in ts)
    covered = {(x + dx, y + dy) for x, y, tw, th in ts for dx in (0, tw - 1) for dy in (0, th - 1)}
    assert (959, 539) in covered and (0, 0) in covered


def test_tile_detections_map_back_with_keypoints_and_dedupe():
    det = {"box": [0.5, 0.5, 0.75, 0.875], "score": 0.9, "keypoints": {"mouth": (0.625, 0.8)}}
    m = map_tile(det, (480, 180, 480, 360), 960, 540)
    assert m["box"] == pytest.approx([0.75, 2 / 3, 0.875, 0.9166667], abs=1e-4)
    assert m["keypoints"]["mouth"] == pytest.approx((0.8125, 0.8666667), abs=1e-4)
    same = dict(m, box=[0.751, 0.668, 0.874, 0.915])  # the whole-frame pass found the same face
    small = {"box": [0.1, 0.1, 0.12, 0.14], "score": 0.6, "keypoints": {}}
    kept = _dedupe([same, small, m])
    assert len(kept) == 2 and kept[1] is not None and kept[1]["box"] == [0.1, 0.1, 0.12, 0.14]  # largest first


def test_mediapipe_model_finds_no_face_on_a_blank_frame():
    pytest.importorskip("mediapipe")
    np = pytest.importorskip("numpy")
    try:
        det = faces.MediaPipeFaces()
    except OSError as e:  # e.g. headless Linux without libEGL: get_detector falls back to Haar (tested above)
        pytest.skip(f"MediaPipe cannot start here: {e}")
    assert det.detect(np.zeros((540, 960, 3), np.uint8)) == []


# ------------------------------------------------------------ smoothing and active speaker (pure functions)

TIMES = [k * 0.5 for k in range(40)]


def test_dead_zone_holds_still_for_small_moves():
    crop_w = 0.3
    small = [0.5 + (0.02 if k % 2 else -0.02) for k in range(40)]  # nodding head: +-0.02 < 15% of the crop
    out = smooth_track(small, TIMES, crop_w, max_speed=0.04)
    assert max(out) - min(out) < 1e-6
    assert DEAD_ZONE * crop_w / 2 > 0.02


def test_speed_cap_and_follow_a_real_move():
    out = smooth_track([0.3] * 10 + [0.7] * 30, TIMES, 0.3, max_speed=0.04)
    steps = [abs(b - a) for a, b in zip(out, out[1:], strict=False)]
    assert max(steps) <= 0.04 * 0.5 + 1e-9  # never faster than the cap
    assert out[-1] > 0.6  # it gets there (outside the dead zone)


def test_size_changes_only_above_15_percent():
    out = smooth_track([0.5] * 4, TIMES[:4], 0.3, 0.04, sizes=[0.2, 0.22, 0.24, 0.1])
    assert [s for _, s in out] == [0.2, 0.2, 0.24, 0.1]


def test_speaker_switches_after_a_one_second_lead_only():
    scores = [[5.0, 1.0]] * 6 + [[1.0, 5.0]] * 10
    who = pick_speaker(scores, TIMES[:16])
    assert who[:6] == [0] * 6
    first_b = who.index(1)
    assert TIMES[first_b] - TIMES[6] == pytest.approx(SWITCH_LEAD_S)


def test_no_switch_during_silence():
    scores = [[5.0, 1.0]] * 4 + [[1.0, 9.0]] * 6  # B "moves" (eats, laughs silently) while nobody talks
    voiced = [True] * 4 + [False] * 6
    assert set(pick_speaker(scores, TIMES[:10], voiced)) == {0}


def test_no_flip_flop_on_alternating_noise():
    scores = [[5.0, 1.0] if k % 2 else [1.0, 6.0] for k in range(20)]  # each lead lasts 0.5 s
    who = pick_speaker(scores, TIMES[:20])
    assert len(set(who[1:])) == 1


def test_speech_spans_and_voicing():
    words = [{"w": "a", "s": 10.0, "e": 10.3}, {"w": "b", "s": 10.4, "e": 10.6}, {"w": "c", "s": 13.0, "e": 13.2}]
    spans = speech_spans(words, offset=9.0)
    assert spans == [(0.85, 1.75), (3.85, 4.35)]
    assert voiced_at([1.0, 2.5, 4.0], spans) == [True, False, True]
