import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from streamclipper import reframe
from streamclipper.util import ToolError

CTX = {"width": 1920, "height": 1080, "ratio": "9:16"}
GAME = [600, 0, 606, 1078]  # 9:16 window (606 / 1078 = 0.5622)
CAM = [0, 0, 412, 256]  # 1080:672 = 1.607 (412 / 256 = 1.609)
SPLIT_GAME = [700, 100, 600, 694]  # 1080:1248 = 0.8654 (600 / 694 = 0.8646)


def seg(a, b, layout="full", game=GAME, cam=None, keys=None, boundary="cut", ease="smooth"):
    return {
        "start": a,
        "end": b,
        "layout": layout,
        "windows": {"cam": cam, "game": game},
        "keys": keys or [],
        "ease": ease,
        "boundary": boundary,
    }


def two_segments():
    return [
        seg(1.0, 4.0),
        seg(4.0, 9.0, keys=[{"t": 5.0, "game": [100, 0, 606, 1078]}, {"t": 8.0, "game": [1300, 0, 606, 1078]}]),
    ]


# ---- model --------------------------------------------------------------------------------------------------


def test_old_form_is_one_segment_and_only_segments_or_keys_use_reframe():
    form = {"start": 1.0, "end": 9.0, "layout": "split", "cam": CAM, "game": SPLIT_GAME}
    s = reframe.normalize_segments(form)
    assert len(s) == 1 and s[0]["layout"] == "split" and s[0]["windows"] == {"cam": CAM, "game": SPLIT_GAME}
    assert not reframe.uses_reframe(s) and not reframe.uses_reframe(None) and not reframe.uses_reframe([])
    assert reframe.uses_reframe(two_segments())
    assert reframe.uses_reframe([seg(1, 9, keys=[{"t": 2, "game": GAME}])])


def test_panel_rects_and_aspects():
    assert reframe.panel_rects("split", "9:16") == [("cam", 0, 0, 1080, 672), ("game", 0, 672, 1080, 1248)]
    assert reframe.panel_rects("split", "1:1") == [("cam", 0, 0, 1080, 454), ("game", 0, 454, 1080, 626)]
    assert reframe.panel_rects("square-frame", "9:16") == [("game", 0, 470, 1080, 900)]
    for lay in ("full", "caption-punch", "speaker"):
        assert reframe.window_aspects(lay, "9:16") == {"game": 1080 / 1920}
    assert reframe.window_aspects("full", "1:1") == {"game": 1.0}


# ---- crop maths ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "box",
    [
        [-300, -200, 500, 300],  # top-left outside
        [1800, 900, 500, 300],  # bottom-right outside
        [-50, 900, 400, 100],  # bottom-left
        [1700, -40, 400, 100],  # top-right
        [100, 100, 5000, 50],  # wider than the frame
        [500, 500, 10, 10],  # tiny
        [501, 333, 301, 199],  # odd numbers
    ],
)
def test_lock_aspect_keeps_shape_inside_frame_even_and_big_enough(box):
    for aspect in (1080 / 1920, 1080 / 672, 1080 / 1248, 1080 / 900, 1.0):
        x, y, w, h = reframe.lock_aspect(box, aspect, 1920, 1080)
        assert abs((w / h) / aspect - 1) <= reframe.ASPECT_TOL
        assert 0 <= x and 0 <= y and x + w <= 1920 and y + h <= 1080
        assert w >= reframe.MIN_BOX and h >= reframe.MIN_BOX
        assert x % 2 == y % 2 == w % 2 == h % 2 == 0
        reframe.check_box([x, y, w, h], "test", 1920, 1080, aspect)  # the editor accepts it


def test_lock_aspect_keeps_the_centre_when_there_is_room():
    x, y, w, h = reframe.lock_aspect([800, 300, 300, 100], 1.0, 1920, 1080)
    assert (w, h) == (300, 300) and abs(x + w / 2 - 950) <= 2 and abs(y + h / 2 - 350) <= 2


