import time

import pytest

from dashboard_util import start, stop
from streamclipper.dashboard import shares
from streamclipper.util import ToolError


@pytest.fixture
def lan(tmp_path, monkeypatch):
    d = start(tmp_path, monkeypatch, lan=True)
    yield d
    stop(d)


def test_share_needs_lan(tmp_path, monkeypatch):
    d = start(tmp_path, monkeypatch)
    try:
        a = d.new_job()
        d.wait(a)
        st, r = d.json("POST", "/api/shares", {"kind": "clip", "job": a, "clip": "clip_01"})
        assert st == 400 and "--lan" in r["error"]
    finally:
        stop(d)


def test_clip_and_job_shares(lan, monkeypatch):
    a = lan.new_job()
    lan.wait(a)
    st, r = lan.json("POST", "/api/shares", {"kind": "clip", "job": a, "clip": "clip_01", "days": 7})
    assert st == 201 and "/s/" in r["url"] and r["label"] == "Works for people on the same Wi-Fi/network"
    token = r["url"].rsplit("/s/", 1)[1]
    assert token != lan.access
    st, h, page = lan.req("GET", f"/s/{token}", cookie=None)
    assert st == 200 and b"Works for people on the same Wi-Fi/network" in page
    assert b"Title &lt;b&gt;1&lt;/b&gt;" in page and b"<b>1</b>" not in page and b"<script" not in page
    st, _h, mp4 = lan.req("GET", f"/s/{token}/f/clip_01.mp4?download=1", cookie=None)
    assert st == 200 and len(mp4) == 4096
    assert lan.req("GET", f"/s/{token}/f/clip_02.mp4", cookie=None)[0] == 404
    assert lan.req("GET", f"/s/{token}/f/..%2fclips.json", cookie=None)[0] == 404
    assert lan.req("GET", "/s/wrongtoken", cookie=None)[0] == 404
    assert lan.req("POST", f"/s/{token}", {}, cookie=None)[0] == 405
    # without the access cookie nothing else is reachable with a share token
    assert lan.req("GET", f"/api/jobs?token={token}", cookie=None)[0] == 401

    st, r2 = lan.json("POST", "/api/shares", {"kind": "job", "job": a})
    jt = r2["url"].rsplit("/s/", 1)[1]
    page = lan.req("GET", f"/s/{jt}", cookie=None)[2]
    assert page.count(b"<video") == 2
    lan.json("POST", f"/api/jobs/{a}/clips/clip_02/delete", {"confirm": True})
    page = lan.req("GET", f"/s/{jt}", cookie=None)[2]
    assert page.count(b"<video") == 1

    listed = lan.json("GET", "/api/shares")[1]["shares"]
    assert all("token_sha256" not in s for s in listed)
    assert lan.json("POST", f"/api/shares/{r['id']}/revoke")[0] == 200
    assert lan.req("GET", f"/s/{token}", cookie=None)[0] == 404

    real = time.time
    monkeypatch.setattr(shares.time, "time", lambda: real() + 8 * 86400)
    assert lan.req("GET", f"/s/{jt}", cookie=None)[0] == 404


def test_bad_days_falls_back_to_7_without_a_500(lan):
    a = lan.new_job()
    lan.wait(a)
    for bad in ("abc", None, [1], {"x": 1}):
        st, r = lan.json("POST", "/api/shares", {"kind": "job", "job": a, "days": bad})
        assert st == 201 and r["days"] == 7, (bad, r)
    st, r = lan.json("POST", "/api/shares", {"kind": "job", "job": a, "days": 3})
    assert st == 400 and "1, 7 or 30" in r["error"]


def test_share_store_expiry_and_revoke(tmp_path):
    s = shares.ShareStore(tmp_path)
    sid, tok, days = s.create("clip", "job1", "clip_01", days=7)
    assert days == 7 and s.resolve(tok)["clip"] == "clip_01"
    assert s.create("clip", "job1", "clip_01", days="30")[2] == 30
    assert s.resolve(tok, now=time.time() + 8 * 86400) is None
    assert tok not in (tmp_path / "shares.json").read_text(encoding="utf-8")
    s.revoke(sid)
    assert s.resolve(tok) is None
    with pytest.raises(ToolError):
        s.create("clip", "job1", "clip_01", days=99)
