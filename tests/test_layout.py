import pytest

from streamclipper.captions.styles import STYLES
from streamclipper.layout import capcut_y, caption_anchor, frame_crop, layout_boxes
from streamclipper.util import ToolError
from streamclipper.vision import classify_faces, classify_layout, face_crop_region


def _inside(rect, canvas):
    x, y, w, h = rect
    return x >= 0 and y >= 0 and x + w <= canvas[0] and y + h <= canvas[1]


def test_square_frame_rects_inside_canvas():
    lay = layout_boxes("9:16", "square-frame")
    assert lay["canvas"] == [1080, 1920]
    for rect in lay["rects"].values():
        assert _inside(rect, lay["canvas"])
    frame = lay["rects"]["frame"]
    assert frame == [0, 470, 1080, 900]
    assert 150 <= lay["hook"]["y"] <= 430  # hook banner above the frame
    assert lay["caption"]["y"] >= frame[1] + frame[3]  # captions below the frame
    with pytest.raises(ToolError):
        layout_boxes("1:1", "square-frame")


@pytest.mark.parametrize("ratio", ["9:16", "1:1"])
@pytest.mark.parametrize("name", list(STYLES))
def test_auto_captions_stay_below_the_cam(ratio, name):
    lay = layout_boxes(ratio, "split")
    cam = lay["rects"]["cam"]
    st = STYLES[name]
    for pos in ("auto", "top"):
        align, y = caption_anchor(lay, pos, st.anim_up)
        assert align == 8
        assert y - st.anim_up >= cam[1] + cam[3] + 20


@pytest.mark.parametrize("layout", ["split", "full", "hook-grabber", "square-frame"])
def test_every_position_is_on_the_canvas(layout):
    lay = layout_boxes("9:16", layout)
    for pos in ("auto", "top", "center", "lower"):
        align, y = caption_anchor(lay, pos)
        assert align in (2, 5, 8) and 0 < y < 1920
        assert -1.0 < capcut_y(align, y, 1920) < 1.0


def test_frame_crop_never_cuts_the_cam():
    cam = (0, 0, 412, 260)  # top-left webcam in a 1920x1080 stream
    w, h, x, y = frame_crop(1920, 1080, None, cam, 1080 / 900)
    assert abs(w / h - 1.2) < 0.01 and x + w <= 1920
    assert x >= 412  # cam left out entirely
    w, h, x, _ = frame_crop(1920, 1080, None, (1508, 0, 412, 260), 1.2)
    assert x + w <= 1508  # right-side cam left out
    w, h, x, _ = frame_crop(1920, 1080, None, None, 1.2)
    assert abs(x - (1920 - w) / 2) <= 2  # no cam: centred


# --- face-based layout detection (vision.classify_faces / classify_layout; no cv2 needed) ---


def _face(cx, cy, h, aspect=0.56):
    """Normalised face box centred on (cx, cy), h of the frame height (16:9 frame, roughly square face)."""
    w = h * aspect
    return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]


def test_corner_cam_from_stable_corner_face():
    faces = [[_face(0.12 + 0.005 * (k % 2), 0.12, 0.12)] for k in range(8)]
    res = classify_layout(None, 0, 8, faces=faces)
    assert res["kind"] == "corner-cam" and res["corner"] == "top_left"
    x0, y0, x1, y1 = res["cam_box"]
    f = res["face_box"]
    assert x0 <= f[0] and y0 <= f[1] and x1 >= f[2] and y1 >= f[3]  # face fully inside the cam box


def test_large_cutout_corner_cam_stays_corner():
    # large keyed cut-out cam: face ~23% of the frame height, centre (0.22, 0.66)
    faces = [[_face(0.22, 0.66, 0.23)] for _ in range(7)] + [[]]
    res = classify_layout(None, 0, 8, faces=faces)
    assert res["kind"] == "corner-cam" and res["corner"] == "bottom_left"


def test_full_cam_from_big_centred_face():
    faces = [[_face(0.5, 0.45, 0.4)] for _ in range(6)] + [[], []]
    res = classify_faces(faces)
    assert res["kind"] == "full-cam"
    region = face_crop_region(res["face_box"], 1920, 1080)
    assert abs((region[2] - region[0]) * 1920 / 1080 - 9 / 16) < 0.01
    assert region[0] <= res["face_box"][0] and region[2] >= res["face_box"][2]