def test_clamp_box_at_all_edges():
    assert reframe.clamp_box([-10, -10, 100, 100], 1920, 1080) == [0, 0, 100, 100]
    assert reframe.clamp_box([1900, 1000, 100, 100], 1920, 1080) == [1820, 980, 100, 100]
    assert reframe.clamp_box([0, 0, 4000, 4000], 1920, 1080) == [0, 0, 1920, 1080]


# ---- validation ---------------------------------------------------------------------------------------------


def _bad_cases():
    k = {"t": 5.0, "game": [100, 0, 606, 1078]}
    return {
        "not a list": "x",
        "empty": [],
        "too many": [seg(1 + i * 0.4, 1.4 + i * 0.4) for i in range(21)],
        "unknown layout": [seg(1, 9, layout="nope")],
        "9:16 only on 1:1": "square-1:1",
        "too short": [seg(1, 8.8), seg(8.8, 9)],
        "gap": [seg(1, 4), seg(4.5, 9)],
        "overlap": [seg(1, 4), seg(3.5, 9)],
        "first not at start": [seg(2, 9)],
        "last not at end": [seg(1, 8)],
        "box outside": [seg(1, 9, game=[1500, 0, 606, 1078])],
        "box too small": [seg(1, 9, game=[0, 0, 36, 64])],
        "wrong shape": [seg(1, 9, game=[0, 0, 600, 600])],
        "missing window": [seg(1, 9, layout="split", cam=None, game=SPLIT_GAME)],
        "key outside": [seg(1, 4, keys=[dict(k, t=5.0)]), seg(4, 9)],
        "keys unsorted": [seg(1, 9, keys=[dict(k, t=6.0), dict(k, t=5.0)])],
        "key duplicate": [seg(1, 9, keys=[dict(k, t=5.0), dict(k, t=5.0)])],
        "too many keys": [seg(1, 9, keys=[dict(k, t=1.5 + i * 0.5) for i in range(13)])],
        "key size": [seg(1, 9, keys=[{"t": 5.0, "game": [0, 0, 304, 540]}])],
        "key box outside": [seg(1, 9, keys=[{"t": 5.0, "game": [1400, 0, 606, 1078]}])],
        "key not a dict": [seg(1, 9, keys=["x"])],
        "key time nan": [seg(1, 9, keys=[dict(k, t="nan")])],
        "bad ease": [seg(1, 9, ease="bounce")],
        "bad boundary": [seg(1, 4, boundary="wipe"), seg(4, 9)],
        "smooth across layouts": [seg(1, 4, boundary="smooth"), seg(4, 9, layout="split", cam=CAM, game=SPLIT_GAME)],
        "smooth across sizes": [seg(1, 4, boundary="smooth"), seg(4, 9, game=[0, 0, 304, 540])],
        "segment not a dict": [seg(1, 4), "x"],
        "start not a number": [dict(seg(1, 9), start=None)],
    }


@pytest.mark.parametrize("name", list(_bad_cases()))
def test_each_bad_segment_list_is_a_clear_tool_error(name):
    segs = _bad_cases()[name]
    ctx = CTX
    if segs == "square-1:1":
        segs, ctx = [seg(1, 9, layout="square-frame", game=[0, 0, 1080, 900])], dict(CTX, ratio="1:1")
    with pytest.raises(ToolError) as e:
        reframe.validate_segments(segs, ctx, 1.0, 9.0)
    assert str(e.value) and e.value.hint


