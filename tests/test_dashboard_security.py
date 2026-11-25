import json

import pytest

from dashboard_util import start, stop
from streamclipper.cli import build_parser
from streamclipper.dashboard import server
from streamclipper.util import ToolError


@pytest.fixture
def dash(tmp_path, monkeypatch):
    d = start(tmp_path, monkeypatch)
    yield d
    stop(d)


@pytest.fixture
def lan(tmp_path, monkeypatch):
    d = start(tmp_path, monkeypatch, lan=True)
    yield d
    stop(d)


def test_default_bind_is_loopback_and_wide_host_needs_lan(tmp_path):
    args = build_parser().parse_args(["dashboard"])
    assert args.host == "127.0.0.1" and args.port == 8765 and not args.lan
    srv = server.make_server(tmp_path / "ws", "127.0.0.1", 0)
    try:
        assert srv.server_address[0] == "127.0.0.1" and srv.access == ""
    finally:
        srv.close_dashboard(1)
    with pytest.raises(ToolError, match="would open the dashboard"):
        server.make_server(tmp_path / "ws", "0.0.0.0", 0)


def test_port_in_use_is_a_clear_error(dash, tmp_path):
    with pytest.raises(ToolError, match=f"Port {dash.port} is in use"):
        server.make_server(tmp_path / "ws2", "127.0.0.1", dash.port)


def test_lan_needs_the_access_token(lan):
    assert lan.req("GET", "/api/jobs", cookie=None)[0] == 401
    assert lan.req("GET", "/?token=wrong", cookie=None)[0] == 401
    assert lan.req("GET", "/api/jobs", cookie="sc_access=wrong")[0] == 401
    st, h, _ = lan.req("GET", f"/?token={lan.access}", cookie=None)
    assert st == 302 and h["Location"] == "/"
    ck = h["Set-Cookie"]
    assert ck.startswith(f"sc_access={lan.access}") and "HttpOnly" in ck and "SameSite=Strict" in ck
    assert lan.req("GET", "/api/jobs")[0] == 200
    assert server.make_server  # LAN servers bind 0.0.0.0 unless told otherwise
    assert server.LAN_WARNING.startswith("WARNING: --lan")


def test_host_header_and_csrf_and_origin(dash):
    assert dash.req("GET", "/api/jobs", headers={"Host": f"evil.example:{dash.port}"})[0] == 403
    body = {"source": "https://example.com/v"}
    assert dash.req("POST", "/api/jobs", body, headers={"X-CSRF-Token": ""})[0] == 403
    assert dash.req("POST", "/api/jobs", body, headers={"X-CSRF-Token": "wrong"})[0] == 403
    assert dash.req("POST", "/api/jobs", body, headers={"Origin": "http://evil.example"})[0] == 403
    st, _h, _b = dash.req("POST", "/api/jobs", body, headers={"Origin": f"http://127.0.0.1:{dash.port}"})
    assert st == 201


def test_path_traversal_and_file_allowlist(dash, tmp_path):
    a = dash.new_job()
    dash.wait(a)
    ws = tmp_path / "ws"
    (ws / "settings.json").write_text('{"secret": "SETTINGS-CONTENT"}', encoding="utf-8")
    (ws / "jobs" / a / "out" / "x.exe").write_bytes(b"MZ")
    jobs_json = (ws / "jobs.json").read_bytes()
    bad = [
        f"/files/{a}/../jobs.json",
        f"/files/{a}/..%2f..%2fjobs.json",
        f"/files/{a}/%2e%2e/%2e%2e/settings.json",
        f"/files/{a}/C:%5CWindows%5Cwin.ini",
        f"/files/{a}/out%5C..%5C..%5Cjobs.json",
        f"/files/..%2f{a}/x",
        f"/files/{a}/out/..%2f..%2f..%2fsettings.json",
        f"/files/{a}/out/%00clip_01.mp4",
        "/static/../server.py",
        "/static/server.py",
        "/static/%2e%2e%2fserver.py",
        "/C:/Windows/win.ini",
        f"/files/{a}/job.json",
        f"/files/{a}/out/x.exe",
    ]
    for path in bad:
        st, _h, body = dash.req("GET", path)
        assert st in (403, 404), path
        assert b"SETTINGS-CONTENT" not in body and body != jobs_json and b"import " not in body, path
    assert dash.req("GET", f"/files/{a}/log.txt")[0] == 200
    assert dash.req("GET", f"/files/{a}/out/clips.json")[0] == 200


