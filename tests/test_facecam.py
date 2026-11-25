import numpy as np

from streamclipper.facecam import border_candidates, fuse, iou


def test_manual_crop_override_is_preserved():
    got = fuse([], [], [0.1, 0.2, 0.5, 0.6])
    assert got["method"] == "manual"
    assert got["confidence"] == 1.0
    assert got["cam_box"] == [0.1, 0.2, 0.5, 0.6]


def test_face_and_border_fusion_resolves_overlapping_evidence():
    face = [{"box": [0.05, 0.05, 0.25, 0.35], "confidence": 0.8, "corner": "top-left"}]
    border = [{"box": [0.0, 0.0, 0.30, 0.40], "confidence": 0.7, "method": "border"}]
    got = fuse(face, border)
    assert got["method"] == "face+border"
    assert got["confidence"] >= 0.7
    assert got["diagnostics"]["face"]
    assert got["diagnostics"]["border"]


def test_face_only_is_conservative_when_border_evidence_is_missing():
    face = [{"box": [0.78, 0.68, 1.0, 0.96], "confidence": 0.8, "corner": "bottom-right"}]
    got = fuse(face, [])
    assert got["method"] == "face"
    assert got["kind"] == "corner-cam"
    assert got["cam_box"] == face[0]["box"]


def test_low_confidence_border_does_not_force_camera_layout():
    assert fuse([], [{"box": [0.0, 0.0, 0.2, 0.2], "confidence": 0.30}]) is None


def test_static_border_candidates_find_a_still_rectangular_region():
    frames = []
    rng = np.random.default_rng(7)
    for _i in range(8):
        frame = rng.integers(40, 180, (180, 320, 3), dtype=np.uint8)
        frame[20:100, 220:300] = 120
        frames.append(frame)
    got = border_candidates(frames)
    assert isinstance(got, list)


def test_animated_camera_movement_still_preserves_best_face_evidence():
    faces = [
        {"box": [0.05, 0.05, 0.25, 0.35], "confidence": 0.55, "corner": "top-left"},
        {"box": [0.08, 0.08, 0.28, 0.38], "confidence": 0.65, "corner": "top-left"},
    ]
    got = fuse(faces, [])
    assert got is not None
    assert got["method"] == "face"


def test_iou_separates_overlapping_and_distinct_boxes():
    assert iou([0, 0, 0.5, 0.5], [0, 0, 0.5, 0.5]) == 1.0
    assert iou([0, 0, 0.2, 0.2], [0.8, 0.8, 1.0, 1.0]) == 0.0