def test_valid_segments_are_cleaned():
    segs = two_segments()
    segs[0]["start"], segs[1]["start"], segs[0]["end"] = 1.01, 4.0004, 4.0004
    segs[1]["boundary"] = "smooth"  # the last join means nothing: it becomes a cut
    out = reframe.validate_segments(segs, CTX, 1.0, 9.0)
    assert out[0]["start"] == 1.0 and out[1]["start"] == out[0]["end"] == 4.0 and out[1]["boundary"] == "cut"
    assert out[0]["windows"] == {"cam": None, "game": GAME} and len(out[1]["keys"]) == 2
    same = [seg(1, 4, boundary="smooth"), seg(4, 9, game=[1300, 0, 606, 1078])]
    assert reframe.validate_segments(same, CTX, 1, 9)[0]["boundary"] == "smooth"


def test_caption_anchor_validation():
    assert reframe.validate_anchor(None, "9:16") is None and reframe.validate_anchor([], "9:16") is None
    assert reframe.validate_anchor([2, 1500.4], "9:16") == [2, 1500]
    for bad in ([3, 900], [2, 100], [8, 1700], "x", [2], [2, "nan"]):
        with pytest.raises(ToolError):
            reframe.validate_anchor(bad, "9:16")
    with pytest.raises(ToolError):
        reframe.validate_anchor([2, 1500], "1:1")


# ---- pan paths and the graph --------------------------------------------------------------------------------


def _eval_expr(expr: str, t: float) -> float:
    """The ffmpeg expression in Python (only numbers, t, + - * /, if() and lt() occur)."""
    assert re.fullmatch(r"[0-9t.+\-*/(),iflt ]+", expr), expr
    py = expr.replace("if(", "if_(").replace("lt(", "lt_(")
    return float(eval(py, {"if_": lambda c, a, b: a if c else b, "lt_": lambda a, b: a < b, "t": t}))  # noqa: S307


@pytest.mark.parametrize("ease", ["smooth", "linear"])
def test_pan_expr_matches_pan_value(ease):
    pts = [(4.0, 600), (5.0, 100), (8.0, 1300)]
    t0 = 3.9667  # the segment's first frame time
    expr = reframe.pan_expr(pts, t0, ease)
    for t in (3.97, 4.0, 4.25, 4.5, 4.99, 5.0, 6.5, 7.9, 8.0, 9.0):
        assert abs(_eval_expr(expr, t - t0) - reframe.pan_value(pts, t, ease)) < 1.0, t
    assert reframe.pan_value(pts, 4.5, ease) == pytest.approx(350.0)  # midpoint of both easings
    q = reframe.pan_value(pts, 4.25, ease)
    assert q == pytest.approx(600 - 500 * (0.15625 if ease == "smooth" else 0.25))
    assert reframe.pan_expr([(4.0, 600), (6.0, 600)], 4.0, ease) == "600"


def test_negative_offsets_are_written_as_plus():
    expr = reframe.pan_expr([(3.99, 10), (5.0, 20)], 4.0, "linear")
    assert "--" not in expr and "(t+0.010)" in expr


def test_smooth_boundary_eases_to_the_next_window():
    segs = [seg(1, 4, boundary="smooth"), seg(4, 9, game=[1300, 0, 606, 1078])]
    pts = reframe.window_points(segs, 0, "game")
    assert pts[-2] == (3.5, (600.0, 0.0)) and pts[-1] == (4, (1300, 0))
    xs = [(t, p[0]) for t, p in pts]
    assert reframe.pan_value(xs, 3.0, "smooth") == 600 and reframe.pan_value(xs, 3.75, "smooth") == 950
    assert reframe.window_points([seg(1, 4), seg(4, 9)], 0, "game") == [(1, (600, 0))]  # a cut holds


def test_segment_frames_are_contiguous_and_cover_the_panel():
    segs = reframe.validate_segments([seg(1.0, 3.33), seg(3.33, 6.017), seg(6.017, 9.0)], CTX, 1.0, 9.0)
    ws, we = 0.0, 10.0
    fr = reframe.segment_frames(segs, ws, we)
    assert fr[0][0] == 0 and fr[-1][1] == 300 and all(a[1] == b[0] for a, b in zip(fr, fr[1:], strict=False))
    assert sum(b - a for a, b in fr) == round((we - ws) * 30)
    pieces = reframe.frame_pieces(segs, ws)
    assert pieces[0][0] == 1.0 and pieces[-1][1] == 9.0
    for (_a, b), (c, _d) in zip(pieces, pieces[1:], strict=False):
        assert b == c and abs(b * 30 - round(b * 30)) < 1e-3


