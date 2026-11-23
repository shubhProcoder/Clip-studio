import json

import pytest

from dashboard_util import start, stop
from streamclipper.dashboard import editor, library


@pytest.fixture
def dash(tmp_path, monkeypatch):
    d = start(tmp_path, monkeypatch)
    a = d.new_job(max_clips=2)
    d.wait(a)
    d.job = a
    yield d
    stop(d)


def _ctx(d):
    st, c = d.json("GET", f"/api/jobs/{d.job}/clips/clip_01/edit")
    assert st == 200, c
    return c


def test_context(dash):
    c = _ctx(dash)
    assert c["bounds"] == [0.0, 30.0] and c["original"]["start"] == 1.0 and c["original"]["end"] == 25.0
    assert [w["w"] for w in c["words"]] == ["no", "way", "bro", "thats", "insane"]
    split = c["layouts"]["split"]
    assert abs(split["cam"][2] / split["cam"][3] - 1080 / 672) < 0.01
    assert abs(split["game"][2] / split["game"][3] - 1080 / 1248) < 0.01
    assert c["layouts"]["speaker"]["cam"] is None and c["layouts"]["full"]["game"]
    assert len(c["wave"]["peaks"]) == 300
    st, _h, body = dash.req("GET", c["video_url"], headers={"Range": "bytes=0-9"})
    assert st == 206 and len(body) == 10


def _edit(c, **kw):
    e = json.loads(json.dumps(c["current"]))
    e.update(kw)
    return e


def test_validation_errors(dash):
    c = _ctx(dash)
    url = f"/api/jobs/{dash.job}/clips/clip_01/edit"
    cam = c["current"]["cam"]
    bad = [
        {"end": 31.0},
        {"start": 20.0, "end": 20.5},
        {"start": "nan"},
        {"cam": [1900, 0, cam[2], cam[3]]},
        {"cam": [0, 0, 40, 25]},
        {"cam": [0, 0, 400, 400]},
        {"words": ["only one"]},
        {"words": ["x" * 41, "a", "b", "c", "d"]},
        {"words": ["", "a", "b", "c", "d"]},
        {"caption_style": "evil"},
        {"layout": "hook-grabber"},
        {"hook_text": "x" * 81},
        {"title": "t" * 101},
        {"hashtags": ["#a"] * 16},
    ]
    for b in bad:
        st, r = dash.json("POST", url, {"action": "render", "edit": _edit(c, **b)})
        assert st == 400 and r["error"], b
    assert dash.json("POST", url, {"action": "nope", "edit": c["current"]})[0] == 400
    assert dash.json("POST", url, {"action": "render", "edit": "x"})[0] == 400


