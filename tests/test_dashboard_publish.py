import json
import sys
import threading
import time
from pathlib import Path

import pytest

from dashboard_util import start, stop
from publish_util import guard
from streamclipper.dashboard import server
from streamclipper.publish import http, store
from streamclipper.publish import youtube as yt
from streamclipper.publish.http import MockTransport

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import dashboard_fake_cli as fake  # noqa: E402

CID = "1234-abc.apps.googleusercontent.com"
ACCESS, REFRESH = "ya29.DASHBOARD-ACCESS", "1//DASHBOARD-REFRESH"
SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = guard(monkeypatch, tmp_path)
    gate = threading.Event()
    gate.set()
    seen = {"n": 0}

    def handler(method, url, headers, body):
        gate.wait(10)
        if url.startswith(yt.TOKEN_URL):
            return 200, {}, {"access_token": ACCESS, "expires_in": 3599}
        if url.startswith(yt.UPLOAD_URL):
            return 200, {"Location": SESSION}, b""
        if url.startswith(SESSION):
            seen["n"] += 1
            return (
                200,
                {},
                {"id": f"VID{seen['n']}", "status": {"privacyStatus": "private", "publishAt": json.loads("null")}},
            )
        if "graph.facebook.com" in url and url.endswith("fields=username"):
            return 200, {}, {"username": "my.channel"}
        raise AssertionError(url)

    t = MockTransport(handler)
    monkeypatch.setattr(http, "make_transport", lambda: t)
    d = start(tmp_path, monkeypatch)
    jid = d.srv.store.create("vod", "x", ["clip-stream"], {})["id"]
    d.srv.store.update(jid, status="done")
    out = d.srv.store.job_dir(jid) / "out"
    out.mkdir(parents=True)
    for r in (1, 2):
        fake.write_clip(out, f"clip_{r:02d}", r)
    (out / "clips.json").write_text(json.dumps(fake.clip_doc(2)), encoding="utf-8")
    yield {"d": d, "jid": jid, "t": t, "gate": gate, "cfg": cfg, "tmp": tmp_path}
    gate.set()
    stop(d)


def _connect_youtube():
    store.save_token(
        "youtube",
        {"client_id": CID, "client_secret": "GOCSPX-secret", "access_token": ACCESS, "refresh_token": REFRESH,
         "expires_at": time.time() + 3600, "account": "me@example.com"},
    )  # fmt: skip