def test_graph_has_one_trim_per_segment_and_one_concat():
    segs = reframe.validate_segments(
        [
            seg(1, 3),
            seg(3, 6, layout="split", cam=CAM, game=SPLIT_GAME),
            seg(
                6, 9, layout="square-frame", game=[400, 100, 1080, 900], keys=[{"t": 7, "game": [800, 100, 1080, 900]}]
            ),
        ],
        CTX,
        1,
        9,
    )
    fc = reframe.build_panel_graph(segs, 0.0, 10.0, "9:16", 1920, 1080)
    assert fc.count("trim=start_frame") == 3 and "concat=n=3:v=1:a=0" in fc and fc.startswith("[0:v]fps=30,split=3")
    assert "trim=start_frame=0:end_frame=90" in fc and "trim=start_frame=180:end_frame=300" in fc
    assert "vstack=2" in fc and "overlay=0:470" in fc and "x='if(lt(t," in fc and fc.endswith("[v]")
    one = reframe.build_panel_graph(segs[:1], 0.0, 4.0, "9:16", 1920, 1080)
    assert one.startswith("[0:v]fps=30[s0];") and "concat=n=1" in one


def test_each_word_is_captioned_once():
    words = [{"w": "a", "s": 0.8, "e": 1.2}, {"w": "b", "s": 3.9, "e": 4.3}, {"w": "c", "s": 5.0, "e": 5.2}]
    words += [{"w": "late", "s": 9.5, "e": 9.8}]
    per = reframe.assign_words(words, [(1.0, 4.0), (4.0, 9.0)])
    assert [[w["w"] for w in p] for p in per] == [["a", "b"], ["c"]]


def test_propose_keys_follows_the_face():
    times = [1.0 + 0.5 * k for k in range(17)]  # 1.0 .. 9.0
    faces = [[[0.1 + 0.05 * k, 0.3, 0.16 + 0.05 * k, 0.45]] if k < 16 else [] for k in range(17)]
    segs = [seg(1, 4), seg(4, 9, layout="split", cam=CAM, game=SPLIT_GAME)]
    out, note = reframe.propose_keys(segs, times, faces, 1920, 1080, "9:16")
    assert "keyframe" in note
    assert out[0]["keys"] and out[1]["keys"] and all(set(k) == {"t", "cam", "game"} for k in out[1]["keys"])
    assert all(k["game"] == SPLIT_GAME for k in out[1]["keys"])  # split: only the facecam window moves
    xs = [out[1]["windows"]["cam"][0]] + [k["cam"][0] for k in out[1]["keys"]]
    assert xs == sorted(xs) and xs[-1] > xs[0]  # the face moves right, so does the window
    reframe.validate_segments(out, CTX, 1, 9)  # the proposal is a valid edit
    none, note = reframe.propose_keys(segs, times, [[] for _ in times], 1920, 1080, "9:16")
    assert none[0]["keys"] == [] and "No face" in note


LOW_CAM = [1440, 700, 440, 340]  # facecam overlay in the lower right corner of a 1920x1080 stream


def _low_cam_faces(n=13):
    # the streamer's face moves a little inside the cam box (normalised x0, y0, x1, y1), every 0.5 s
    return [[[0.80 + 0.002 * k, 0.72, 0.86 + 0.002 * k, 0.84]] for k in range(n)]