def test_upload_rules(dash, tmp_path, monkeypatch):
    st, _ = dash.json("PUT", "/api/upload?name=evil.exe", raw=b"MZ")
    assert st == 415
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=5)
    conn.putrequest("PUT", "/api/upload?name=a.mp4")
    conn.putheader("X-CSRF-Token", dash.csrf)
    conn.endheaders()
    assert conn.getresponse().status == 411
    conn.close()
    writes = []
    real = server._write_chunk
    monkeypatch.setattr(server, "UPLOAD_CHUNK", 64 * 1024)
    monkeypatch.setattr(server, "_write_chunk", lambda f, d: (writes.append(len(d)), real(f, d)))
    data = bytes(range(256)) * (3 * 4096)  # 3 MiB
    st, r = dash.json("PUT", "/api/upload?name=..%2F..%2Fevil.mp4", raw=data)
    assert st == 201 and r["upload"] == "evil.mp4"
    assert len(writes) >= 40 and max(writes) <= 64 * 1024
    up = tmp_path / "ws" / "uploads" / "evil.mp4"
    assert up.read_bytes() == data and not (tmp_path / "evil.mp4").exists()
    # an uploaded file can be the source of a job
    st, j = dash.json("POST", "/api/jobs", {"upload": "evil.mp4"})
    assert st == 201


def test_lan_local_paths_only_from_uploads(lan, tmp_path):
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"x")
    st, r = lan.json("POST", "/api/jobs", {"source": str(outside)})
    assert st == 400 and "uploaded" in r["error"]


def _point_source_outside(d, job, tmp_path, window_outside=False):
    p = d.srv.store.job_dir(job) / "out" / "clip_01.clip.json"
    plan = json.loads(p.read_text(encoding="utf-8"))
    private = tmp_path / "private"
    private.mkdir(exist_ok=True)
    (private / "stream.mp4").write_bytes(b"\x01" * 5000)
    plan["source"] = str(private / "stream.mp4")
    if window_outside:
        (private / "win.mp4").write_bytes(b"\x02" * 1000)
        plan["source_window"]["path"] = str(private / "win.mp4")
    p.write_text(json.dumps(plan), encoding="utf-8")


def test_lan_editor_source_stays_inside_the_workspace(lan, tmp_path):
    a = lan.new_job()
    lan.wait(a)
    url = f"/api/jobs/{a}/clips/clip_01/source"
    _point_source_outside(lan, a, tmp_path)
    st, _h, body = lan.req("GET", url)
    assert st == 200 and len(body) == 2048  # the clip's window file, not the stream outside the workspace
    st, c = lan.json("GET", f"/api/jobs/{a}/clips/clip_01/edit")
    assert st == 200 and c["original_source"] is False
    _point_source_outside(lan, a, tmp_path, window_outside=True)
    st, r = lan.json("GET", url)
    assert st == 404 and "outside the workspace" in r["error"]


def test_local_editor_source_may_be_anywhere(dash, tmp_path):
    a = dash.new_job()
    dash.wait(a)
    _point_source_outside(dash, a, tmp_path)
    st, _h, body = dash.req("GET", f"/api/jobs/{a}/clips/clip_01/source")
    assert st == 200 and len(body) == 5000


def test_no_secret_in_responses_or_logs(dash, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test-SECRET")
    a = dash.new_job(llm="gemini")
    dash.wait(a)
    log = (dash.srv.ws / "jobs" / a / "log.txt").read_text(encoding="utf-8")
    assert "sk-test-SECRET" not in log
    for path in (
        "/api/settings",
        "/api/options",
        "/api/jobs",
        f"/api/jobs/{a}",
        f"/files/{a}/log.txt",
        f"/api/jobs/{a}/clips",
    ):
        st, _h, body = dash.req("GET", path)
        assert st == 200 and b"sk-test-SECRET" not in body, path
    assert dash.json("GET", "/api/settings")[1]["gemini"] == "set"