def test_moving_or_missing_face_is_no_cam():
    assert classify_faces([[] for _ in range(8)])["kind"] == "no-cam"
    moving = [[_face(0.05 if k % 2 else 0.25, 0.8, 0.1)] for k in range(8)]  # jumps 20% between samples
    res = classify_faces(moving)
    assert res["kind"] == "no-cam"
    few = [[_face(0.1, 0.1, 0.1)] for _ in range(3)] + [[] for _ in range(5)]
    assert classify_faces(few)["kind"] == "no-cam"


def test_no_cv2_falls_back_to_skin_heuristic(monkeypatch):
    import streamclipper.options as options
    import streamclipper.vision as vision

    monkeypatch.setattr(vision, "_detector", lambda: None)  # neither MediaPipe nor OpenCV installed
    assert vision.detect_faces("x.mp4", [1.0]) is None
    assert vision.classify_layout("x.mp4", 0, 5) is None
    monkeypatch.setattr(
        options, "detect_player_and_webcam", lambda src, dur: {"player_top_crop": [0.0, 0.0, 0.22, 0.24]}
    )
    opts = {"layout": "split", "cam_crop": None, "game_crop": None, "ratio": "9:16", "preset": ""}
    meta = {"width": 1920, "height": 1080, "dur": 30.0}
    new_opts, cam, game, note = options.clip_layout("x.mp4", meta, opts, 0, 10)
    assert new_opts["layout"] == "split" and cam == [0.0, 0.0, 0.22, 0.24] and "skin heuristic" in note


def test_full_cam_and_no_cam_never_split(monkeypatch):
    import streamclipper.options as options

    meta = {"width": 1920, "height": 1080, "dur": 30.0}
    opts = {"layout": "split", "cam_crop": None, "game_crop": None, "ratio": "9:16", "preset": ""}
    for kind, face in (("full-cam", [0.4, 0.2, 0.6, 0.6]), ("no-cam", None)):
        monkeypatch.setattr(
            options,
            "classify_layout",
            lambda *a, kind=kind, face=face: {"kind": kind, "corner": None, "face_box": face, "cam_box": None},
        )
        new_opts, cam, game, note = options.clip_layout("x.mp4", meta, opts, 0, 10)
        assert new_opts["layout"] == "full" and cam is None and kind in note
        if kind == "full-cam":
            assert game[0] <= 0.4 and game[2] >= 0.6
    assert opts["layout"] == "split"  # the caller's options are not changed


def test_blank_screen_clip_is_skipped(monkeypatch):
    import numpy as np

    import streamclipper.options as options
    from streamclipper.util import SkipClip
    from streamclipper.vision import dark_share

    black = np.zeros((36, 64), np.uint8)
    lit = np.full((36, 64), 120, np.uint8)
    assert dark_share([black, black, lit, lit]) == 0.5 and dark_share([]) == 0.0
    monkeypatch.setattr(
        options, "classify_layout", lambda *a: {"kind": "blank", "dark": 0.75, "corner": None, "face_box": None}
    )
    meta = {"width": 1920, "height": 1080, "dur": 30.0}
    opts = {"layout": "split", "cam_crop": None, "game_crop": None, "ratio": "9:16", "preset": ""}
    with pytest.raises(SkipClip):
        options.clip_layout("x.mp4", meta, opts, 0, 10)


def _keyed_streamer_frame(vw=640, vh=360):
    """Synthetic keyed-streamer frame: green game, a skin-coloured head, red torso and an arm reaching right."""
    import numpy as np

    img = np.zeros((vh, vw, 3), np.uint8)
    img[...] = (40, 160, 60)
    fx0, fy0, fx1, fy1 = 0.16, 0.55, 0.28, 0.78  # face (normalised), like a large keyed cut-out cam
    img[int(fy0 * vh) : int(fy1 * vh), int(fx0 * vw) : int(fx1 * vw)] = (120, 80, 60)
    img[int(0.78 * vh) :, int(0.04 * vw) : int(0.40 * vw)] = (200, 30, 30)  # torso
    img[int(0.82 * vh) : int(0.90 * vh), int(0.40 * vw) : int(0.47 * vw)] = (120, 80, 60)  # arm + hand
    person = (img != (40, 160, 60)).any(axis=2)
    return [fx0, fy0, fx1, fy1], person