def test_auto_reframe_low_corner_cam_proposes_split():
    times = [1.0 + 0.5 * k for k in range(12)]  # 1.0 .. 6.5, no face in the second segment
    segs = [seg(1, 7, boundary="smooth"), seg(7, 9)]
    out, note = reframe.propose_keys(segs, times, _low_cam_faces(), 1920, 1080, "9:16", LOW_CAM, SPLIT_GAME)
    first = out[0]
    assert first["layout"] == "split" and first["keys"] == [] and "Split" in note
    cx, cy, cw, ch = first["windows"]["cam"]
    assert cx <= 1440 + 220 <= cx + cw and cy <= 700 + 170 <= cy + ch  # the cam box is in the facecam panel
    gx, gy, gw, gh = first["windows"]["game"]
    assert abs(cw / ch - 1080 / 672) < 0.03 and abs(gx - SPLIT_GAME[0]) <= 2 and abs(gw - SPLIT_GAME[2]) <= 2
    assert first["boundary"] == "cut"  # split -> full cannot join smoothly
    assert out[1]["layout"] == "full"  # no face there: left as it is
    reframe.validate_segments(out, CTX, 1, 9)
    # the same faces as the main scene (no corner overlay): a full-height window moves the face up
    full, _n = reframe.propose_keys(segs[:1], times, _low_cam_faces(), 1920, 1080, "9:16")
    x, y, w, h = full[0]["windows"]["game"]
    assert full[0]["layout"] == "full" and h >= reframe.MIN_ZOOM_H * 1080 - 2 and h < 1078
    face_y = (0.72 + 0.84) / 2 * 1080
    assert y + h == 1080 and (face_y - y) / h < (face_y / 1080)  # higher in the window than in the source
    assert all(k["game"][1] == y and k["game"][3] == h for k in full[0]["keys"])
    reframe.validate_segments(full, CTX, 1, 7)
    # a big cam box is not a corner overlay: no split
    big, _n = reframe.propose_keys(segs[:1], times, _low_cam_faces(), 1920, 1080, "9:16", [960, 300, 960, 780])
    assert big[0]["layout"] == "full"


# ---- real ffmpeg render -------------------------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
COLOURS = {"red": (0, 0), "green": (320, 0), "blue": (0, 180), "yellow": (320, 180)}


def _make_source(path: Path, dur: float = 6.0):
    vf = "color=c=red:s=640x360:r=30:d={d}".format(d=dur)
    for name, (x, y) in COLOURS.items():
        if name != "red":
            vf += f",drawbox=x={x}:y={y}:w=320:h=180:c={name}:t=fill"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", vf, "-f", "lavfi", "-i", f"sine=f=440:d={dur}"]
        + ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-shortest", str(path)],
        check=True,
    )


def _probe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=codec_type,nb_read_frames,duration"]
        + ["-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return {s["codec_type"]: s for s in json.loads(r.stdout)["streams"]}


def _pixel(path: Path, t: float, x: int, y: int) -> tuple:
    r = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1"]
        + ["-vf", f"crop=2:2:{x}:{y},format=rgb24", "-f", "rawvideo", "-"],
        capture_output=True,
        check=True,
    )
    return tuple(r.stdout[:3])


@needs_ffmpeg
def test_two_segment_panel_frames_audio_and_regions(tmp_path):
    from streamclipper.layout import render_reframe_panel

    src = tmp_path / "src.mp4"
    _make_source(src)
    ctx = {"width": 640, "height": 360, "ratio": "1:1"}
    segs = reframe.validate_segments(
        [
            seg(1.0, 3.5, game=[40, 10, 160, 160]),  # inside the red quadrant
            seg(3.5, 5.0, game=[60, 190, 160, 160], keys=[{"t": 4.5, "game": [100, 190, 160, 160]}]),  # blue
        ],
        ctx,
        1.0,
        5.0,
    )
    ws, we = 0.0, 6.0
    meta = {"width": 640, "height": 360}
    p = render_reframe_panel(src, tmp_path / "panels", meta, segs, ws, we, "1:1")
    out = Path(p["main_src"])
    assert out.is_file() and p["layout"] == "full" and p["caption_anchor"] is None
    st = _probe(out)
    assert int(st["video"]["nb_read_frames"]) == round((we - ws) * 30)
    assert abs(float(st["audio"]["duration"]) - (we - ws)) < 0.1
    r = _pixel(out, 2.0, 540, 540)
    b = _pixel(out, 4.2, 540, 540)
    assert r[0] > 180 and r[2] < 80, r  # segment 1 shows the red region
    assert b[2] > 180 and b[0] < 80, b  # segment 2 shows the blue region
    again = render_reframe_panel(src, tmp_path / "panels", meta, segs, ws, we, "1:1")
    assert again["main_src"] == p["main_src"]  # cached by the segment list


