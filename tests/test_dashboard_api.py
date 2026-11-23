import re
import struct

import pytest

from dashboard_util import start, stop
from streamclipper.captions.styles import HOOK_STYLES, STYLES
from streamclipper.dashboard import server


@pytest.fixture
def dash(tmp_path, monkeypatch):
    d = start(tmp_path, monkeypatch)
    yield d
    stop(d)


def _jpeg_size(path):
    data = path.read_bytes()
    i = 2
    while i < len(data):
        marker, seglen = data[i + 1], struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return w, h
        i += 2 + seglen
    raise AssertionError("no SOF marker")


def test_options_and_gallery_geometry(dash, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    st, o = dash.json("GET", "/api/options")
    assert st == 200
    assert "bold-pop" in o["caption_styles"] and o["caption_styles"][0] == "auto"
    assert o["hook_styles"] == list(HOOK_STYLES) and "podcast-9x16" in o["presets"]
    assert o["llms"] == ["none"] and o["mode"] == "local"
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert dash.json("GET", "/api/options")[1]["llms"] == ["none", "gemini"]
    g = o["gallery"]
    assert len(g["tiles"]) == len(STYLES) + len(HOOK_STYLES)
    assert _jpeg_size(server.STATIC_DIR / "styles_gallery.jpg") == (g["width"], g["height"])


def test_index_static_and_headers(dash):
    st, h, body = dash.req("GET", "/")
    assert st == 200 and b'<meta name="csrf" content="' + dash.csrf.encode() in body
    assert "default-src 'self'" in h["Content-Security-Policy"] and h["X-Content-Type-Options"] == "nosniff"
    for name in ("app.js", "icons.js", "editor.js", "app.css", "theme.js", "logo.png", "styles_gallery.jpg"):
        st, h, body = dash.req("GET", f"/static/{name}")
        assert st == 200 and body, name
    # every script and stylesheet the page loads is served (STATIC_TYPES is a whitelist)
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', body_of_index(dash))
    assert len(refs) >= 6
    for ref in refs:
        assert dash.req("GET", ref)[0] == 200, ref
    for name in ("app.js", "icons.js", "editor.js"):
        assert "innerHTML" not in (server.STATIC_DIR / name).read_text(encoding="utf-8"), name
    assert "ISC License" in (server.STATIC_DIR / "icons.js").read_text(encoding="utf-8")
    assert "sc_theme" in (server.STATIC_DIR / "theme.js").read_text(encoding="utf-8")
    css = (server.STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert '[data-theme="dark"]' in css and "#09090b" in css and "#8b5cf6" in css.lower()


def body_of_index(dash) -> str:
    return dash.req("GET", "/")[2].decode("utf-8")


def test_source_kind():
    sk = server.source_kind
    assert sk("https://www.twitch.tv/videos/123") == "twitch" and sk("https://m.twitch.tv/x") == "twitch"
    assert sk("https://youtu.be/abc") == "youtube" and sk("https://www.youtube.com/watch?v=a") == "youtube"
    assert sk("https://kick.com/x") == "kick" and sk("https://example.com/v.mp4") == "link"
    assert sk(r"D:\Videos\a.mp4") == "file" and sk("") == "file" and sk("https://[bad") == "link"
    assert sk("https://nottwitch.tv/x") == "link"


def test_job_cards_have_thumb_count_and_source(dash):
    import time

    ids = {
        "twitch": dash.new_job(source="https://www.twitch.tv/videos/1", max_clips=2),
        "youtube": dash.new_job(source="https://www.youtube.com/watch?v=x"),
        "kick": dash.new_job(source="https://kick.com/someone/videos/1"),
    }
    for jid in ids.values():
        dash.wait(jid)
    jobs = {j["id"]: j for j in dash.json("GET", "/api/jobs")[1]["jobs"]}
    for kind, jid in ids.items():
        assert jobs[jid]["source_kind"] == kind
    tw = jobs[ids["twitch"]]
    assert tw["clip_count"] == 2 and tw["thumb_url"] == f"/files/{ids['twitch']}/out/clip_01_thumb.jpg"
    assert dash.req("GET", tw["thumb_url"])[0] == 200
    # a deleted clip no longer counts, and the next clip's thumbnail is used
    delete = f"/api/jobs/{ids['twitch']}/clips/clip_01/delete"
    st, r = dash.json("POST", delete, {"confirm": "maybe"})
    assert st == 400 and "confirm must be true or false" in r["error"]
    assert dash.json("POST", delete, {"confirm": "false"})[0] == 400  # not confirmed
    assert dash.json("POST", delete, {"confirm": True})[0] == 200
    tw = next(j for j in dash.json("GET", "/api/jobs")[1]["jobs"] if j["id"] == ids["twitch"])
    assert tw["clip_count"] == 1 and tw["thumb_url"].endswith("/clip_02_thumb.jpg")
    # file jobs, and jobs without clips yet
    store = dash.srv.store
    for i in range(20):
        store.create("vod", f"stream_{i}.mp4", ["x"], {"mode": "vod"})
    t0 = time.perf_counter()
    st, r = dash.json("GET", "/api/jobs")
    took = time.perf_counter() - t0
    assert st == 200 and took < 0.2, took
    files = [j for j in r["jobs"] if j["label"].startswith("stream_")]
    assert len(files) == 20 and all(
        j["source_kind"] == "file" and j["clip_count"] == 0 and j["thumb_url"] == "" for j in files
    )


def test_job_booleans_are_strict(dash):
    st, r = dash.json("POST", "/api/jobs", {"source": "https://example.com/vod", "render": "maybe"})
    assert st == 400 and "render must be true or false" in r["error"] and r["hint"]
    st, j = dash.json("POST", "/api/jobs", {"source": "https://example.com/vod", "render": "false", "drafts": "0"})
    assert st == 201 and j["options"]["render"] is False and j["options"]["drafts"] is False, j


def test_vod_job_runs_queue_cancel_resume_files_mark_rerender(dash, tmp_path):
    a = dash.new_job(max_clips=2, render=True, drafts=True)
    seen = set()
    import time

    t0 = time.time()
    while time.time() - t0 < 20:
        j = dash.json("GET", f"/api/jobs/{a}")[1]
        seen.add((j["progress"] or {}).get("stage"))
        if j["status"] == "done":
            break
        time.sleep(0.02)
    assert j["status"] == "done" and "ASR chunk 2/2" in j["log_tail"]
    assert {"transcribe", "render"} <= seen or "finishing" in seen
    st, lib = dash.json("GET", f"/api/jobs/{a}/clips")
    assert st == 200 and [c["key"] for c in lib["clips"]] == ["clip_01", "clip_02"]
    c = lib["clips"][0]
    assert c["gate"]["pass"] and c["gate"]["rows"][0][0] == "resolution"
    assert c["mp4_url"] == f"/files/{a}/out/clip_01.mp4" and c["draft"] == "clip_01"
    st, h, body = dash.req("GET", c["mp4_url"], headers={"Range": "bytes=0-1"})
    assert st == 206 and body == b"\x00\x01" and h["Content-Range"].endswith("/4096")
    st, h, body = dash.req("GET", c["mp4_url"] + "?download=1")
    assert st == 200 and "attachment" in h["Content-Disposition"] and len(body) == 4096

    assert dash.json("POST", f"/api/jobs/{a}/clips/clip_01/mark", {"mark": "posted"})[0] == 200
    st, r = dash.json("POST", f"/api/jobs/{a}/clips/clip_01/rerender", {"caption_style": "comic-burst"})
    assert st == 202 and r["variant"]["name"] == "clip_01_2"
    dash.wait(r["variant"]["job"])
    st, lib = dash.json("GET", f"/api/jobs/{a}/clips")
    c = lib["clips"][0]
    assert c["mark"] == "posted" and c["variants"][0]["name"] == "clip_01_2" and c["variants"][0]["mp4_url"]
    jobs = dash.json("GET", "/api/jobs")[1]["jobs"]
    rj = next(j for j in jobs if j["id"] == r["variant"]["job"])
    assert rj["kind"] == "render"

    # queue: the second job waits while the first runs; cancel; resume
    import os

    os.environ["SC_FAKE_SLEEP"] = "0.4"
    b, c2 = dash.new_job(), dash.new_job()
    dash.wait(b, ("running",))
    assert dash.json("GET", f"/api/jobs/{c2}")[1]["status"] == "queued"
    assert dash.json("POST", f"/api/jobs/{c2}/cancel")[1]["status"] == "cancelled"
    assert dash.json("POST", f"/api/jobs/{b}/cancel")[1]["status"] == "cancelled"
    dash.wait(b, ("cancelled",))
    os.environ["SC_FAKE_SLEEP"] = "0.02"
    assert dash.json("POST", f"/api/jobs/{b}/resume")[0] == 200
    assert dash.wait(b)["status"] == "done"


def test_restart_lists_old_jobs(tmp_path, monkeypatch):
    d = start(tmp_path, monkeypatch)
    a = d.new_job()
    d.wait(a)
    stop(d)
    d2 = start(tmp_path, monkeypatch)
    try:
        assert a in [j["id"] for j in d2.json("GET", "/api/jobs")[1]["jobs"]]
    finally:
        stop(d2)


def test_live_job_start_and_stop(dash, tmp_path):
    src = tmp_path / "fake_live.mp4"
    src.write_bytes(b"x")
    st, j = dash.json("POST", "/api/jobs", {"mode": "live", "source": str(src), "min_score": 0, "stop_after": 2})
    assert st == 201 and j["kind"] == "live"
    import time

    t0 = time.time()
    clips = []
    while time.time() - t0 < 15 and not clips:
        clips = dash.json("GET", f"/api/jobs/{j['id']}/clips")[1]["clips"]
        time.sleep(0.1)
    assert clips and clips[0]["latency_s"] > 0
    assert dash.json("POST", f"/api/jobs/{j['id']}/stop")[1]["status"] == "stopped"
    done = dash.wait(j["id"], ("stopped", "done"))
    assert done["status"] == "stopped"
    assert (tmp_path / "ws" / "jobs" / j["id"] / "out" / "live_summary.json").is_file()


def test_settings_and_doctor(dash, monkeypatch, tmp_path):
    monkeypatch.setattr(server.paths, "USER_DIR", tmp_path / "user")
    st, s = dash.json(
        "POST", "/api/settings", {"defaults": {"caption_style": "comic-burst"}, "workspace": str(tmp_path / "w2")}
    )
    assert st == 200 and "Restart" in s["message"] and s["defaults"]["caption_style"] == "comic-burst"
    assert dash.json("GET", "/api/options")[1]["defaults"]["caption_style"] == "comic-burst"
    assert dash.json("POST", "/api/settings", {"defaults": {"caption_style": "bad"}})[0] == 400
    monkeypatch.setattr("streamclipper.env.detect_env", lambda save=False: {})
    monkeypatch.setattr("streamclipper.env.doctor_checks", lambda env, assets: [("ffmpeg", "OK", "x", "")])
    assert dash.json("POST", "/api/doctor")[0] == 202
    import time

    for _ in range(100):
        r = dash.json("GET", "/api/doctor")[1]
        if r["rows"]:
            break
        time.sleep(0.05)
    assert r["rows"][0][0] == "ffmpeg"


def test_slow_checks_run_off_the_request_thread(dash, monkeypatch):
    import threading
    import time

    calls = []
    gate = threading.Event()

    def slow():
        calls.append(1)
        gate.wait(5)
        return len(calls) > 1

    c = server.Cached(slow, ttl=-1.0, default=False)
    t0 = time.perf_counter()
    assert c.get(wait=0.2) is False and time.perf_counter() - t0 < 1.0  # first read waits at most `wait`
    gate.set()
    c.ready.wait(2)
    gate.clear()
    t0 = time.perf_counter()
    assert c.get() is False and time.perf_counter() - t0 < 0.2  # later reads never wait for a refresh
    gate.set()
    for _ in range(100):
        if c.value is True:
            break
        time.sleep(0.02)
    assert c.value is True and len(calls) == 2
    # /api/settings and /api/options read the cached checks: a slow CapCut/Ollama check never blocks them
    monkeypatch.setattr(dash.srv.capcut_open, "fn", lambda: time.sleep(3) or True)
    monkeypatch.setattr(dash.srv.capcut_open, "ttl", -1.0)
    for path in ("/api/settings", "/api/options", "/api/settings"):
        t0 = time.perf_counter()
        assert dash.json("GET", path)[0] == 200
        assert time.perf_counter() - t0 < 1.0, path


def test_brand_kit_upload_save_and_use_in_a_job(dash):
    from pathlib import Path

    logo = (Path(__file__).resolve().parents[1] / "docs" / "examples" / "brand_logo.png").read_bytes()
    st, b = dash.json("GET", "/api/brand")
    assert st == 200 and b["kit"] is None and b["files"] == {"images": [], "fonts": []}
    assert dash.json("GET", "/api/options")[1]["brand_kit"] is False
    # no kit yet: a job with the brand kit on is refused
    st, e = dash.json("POST", "/api/jobs", {"source": "https://example.com/v", "brand_kit": True})
    assert st == 400 and "brand kit" in e["error"].lower()
    st, r = dash.json("PUT", "/api/brand/upload?name=my%20logo.png", raw=logo)
    assert st == 201 and r["file"] == "my logo.png"
    assert dash.req("PUT", "/api/brand/upload?name=evil.exe", raw=b"MZ")[0] == 415
    assert dash.req("PUT", "/api/brand/upload?name=..%2F..%2Fx.png", raw=logo)[0] == 201
    assert not (dash.srv.ws / "x.png").exists() and (dash.srv.ws / "brand" / "x.png").is_file()
    bad = {"logo": {"image": "missing.png"}}
    assert dash.json("POST", "/api/brand", bad)[0] == 400
    kit = {"logo": {"image": "my logo.png", "position": "auto"}, "handle": "@me", "intro": {"text": "HI", "seconds": 1}}
    st, r = dash.json("POST", "/api/brand", kit)
    assert st == 200 and r["kit"]["handle"] == "@me" and not r["error"]
    assert dash.json("GET", "/api/options")[1]["brand_kit"] is True
    jid = dash.new_job(source="https://example.com/v", brand_kit=True, zoom="medium", remove_fillers=True)
    argv = dash.srv.store.get(jid)["argv"]
    assert argv[argv.index("--brand-kit") + 1] == str(dash.srv.ws / "brand" / "brand_kit.json")
    assert argv[argv.index("--zoom") + 1] == "medium" and "--remove-fillers" in argv
    assert dash.json("POST", "/api/brand", {"clear": True})[1]["kit"] is None


def test_brand_kit_is_read_only_on_the_network(tmp_path, monkeypatch):
    d = start(tmp_path, monkeypatch, lan=True)
    try:
        assert d.json("GET", "/api/brand")[1]["read_only"] is True
        assert d.json("POST", "/api/brand", {"handle": "@x"})[0] == 403
        assert d.req("PUT", "/api/brand/upload?name=a.png", raw=b"\x89PNG")[0] == 403
    finally:
        stop(d)