def test_gameplay_crop_leaves_out_the_whole_keyed_streamer():
    from streamclipper.layout import gameplay_box_avoiding
    from streamclipper.vision import person_box

    vw, vh = 640, 360
    face, person_px = _keyed_streamer_frame(vw, vh)
    pb = person_box(face, "bottom_left")
    ys, xs = person_px.nonzero()
    assert pb[0] * vw <= xs.min() and xs.max() < pb[2] * vw and pb[1] * vh <= ys.min()  # holds the hand too
    game = gameplay_box_avoiding(pb, vw, vh, 1080 / 1248)
    assert game is not None and game[3] - game[1] >= 0.99  # full-height crop beside the streamer
    gx0, gy0, gx1, gy1 = [int(round(v * s)) for v, s in zip(game, (vw, vh, vw, vh), strict=True)]
    assert not person_px[gy0:gy1, gx0:gx1].any()  # no streamer pixel in the gameplay panel
    # a streamer in the middle of the frame leaves no tall crop: the caller falls back to another layout
    assert gameplay_box_avoiding([0.3, 0.2, 0.7, 1.0], vw, vh, 1080 / 1248) is None


def test_cam_panel_box_is_grown_to_fill_the_width():
    from streamclipper.layout import grow_to_aspect

    x, y, w, h = grow_to_aspect((0, 590, 688, 490), 1920, 1080, 1080 / 672)
    assert abs(w / h - 1080 / 672) < 0.01 and x <= 0 and y <= 590 and x + w >= 688 and y + h >= 1080 - 2
    x, y, w, h = grow_to_aspect((1500, 0, 420, 240), 1920, 1080, 1080 / 672)  # top-right overlay
    assert x + w <= 1920 and x <= 1500 and w >= 420 and h >= 240


def test_dark_game_scene_is_not_a_blank_card():
    import numpy as np

    from streamclipper.vision import dark_share

    rng = np.random.default_rng(0)
    dark_game = [rng.integers(8, 40, (90, 160)).astype(np.uint8) for _ in range(8)]  # dim, but not black
    blank = [np.zeros((90, 160), np.uint8) for _ in range(8)]
    for f in blank:
        f[:20] = 120  # a caption bar on the card
    assert dark_share(dark_game) == 0.0
    assert dark_share(blank) == 1.0


# speaker layout (podcasts): injected face tracks, no OpenCV needed


def _track(faces_per_sample, dur=10.0, step=0.5, cuts=()):
    n = int(dur / step)
    return {
        "times": [k * step for k in range(n)],
        "faces": [list(faces_per_sample(k)) for k in range(n)],
        "cuts": list(cuts),
    }


def _box(cx, cy, h, vw=1920, vh=1080):
    w = h * vh / vw  # square face in pixels
    return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]


def test_speaker_two_faces_side_by_side_are_stacked_with_faces_inside_their_halves():
    from streamclipper.layout import SPEAKER_HALF, speaker_shots

    tr = _track(lambda k: [_box(0.25, 0.3, 0.16), _box(0.75, 0.32, 0.15)])
    shots = speaker_shots(tr, 10.0, 1920, 1080, 30.0)
    assert len(shots) == 1 and shots[0]["mode"] == "stack"
    top, bottom = shots[0]["faces_out"]
    hw, hh = SPEAKER_HALF
    assert 0 <= top[0] and top[2] <= hw and 0 <= top[1] and top[3] <= hh  # left person whole in the top half
    assert 0 <= bottom[0] and bottom[2] <= hw and hh <= bottom[1] and bottom[3] <= 2 * hh
    for w, h, x, y in shots[0]["regions"]:
        assert x >= 0 and y >= 0 and x + w <= 1920 and y + h <= 1080