def test_save_render_undo_and_draft(dash, monkeypatch):
    c = _ctx(dash)
    url = f"/api/jobs/{dash.job}/clips/clip_01/edit"
    cam = c["current"]["cam"]
    words = ["no", "WAY", "bro", "that's", "insane"]
    e = _edit(
        c, end=28.0, cam=[cam[0] + 20, cam[1] + 10, cam[2], cam[3]], words=words, title="New title", hashtags=["fun"]
    )
    st, r = dash.json("POST", url, {"action": "save", "edit": e})
    assert st == 200 and _ctx(dash)["pending"]["end"] == 28.0
    out = dash.srv.ws / "jobs" / dash.job / "out"
    doc = json.loads((out / "clips.json").read_text(encoding="utf-8"))
    assert doc["clips"][0]["title"] == "New title" and doc["clips"][0]["hashtags"] == ["#fun"]

    st, r = dash.json("POST", url, {"action": "render", "edit": e})
    assert st == 200 and r["name"] == "clip_01_2"
    rj = dash.wait(r["job"])
    spec_path = dash.srv.ws / "jobs" / dash.job / "edits" / "clip_01" / "v1.json"
    assert rj["argv"] if "argv" in rj else True
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    assert spec["end"] == 28.0 and spec["trim_silence"] is False and spec["layout"] == "split"
    assert spec["cam_crop"] == ",".join(str(v) for v in e["cam"]) and [w["word"] for w in spec["words"]] == words
    assert [w["start"] for w in spec["words"]] == [w["s"] for w in c["words"]]
    job = dash.srv.store.get(r["job"])
    assert job["argv"] == ["render", "--spec", str(spec_path), "--out", str(out)]
    assert (out / "clip_01_2.mp4").is_file()
    c2 = _ctx(dash)
    h = c2["history"][-1]
    assert h["status"] == "done" and h["mp4_url"].endswith("clip_01_2.mp4") and h["gate"]["pass"]
    assert c2["current"]["end"] == 28.0 and c2["pending"] is None
    lib = dash.json("GET", f"/api/jobs/{dash.job}/clips")[1]["clips"][0]
    assert lib["variants"][0]["name"] == "clip_01_2" and lib["variants"][0]["edit"] == 1

    dash.json("POST", url, {"action": "save", "edit": _edit(c2, end=26.0)})
    st, r = dash.json("POST", url, {"action": "undo", "edit": None})
    assert st == 200 and r["current"]["end"] == 28.0 and _ctx(dash)["pending"] is None

    monkeypatch.setattr(editor._env, "capcut_running", lambda: True)
    st, r = dash.json("POST", url, {"action": "draft", "edit": e})
    assert st == 409 and r["error"] == library.CAPCUT_OPEN_MSG
    monkeypatch.setattr(editor._env, "capcut_running", lambda: False)
    st, r = dash.json("POST", url, {"action": "draft", "edit": e})
    assert st == 200
    dj = dash.wait(r["job"])
    assert dj["status"] == "done" and dash.srv.store.get(r["job"])["argv"][0] == "build"


def test_square_frame_keeps_the_chosen_crop(dash):
    c = _ctx(dash)
    g = c["layouts"]["square-frame"]["game"]
    form = editor.validate(c, _edit(c, layout="square-frame", cam=None, game=g))
    spec = editor.clip_spec(c, form, {}, "v.mp4", "clip_01")
    assert spec["cam_crop"] == spec["game_crop"] == ",".join(map(str, g))
    from streamclipper.layout import frame_crop

    x, y, w, h = g
    assert frame_crop(1920, 1080, (x, y, w, h), (x, y, w, h), 1080 / 900) == (w, h, x, y)


def test_source_probe_is_cached(tmp_path, monkeypatch):
    from types import SimpleNamespace

    video = tmp_path / "stream.mp4"
    video.write_bytes(b"\x00" * 100)
    calls = []
    monkeypatch.setattr(editor, "run_cmd", lambda argv, what: calls.append(argv) or SimpleNamespace(stdout="12.5\n"))
    cache = tmp_path / "edits" / "clip_01" / "probe.json"
    assert editor._probe_dur(video, cache) == 12.5
    assert editor._probe_dur(video, cache) == 12.5 and len(calls) == 1  # the reopen reads the cache
    video.write_bytes(b"\x00" * 200)  # a changed file is probed again
    assert editor._probe_dur(video, cache) == 12.5 and len(calls) == 2


def _two_segments(c):
    lay = c["layouts"]
    g = lay["full"]["game"]
    moved = [min(1920 - g[2], g[0] + 200) & ~1, g[1], g[2], g[3]]
    return [
        {
            "start": 1.0,
            "end": 12.0,
            "layout": "split",
            "windows": {"cam": lay["split"]["cam"], "game": lay["split"]["game"]},
        },
        {
            "start": 12.0,
            "end": 25.0,
            "layout": "full",
            "windows": {"cam": None, "game": g},
            "keys": [{"t": 15.0, "game": moved}, {"t": 20.0, "game": g}],
            "ease": "smooth",
            "boundary": "cut",
        },
    ]


