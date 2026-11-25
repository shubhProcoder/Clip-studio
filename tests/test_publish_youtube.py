import base64
import http.client
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from publish_util import no_net  # noqa: F401
from streamclipper.publish import store
from streamclipper.publish import youtube as yt
from streamclipper.publish.http import MockTransport, TransportError
from streamclipper.publish.meta import clip_meta, youtube_meta
from streamclipper.publish.store import PublishError

pytestmark = pytest.mark.usefixtures("no_net")

CID = "1234-abc.apps.googleusercontent.com"
SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=XYZ"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _client_file(tmp_path, kind="installed"):
    p = tmp_path / "client_secret.json"
    p.write_text(json.dumps({kind: {"client_id": CID, "client_secret": "GOCSPX-client-secret"}}), encoding="utf-8")
    return p


def _id_token(email):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).rstrip(b"=").decode()
    return f"h.{payload}.s"


def _token(expires=3600.0):
    return {
        "client_id": CID,
        "client_secret": "GOCSPX-client-secret",
        "access_token": "ya29.ACCESS-OLD",
        "refresh_token": "1//REFRESH-TOKEN",
        "expires_at": time.time() + expires,
        "account": "me@example.com",
    }


def _mp4(tmp_path, size):
    p = tmp_path / "clip_01.mp4"
    p.write_bytes(bytes(range(256)) * (size // 256) + b"\x01" * (size % 256))
    return p


def _body(publish_at=None, privacy="private"):
    base = clip_meta({"title": "No way he did that", "description": "Clutch.", "hashtags": ["#gta", "#gaming"]})
    return yt.video_body(youtube_meta(base), privacy, publish_at, NOW)


class Server:
    """A fake YouTube upload server: keeps the received bytes, can fail on chosen requests."""

    def __init__(self, size, fail=()):
        self.size, self.got, self.n, self.fail = size, bytearray(), 0, dict(fail)

    def __call__(self, method, url, headers, body):
        self.n += 1
        if self.n in self.fail:
            f = self.fail[self.n]
            if isinstance(f, Exception):
                raise f
            return f
        if url.startswith(yt.TOKEN_URL):
            return 200, {}, {"access_token": "ya29.ACCESS-NEW", "expires_in": 3599}
        if method == "POST":
            return 200, {"Location": SESSION}, b""
        rng = headers.get("Content-Range", "")
        if rng.startswith("bytes */"):
            return (308, {"Range": f"bytes=0-{len(self.got) - 1}"} if self.got else {}, b"")
        start = int(rng.split(" ")[1].split("-")[0])
        assert start == len(self.got), (start, len(self.got))
        self.got += body
        if len(self.got) == self.size:
            return 200, {}, {"id": "VID123", "status": {"privacyStatus": "private"}}
        return 308, {"Range": f"bytes=0-{len(self.got) - 1}"}, b""


def test_upload_body_chunks_and_result(tmp_path):
    size = 2 * 262144 + 1000
    mp4 = _mp4(tmp_path, size)
    srv = Server(size)
    t = MockTransport(srv)
    res = yt.upload(mp4, _body(), t, _token(), sleep=lambda s: None, chunk=262144)
    assert res == {
        "platform": "youtube",
        "video_id": "VID123",
        "url": "https://www.youtube.com/shorts/VID123",
        "privacy": "private",
        "publish_at": "",
    }
    assert bytes(srv.got) == mp4.read_bytes()
    init = t.calls[0]
    assert init["method"] == "POST" and init["url"] == yt.UPLOAD_URL
    assert init["headers"]["Authorization"] == "<redacted>" and init["headers"]["X-Upload-Content-Length"] == str(size)
    snip, status = init["body"]["snippet"], init["body"]["status"]
    assert snip["title"] == "No way he did that" and snip["description"] == "Clutch.\n\n#Shorts #gta #gaming"
    assert snip["tags"] == ["Shorts", "gta", "gaming"]
    assert status == {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    ranges = [c["headers"]["Content-Range"] for c in t.calls[1:]]
    assert ranges == [f"bytes 0-262143/{size}", f"bytes 262144-524287/{size}", f"bytes 524288-{size - 1}/{size}"]
    assert all("ya29" not in json.dumps(c) for c in t.calls)


def test_scheduled_upload_is_private_with_publish_at():
    when = NOW + timedelta(days=1, hours=6)
    b = _body(when, privacy="unlisted")
    assert b["status"] == {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": False,
        "publishAt": "2026-11-21T18:00:00Z",
    }
    with pytest.raises(PublishError, match="15 minutes"):
        _body(NOW + timedelta(minutes=3))
    with pytest.raises(PublishError, match="Unknown privacy"):
        _body(privacy="public")


def test_retry_on_5xx_and_network_errors_resumes_at_the_server_offset(tmp_path):
    size = 3 * 262144
    mp4 = _mp4(tmp_path, size)
    # request 1 = POST start -> 503 (retried); 3 = first chunk -> network error; 6 = a chunk -> 500
    srv = Server(size, fail={1: (503, {}, b""), 3: TransportError("boom"), 6: (500, {}, b"")})
    slept = []
    res = yt.upload(mp4, _body(), MockTransport(srv), _token(), sleep=slept.append, chunk=262144)
    assert res["video_id"] == "VID123" and bytes(srv.got) == mp4.read_bytes()
    assert slept == [1, 1, 1]


def test_gives_up_after_max_retries(tmp_path):
    mp4 = _mp4(tmp_path, 1000)
    t = MockTransport(lambda *a: (503, {}, b""))
    with pytest.raises(PublishError, match="did not answer"):
        yt.upload(mp4, _body(), t, _token(), sleep=lambda s: None)
    assert len(t.calls) == yt.MAX_RETRIES + 1


def test_refresh_on_401_and_on_expiry(tmp_path):
    size = 1000
    mp4 = _mp4(tmp_path, size)
    srv = Server(size, fail={1: (401, {}, {"error": {"message": "Invalid Credentials"}})})
    t = MockTransport(srv)
    yt.upload(mp4, _body(), t, _token(), sleep=lambda s: None)
    assert [c["url"] for c in t.calls[:3]] == [yt.UPLOAD_URL, yt.TOKEN_URL, yt.UPLOAD_URL]
    refresh = t.calls[1]["body"]
    assert refresh == {
        "client_id": CID,
        "client_secret": "<redacted>",
        "refresh_token": "<redacted>",
        "grant_type": "refresh_token",
    }
    saved = store.load_token("youtube")
    assert saved["access_token"] == "ya29.ACCESS-NEW" and saved["refresh_token"] == "1//REFRESH-TOKEN"
    t2 = MockTransport(Server(size))
    yt.upload(mp4, _body(), t2, _token(expires=-10), sleep=lambda s: None)
    assert t2.calls[0]["url"] == yt.TOKEN_URL


def test_quota_and_revoked_token_errors(tmp_path):
    mp4 = _mp4(tmp_path, 1000)
    quota = {"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}}
    with pytest.raises(PublishError, match="quota") as e:
        yt.upload(mp4, _body(), MockTransport(responses=[(403, {}, quota)]), _token())
    assert "Pacific" in e.value.hint
    store.save_token("youtube", _token())
    t = MockTransport(responses=[(400, {}, {"error": "invalid_grant"})])
    with pytest.raises(PublishError, match="expired or was removed"):
        yt.upload(mp4, _body(), t, _token(expires=-1))
    assert store.load_token("youtube") is None


def test_client_file_checks(tmp_path):
    assert yt.load_client(_client_file(tmp_path))["client_id"] == CID
    with pytest.raises(PublishError, match="Desktop app"):
        yt.load_client(_client_file(tmp_path, kind="web"))
    with pytest.raises(PublishError, match="not found"):
        yt.load_client(tmp_path / "missing.json")


def test_loopback_sign_in_with_pkce_and_state(tmp_path):
    client = yt.load_client(_client_file(tmp_path))
    seen = {}

    def token_endpoint(method, url, headers, body):
        seen["form"] = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        return (
            200,
            {},
            {
                "access_token": "ya29.FIRST",
                "refresh_token": "1//R",
                "expires_in": 3599,
                "id_token": _id_token("me@example.com"),
            },
        )

    def browser(url):  # what Google does after the user clicks Allow: redirect to the loopback with code+state
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        seen["auth"] = q
        port = urlsplit(q["redirect_uri"]).port
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", f"/?state={q['state']}&code=4/CODE-123&scope=x")
        seen["page"] = c.getresponse().read()

    t = MockTransport(token_endpoint)
    tok = yt.authorize(client, t, open_browser=browser, timeout=10)
    a = seen["auth"]
    assert a["code_challenge_method"] == "S256" and a["redirect_uri"].startswith("http://127.0.0.1:")
    assert "youtube.upload" in a["scope"] and a["access_type"] == "offline"
    f = seen["form"]
    assert (
        f["grant_type"] == "authorization_code" and f["code"] == "4/CODE-123" and f["redirect_uri"] == a["redirect_uri"]
    )
    assert (
        base64.urlsafe_b64encode(__import__("hashlib").sha256(f["code_verifier"].encode()).digest())
        .rstrip(b"=")
        .decode()
        == a["code_challenge"]
    )
    assert b"connected" in seen["page"]
    assert tok["account"] == "me@example.com" and store.load_token("youtube")["refresh_token"] == "1//R"
    assert yt.connected() == {"connected": True, "account": "me@example.com"}
    assert t.calls[0]["body"]["code"] == "<redacted>" and t.calls[0]["body"]["code_verifier"] == "<redacted>"
    assert yt.disconnect() and not yt.connected()["connected"]


def test_sign_in_refuses_a_wrong_state_and_times_out(tmp_path):
    client = yt.load_client(_client_file(tmp_path))

    def bad_browser(url):
        port = urlsplit(parse_qs(urlsplit(url).query)["redirect_uri"][0]).port
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/?state=forged&code=4/EVIL")
        c.getresponse().read()

    t = MockTransport(responses=[])
    with pytest.raises(PublishError, match="state check"):
        yt.authorize(client, t, open_browser=bad_browser, timeout=10)
    with pytest.raises(PublishError, match="timed out"):
        yt.authorize(client, t, open_browser=lambda url: None, timeout=0.6)
    assert t.calls == []


def test_get_token_needs_a_client_the_first_time(tmp_path):
    with pytest.raises(PublishError, match="not connected"):
        yt.get_token(MockTransport(responses=[]))
    calls = []
    tok = yt.get_token(
        MockTransport(responses=[]), str(_client_file(tmp_path)), lambda c, t: calls.append(c) or _token()
    )
    assert calls and tok["client_id"] == CID
    store.save_token("youtube", _token())
    assert yt.get_token(MockTransport(responses=[]))["account"] == "me@example.com"