def test_speaker_cut_splits_shots_and_a_close_up_is_not_stacked():
    from streamclipper.layout import speaker_shots

    # 0-5 s: wide shot with two people; 5-10 s: one close-up plus a hand hit low in the frame
    def faces(k):
        if k < 10:
            return [_box(0.25, 0.3, 0.16), _box(0.75, 0.3, 0.16)]
        return [_box(0.55, 0.35, 0.45), _box(0.6, 0.85, 0.15)]

    shots = speaker_shots(_track(faces, cuts=[5.0]), 10.0, 1920, 1080, 30.0)
    assert [s["mode"] for s in shots] == ["stack", "single"]
    assert shots[0]["f1"] == shots[1]["f0"] == 150  # cut on an exact frame
    assert shots[1]["why"] == "one face"


def test_speaker_crop_speed_limit_and_face_inside():
    from streamclipper.layout import SPEAKER_MAX_SPEED, follow_path

    vw, vh, cw = 1920, 1080, 608
    times = [k * 0.5 for k in range(20)]
    boxes = [_box(0.3 if t < 5 else 0.6, 0.35, 0.3) for t in times]  # the person moves across the frame
    path = follow_path(boxes, times, 0.0, vw, vh, cw)
    for (t0, x0), (t1, x1) in zip(path, path[1:], strict=False):
        assert abs(x1 - x0) <= SPEAKER_MAX_SPEED * vw * (t1 - t0) + 2  # never faster than 4% of the width/s
    still = follow_path([_box(0.5, 0.35, 0.3)] * 20, times, 0.0, vw, vh, cw)
    assert len(still) == 1  # a steady face gives a static crop (no jitter)
    x = still[0][1]
    face = _box(0.5, 0.35, 0.3)
    assert x <= face[0] * vw and face[2] * vw <= x + cw


def test_speaker_no_face_shows_whole_frame_and_captions_avoid_faces():
    from streamclipper.layout import layout_boxes, speaker_caption_zone, speaker_shots

    shots = speaker_shots(_track(lambda k: []), 10.0, 1920, 1080, 30.0)
    assert shots[0]["mode"] == "fit"
    assert speaker_caption_zone(shots) == "lower"
    stack = [{"f0": 0, "f1": 300, "faces_out": [[400, 250, 680, 530], [400, 1460, 680, 1740]]}]
    assert speaker_caption_zone(stack) == "seam"
    for name in ("speaker", "speaker-seam"):
        lay = layout_boxes("9:16", name)
        assert lay["rects"] == {"main": [0, 0, 1080, 1920]}


def test_speaker_switch_follows_the_active_talker_after_a_lead_and_splits_on_a_frame():
    import numpy as np

    from streamclipper.layout import speaker_shots

    a, b = _box(0.2, 0.3, 0.2), _box(0.8, 0.5, 0.3)  # different size and height: not a stacked pair
    tr = _track(lambda k: [b, a])
    rng = np.random.default_rng(53)
    frames = []
    for k in range(len(tr["times"])):
        f = np.zeros((540, 960), np.uint8)
        x0, y0, x1, y1 = a if k < 10 else b  # A talks for 5 s, then B
        r0, r1, c0, c1 = int((y0 + 0.6 * (y1 - y0)) * 540), int(y1 * 540), int(x0 * 960), int(x1 * 960)
        f[r0:r1, c0:c1] = rng.integers(0, 255, (r1 - r0, c1 - c0))
        frames.append(f)
    tr["frames"] = frames
    shots = speaker_shots(tr, 10.0, 1920, 1080, 30.0)
    assert [s["mode"] for s in shots] == ["single", "single"]
    # the 5.0 s sample differs in both mouths (A stops, B starts), so B leads from 5.5 s; the switch waits 1.0 s
    assert shots[0]["f1"] == shots[1]["f0"] == 195
    assert shots[0]["faces_out"][0][0] < shots[1]["faces_out"][0][0] or shots[0]["path"] != shots[1]["path"]
    silent = speaker_shots(tr, 10.0, 1920, 1080, 30.0, spans=[(0.0, 5.0)])  # nobody talks after 5 s
    assert len(silent) == 1