@needs_ffmpeg
def test_caption_timing_across_segments(tmp_path):
    """Words keep their source times in the plan; each word's ASS events fall inside its segment's timeline range."""
    from streamclipper.captions.ass import build_ass
    from streamclipper.commands import build_high_level_clip
    from streamclipper.layout import apply_caption_anchor, layout_boxes

    src = tmp_path / "src.mp4"
    _make_source(src)
    words = [
        {"word": w, "start": 1.2 + i * 0.6, "end": 1.6 + i * 0.6}
        for i, w in enumerate("one two three four five six".split())
    ]
    segs = [
        seg(1.0, 3.0, game=[40, 10, 160, 160]),
        seg(3.0, 5.0, game=[60, 190, 160, 160], keys=[{"t": 4.0, "game": [100, 190, 160, 160]}]),
    ]
    spec = {
        "draft_name": "rf",
        "source_video": str(src),
        "start": 1.0,
        "end": 5.0,
        "ratio": "1:1",
        "layout": "full",
        "trim_silence": False,
        "caption_style": "bold-pop",
        "words": words,
        "reframe_segments": segs,
        "caption_anchor": [8, 300],
    }
    args = SimpleNamespace(
        draft_root="", no_draft=True, close_capcut=False, out=str(tmp_path / "out"), overwrite=True, render=False
    )
    build_high_level_clip(spec, tmp_path, args)
    plan = json.loads((tmp_path / "out" / "rf.clip.json").read_text(encoding="utf-8"))
    assert len(plan["reframe_segments"]) == 2 and plan["panels"]["caption_anchor"] == [8, 300]
    assert [(s["src_s"], s["src_e"], s["fade_ms"]) for s in plan["segments"]] == [(1.0, 3.0, 0), (3.0, 5.0, 0)]
    assert [w["s"] for w in plan["words"]] == [round(w["start"], 3) for w in words]  # ws = 0: source times
    tl2 = (plan["segments"][1]["tl_start"], plan["expected_duration"])
    assert tl2 == (2.0, 4.0)
    lay = apply_caption_anchor(layout_boxes("1:1", "full"), plan["panels"]["caption_anchor"])
    build_ass(plan, lay, tmp_path / "clip.ass", tmp_path / "fonts")
    text = (tmp_path / "clip.ass").read_text(encoding="utf-8")

    def secs(s):
        h, m, x = s.split(":")
        return int(h) * 3600 + int(m) * 60 + float(x)

    events = [
        (secs(a), secs(b), txt) for a, b, txt in re.findall(r"Dialogue: \d+,([\d:.]+),([\d:.]+),[^\n]*?,,(.*)", text)
    ]
    for w in ("FOUR", "FIVE", "SIX"):  # words of segment 2
        evs = [e for e in events if w in e[2].upper()]
        assert evs, w
        assert all(tl2[0] - 0.02 <= a and b <= tl2[1] + 0.02 for a, b, _t in evs), (w, evs)
    for w in ("ONE", "TWO"):
        assert all(b <= tl2[0] + 0.05 for a, b, t in events if w in t.upper() and "FOUR" not in t.upper())
