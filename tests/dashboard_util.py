"""Start a real dashboard server on 127.0.0.1:<free port> with the fake CLI (tests/fixtures/dashboard_fake_cli.py)."""

import http.client
import json
import re
import sys
import threading
import time
from pathlib import Path

from streamclipper.dashboard import jobs, server

FAKE_CLI = Path(__file__).resolve().parent / "fixtures" / "dashboard_fake_cli.py"


class Dash:
    def __init__(self, srv):
        self.srv = srv
        self.port = srv.port
        self.access = srv.access
        self.csrf = srv.csrf
        self.cookie = f"sc_access={srv.access}" if srv.lan else None

    def req(self, method, path, body=None, headers=None, cookie="auto", raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        hdrs = dict(headers or {})
        if cookie == "auto":
            cookie = self.cookie
        if cookie:
            hdrs.setdefault("Cookie", cookie)
        if method != "GET":
            hdrs.setdefault("X-CSRF-Token", self.csrf)
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        out = r.read()
        conn.close()
        return r.status, dict(r.getheaders()), out

    def json(self, method, path, body=None, **kw):
        st, _h, out = self.req(method, path, body, **kw)
        return st, (json.loads(out) if out[:1] in (b"{", b"[") else out)

    def wait(self, job_id, statuses=("done",), timeout=20.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st, j = self.json("GET", f"/api/jobs/{job_id}")
            if st == 200 and j["status"] in statuses:
                return j
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not reach {statuses}: {j}")

    def new_job(self, **body):
        body.setdefault("source", "https://example.com/vod")
        st, j = self.json("POST", "/api/jobs", body)
        assert st == 201, j
        return j["id"]


def start(tmp_path, monkeypatch, lan=False, draft_root="", ws=None, sleep="0.05"):
    monkeypatch.setattr(jobs, "CLI_PREFIX", [sys.executable, str(FAKE_CLI)])
    monkeypatch.setattr(jobs, "POLL_S", 0.05)
    monkeypatch.setenv("SC_FAKE_SLEEP", sleep)
    monkeypatch.setattr(server, "ollama_available", lambda: False)
    ws = ws or (tmp_path / "ws")
    srv = server.make_server(ws, "127.0.0.1", 0, lan, draft_root, bind="127.0.0.1")
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return Dash(srv)


def stop(d: Dash):
    d.srv.shutdown()
    d.srv.close_dashboard(wait=10)


def csrf_from_index(html: bytes) -> str:
    return re.search(rb'<meta name="csrf" content="([^"]+)"', html).group(1).decode()