def test_two_segment_edit_saves_and_renders(dash):
    c = _ctx(dash)
    url = f"/api/jobs/{dash.job}/clips/clip_01/edit"
    e = _edit(c, segments=_two_segments(c), caption_anchor=[2, 1400])
    st, r = dash.json("POST", url, {"action": "save", "edit": e})
    assert st == 200, r
    pend = _ctx(dash)["pending"]
    assert len(pend["segments"]) == 2 and pend["caption_anchor"] == [2, 1400] and pend["layout"] == "split"
    st, r = dash.json("POST", url, {"action": "render", "edit": e})
    assert st == 200, r
    dash.wait(r["job"])
    spec = json.loads((dash.srv.ws / "jobs" / dash.job / "edits" / "clip_01" / "v1.json").read_text(encoding="utf-8"))
    assert spec["layout"] == "full" and len(spec["reframe_segments"]) == 2 and spec["caption_anchor"] == [2, 1400]
    assert spec["reframe_segments"][1]["keys"][0]["t"] == 15.0 and spec["caption_position"] == "auto"
    # one segment without keyframes is the old form: no reframe fields, the old layout path
    one = _edit(c, segments=_two_segments(c)[:1], end=25.0)
    one["segments"][0]["end"] = 25.0
    form = editor.validate(c, one)
    assert "segments" not in form and form["layout"] == "split"
    assert "reframe_segments" not in editor.clip_spec(c, form, {}, "v.mp4", "clip_01")


def test_bad_segments_are_400(dash):
    c = _ctx(dash)
    url = f"/api/jobs/{dash.job}/clips/clip_01/edit"
    g = c["layouts"]["full"]["game"]
    cases = []
    s = _two_segments(c)
    s[1]["start"] = 11.0  # overlap
    cases.append(s)
    s = _two_segments(c)
    s[1]["start"] = 13.0  # gap
    cases.append(s)
    s = _two_segments(c)
    s[1]["windows"]["game"] = [1900, 0, g[2], g[3]]  # outside
    cases.append(s)
    s = _two_segments(c)
    s[1]["windows"]["game"] = [0, 0, 36, 64]  # too small
    cases.append(s)
    s = _two_segments(c)
    s[1]["keys"] = [{"t": 13.0 + i * 0.5, "game": g} for i in range(13)]  # too many keyframes
    cases.append(s)
    for segs in cases:
        st, r = dash.json("POST", url, {"action": "render", "edit": _edit(c, segments=segs)})
        assert st == 400 and r["error"] and r["hint"], segs
    st, r = dash.json("POST", url, {"action": "save", "edit": _edit(c, caption_anchor=[2, 50])})
    assert st == 400 and "edge" in r["error"]


def test_auto_reframe_runs_in_the_background(dash, monkeypatch):
    import time

    from streamclipper import vision

    c = _ctx(dash)
    base = f"/api/jobs/{dash.job}/clips/clip_01"
    times = [1.0 + 0.5 * k for k in range(49)]

    def fake_track(video, start, end, step, vw, vh):
        time.sleep(0.2)
        return {"times": times, "faces": [[[0.1 + 0.01 * k, 0.3, 0.2 + 0.01 * k, 0.5]] for k in range(49)]}

    monkeypatch.setattr(vision, "track_faces", fake_track)
    st, r = dash.json("POST", f"{base}/reframe", {"edit": _edit(c, segments=_two_segments(c))})
    assert st == 202 and r["running"] is True
    for _ in range(100):
        st, r = dash.json("GET", f"{base}/reframe")
        if not r["running"]:
            break
        time.sleep(0.05)
    assert st == 200 and not r["error"] and len(r["segments"]) == 2 and r["faces"], r
    assert r["segments"][0]["keys"] and all(set(k) == {"t", "cam", "game"} for k in r["segments"][0]["keys"])
    assert _ctx(dash)["faces"]  # cached for the caption face warning
    monkeypatch.setattr(vision, "track_faces", lambda *a: None)
    st, r = dash.json("POST", f"{base}/reframe", {"edit": c["current"]})
    assert st == 202
    for _ in range(100):
        r = dash.json("GET", f"{base}/reframe")[1]
        if not r["running"]:
            break
        time.sleep(0.05)
    assert "OpenCV" in r["error"] and "streamclipper[vision]" in r["hint"]
    assert dash.json("POST", f"{base}/reframe", {"edit": "x"})[0] == 400
    assert "OpenCV" in dash.json("GET", f"{base}/reframe")[1]["error"]  # a bad request keeps the last result