def _wait(d, jid, key, platform, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, s = d.json("GET", f"/api/jobs/{jid}/clips/{key}/publish")
        if st == 200 and not s["running"] and platform in s["publish"]:
            return s["publish"][platform]
        time.sleep(0.05)
    raise AssertionError("publish did not finish")


def test_publish_youtube_from_the_dashboard(env):
    d, jid, t = env["d"], env["jid"], env["t"]
    base = f"/api/jobs/{jid}/clips/clip_01/publish"
    st, e = d.json("POST", base, {"platform": "youtube"})
    assert st == 400 and "not connected" in e["error"]
    _connect_youtube()
    st, r = d.json(
        "POST", base, {"platform": "youtube", "title": "My title", "tags": "#gta #wow", "privacy": "unlisted"}
    )
    assert st == 202 and r["started"] and r["platform"] == "youtube"
    entry = _wait(d, jid, "clip_01", "youtube")
    assert entry["state"] == "uploaded" and entry["url"] == "https://www.youtube.com/shorts/VID1"
    init = [c for c in t.calls if c["method"] == "POST"][0]
    assert init["body"]["snippet"]["title"] == "My title" and init["body"]["status"]["privacyStatus"] == "unlisted"
    assert init["body"]["snippet"]["description"].endswith("#Shorts #gta #wow")
    clips = d.json("GET", f"/api/jobs/{jid}/clips")[1]["clips"]
    assert clips[0]["publish"]["youtube"]["state"] == "uploaded" and clips[1]["publish"] == {}
    # a time in the past is refused before anything starts
    st, e = d.json("POST", base, {"platform": "youtube", "publish_at": "2001-01-01T10:00"})
    assert st == 400 and "15 minutes" in e["error"]
    st, e = d.json("POST", base, {"platform": "tiktok"})
    assert st == 400


def test_double_start_is_409_and_pack_works(env):
    d, jid, gate = env["d"], env["jid"], env["gate"]
    _connect_youtube()
    gate.clear()  # the mock server hangs until released
    base = f"/api/jobs/{jid}/clips/clip_02/publish"
    assert d.json("POST", base, {"platform": "youtube"})[0] == 202
    st, e = d.json("POST", base, {"platform": "youtube"})
    assert st == 409 and "already being published" in e["error"]
    clips = d.json("GET", f"/api/jobs/{jid}/clips")[1]["clips"]
    assert clips[1]["publish"]["youtube"]["state"] == "uploading"
    gate.set()
    assert _wait(d, jid, "clip_02", "youtube")["state"] == "uploaded"
    assert d.json("POST", base, {"platform": "pack", "description": "Watch this"})[0] == 202
    entry = _wait(d, jid, "clip_02", "pack")
    folder = Path(entry["folder"])
    assert entry["state"] == "packed" and folder.parent.name == "publish_pack"
    assert sorted(p.name for p in folder.iterdir()) == ["best_time.txt", "caption.txt", "clip_02.mp4", "hashtags.txt"]


def test_schedule_week_preview_then_start(env):
    d, jid, t = env["d"], env["jid"], env["t"]
    url = f"/api/jobs/{jid}/publish/schedule"
    body = {"platform": "youtube", "start": "2099-03-02", "per_day": 1, "times": "18:00"}
    st, r = d.json("POST", url, body)
    assert st == 200 and not r["started"] and [s["key"] for s in r["slots"]] == ["clip_01", "clip_02"]
    assert r["slots"][0]["at"].startswith("2099-03-02T18:00") and r["slots"][1]["label"].startswith("Tue 03 Mar")
    assert t.calls == []
    assert d.json("POST", url, {**body, "confirm": True})[0] == 400  # not connected yet
    _connect_youtube()
    st, r = d.json("POST", url, {**body, "confirm": True})
    assert st == 202 and r["started"]
    e1, e2 = _wait(d, jid, "clip_01", "youtube"), _wait(d, jid, "clip_02", "youtube")
    assert e1["state"] == "uploaded" or e1["state"] == "scheduled"
    starts = [c["body"]["status"] for c in t.calls if c["method"] == "POST"]
    assert [s["privacyStatus"] for s in starts] == ["private", "private"]
    assert all(s["publishAt"].startswith("2099-03-0") for s in starts)
    assert e2["state"] in ("uploaded", "scheduled")
    st, r = d.json("POST", url, body)  # both clips are done: nothing left to plan
    assert st == 200 and r["slots"] == []
    assert d.json("POST", url, {**body, "platform": "instagram"})[0] == 400


def test_settings_connect_disconnect_and_no_token_in_responses(env, monkeypatch):
    d, tmp = env["d"], env["tmp"]
    st, s = d.json("GET", "/api/publish")
    assert st == 200 and not s["youtube"]["connected"] and not s["instagram"]["connected"]
    client = tmp / "client_secret.json"
    client.write_text(json.dumps({"installed": {"client_id": CID, "client_secret": "GOCSPX-secret"}}), encoding="utf-8")
    done = threading.Event()

    def fake_authorize(c, transport, **kw):
        _connect_youtube()
        done.set()

    monkeypatch.setattr(yt, "authorize", fake_authorize)
    st, s = d.json("POST", "/api/publish/youtube/connect", {"client_file": str(client)})
    assert st == 202 and done.wait(5)
    time.sleep(0.1)
    s = d.json("GET", "/api/publish")[1]
    assert s["youtube"] == {
        "client_file": str(client),
        "connected": True,
        "account": "me@example.com",
        "connecting": False,
        "error": "",
    }
    assert d.json("POST", "/api/publish/youtube/connect", {"client_file": str(tmp / "nope.json")})[0] == 400
    igf = tmp / "ig.json"
    igf.write_text(json.dumps({"ig_user_id": "17841400000000000", "access_token": "EAAG" + "z" * 40}), encoding="utf-8")
    st, s = d.json("POST", "/api/publish/instagram/connect", {"credentials_file": str(igf)})
    assert st == 200 and s["instagram"]["connected"] and s["instagram"]["account"] == "my.channel"
    for path in ("/api/publish", f"/api/jobs/{env['jid']}/clips", "/api/settings"):
        _st, _h, raw = d.req("GET", path)
        for secret in (ACCESS, REFRESH, "GOCSPX-secret", "EAAG" + "z" * 40):
            assert secret.encode() not in raw, (path, secret)
    tok_file = env["cfg"] / "tokens" / "youtube.json"
    assert tok_file.is_file() and not list((d.srv.ws).rglob("youtube.json"))
    assert d.json("POST", "/api/publish/youtube/disconnect", {})[1]["youtube"]["connected"] is False
    assert not tok_file.exists()
    assert d.json("POST", "/api/publish/instagram/disconnect", {})[1]["instagram"]["connected"] is False
    assert igf.is_file()  # the user's own file is never deleted
    assert d.json("POST", "/api/publish/youtube/hack", {})[0] == 404


def test_lan_mode_refuses_publishing_and_csrf_is_required(env, tmp_path, monkeypatch):
    d, jid = env["d"], env["jid"]
    base = f"/api/jobs/{jid}/clips/clip_01/publish"
    assert d.req("POST", base, {"platform": "pack"}, headers={"X-CSRF-Token": "wrong"})[0] == 403
    assert d.req("POST", "/api/publish/youtube/disconnect", {}, headers={"X-CSRF-Token": ""})[0] == 403
    monkeypatch.setattr(server, "lan_ip", lambda: "127.0.0.1")  # its UDP probe would trip the network guard
    lan = start(tmp_path / "lan", monkeypatch, lan=True, ws=d.srv.ws)
    try:
        for method, path in (
            ("GET", "/api/publish"),
            ("POST", "/api/publish/youtube/connect"),
            ("GET", base),
            ("POST", base),
            ("POST", f"/api/jobs/{jid}/publish/schedule"),
        ):
            st, e = lan.json(method, path, {} if method == "POST" else None)
            assert st == 403 and e["error"] == "Publishing works only on this PC.", (method, path)
    finally:
        stop(lan)