def test_two_quick_auto_reframe_posts_start_one_run(dash, monkeypatch):
    import threading
    import time

    from streamclipper import vision

    c = _ctx(dash)
    base = f"/api/jobs/{dash.job}/clips/clip_01"
    calls, go = [], threading.Event()

    def fake_track(video, start, end, step, vw, vh):
        calls.append(start)
        time.sleep(0.3)
        return {"times": [1.0, 2.0], "faces": [[[0.1, 0.3, 0.2, 0.5]], [[0.1, 0.3, 0.2, 0.5]]]}

    monkeypatch.setattr(vision, "track_faces", fake_track)
    real_context = editor.context

    def slow_context(*a, **kw):  # widen the window between the check and the thread start
        time.sleep(0.1)
        return real_context(*a, **kw)

    monkeypatch.setattr(editor, "context", slow_context)
    res = []

    def post():
        go.wait()
        res.append(dash.json("POST", f"{base}/reframe", {"edit": c["current"]}))

    ts = [threading.Thread(target=post) for _ in range(2)]
    for t in ts:
        t.start()
    go.set()
    for t in ts:
        t.join(10)
    codes = sorted(st for st, _r in res)
    assert codes == [202, 409], res
    assert "already running" in next(r for st, r in res if st == 409)["error"]
    for _ in range(100):
        if not dash.json("GET", f"{base}/reframe")[1]["running"]:
            break
        time.sleep(0.05)
    assert len(calls) == 1


def test_thumbs_sprite_is_made_once(dash, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from streamclipper.util import ToolError

    calls = []

    def fake_run(argv, what, **kw):
        if "editor thumbnails" != what:
            raise ToolError("no ffmpeg in this test")
        calls.append(argv)
        Path(argv[-1]).write_bytes(b"\xff\xd8fakejpg")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(editor, "run_cmd", fake_run)
    c = _ctx(dash)
    assert c["thumbs"]["n"] == 30 and c["thumbs"]["w"] == 160 and c["thumbs"]["h"] == 90
    for _ in range(2):
        st, h, body = dash.req("GET", c["thumbs"]["url"])
        assert st == 200 and h["Content-Type"] == "image/jpeg" and body == b"\xff\xd8fakejpg"
    assert len(calls) == 1 and "tile=30x1" in " ".join(calls[0])


def test_effects_go_into_the_render_spec(dash):
    c = _ctx(dash)
    assert c["current"]["zoom"] == "off" and c["current"]["remove_fillers"] is False
    assert c["effect_choices"]["zoom"] == ["off", "light", "medium", "strong"] and c["brand_kit_ready"] is False
    url = f"/api/jobs/{dash.job}/clips/clip_01/edit"
    for bad in ({"zoom": "max"}, {"enhance_speech": "x"}, {"remove_fillers": "maybe"}):
        assert dash.json("POST", url, {"action": "render", "edit": _edit(c, **bad)})[0] == 400, bad
    st, r = dash.json("POST", url, {"action": "render", "edit": _edit(c, brand_kit=True)})
    assert st == 400 and "brand kit" in r["error"].lower()
    e = _edit(c, zoom="medium", remove_fillers=True, enhance_speech="light")
    st, r = dash.json("POST", url, {"action": "render", "edit": e})
    assert st == 200, r
    dash.wait(r["job"])
    spec_path = dash.srv.ws / "jobs" / dash.job / "edits" / "clip_01" / "v1.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    assert spec["zoom"] == "medium" and spec["remove_fillers"] is True and spec["enhance_speech"] == "light"
    assert "brand_kit" not in spec and spec["editor_form"]["zoom"] == "medium"
