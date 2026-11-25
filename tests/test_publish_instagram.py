import json

import pytest

from publish_util import no_net  # noqa: F401
from streamclipper.publish import instagram as ig
from streamclipper.publish.http import MockTransport
from streamclipper.publish.store import PublishError

pytestmark = pytest.mark.usefixtures("no_net")

TOKEN = "EAAG" + "x" * 40
UID = "17841400000000000"
URI = f"https://rupload.facebook.com/ig-api-upload/v23.0/{'9' * 17}"


def _creds(tmp_path, **kw):
    p = tmp_path / "ig.json"
    p.write_text(
        json.dumps({"ig_user_id": UID, "access_token": TOKEN, "graph_version": "v23.0", **kw}), encoding="utf-8"
    )
    return ig.load_credentials(p)


def _mp4(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 5000)
    return p


def _graph(statuses=("IN_PROGRESS", "IN_PROGRESS", "FINISHED"), fail=None):
    seq = list(statuses)

    def handler(method, url, headers, body):
        if fail and fail[0](method, url):
            return fail[1]
        if method == "POST" and url.endswith(f"/{UID}/media"):
            return 200, {}, {"id": "9" * 17, "uri": URI}
        if url.startswith(ig.RUPLOAD_PREFIX):
            return 200, {}, {"success": True, "message": "Upload successful."}
        if method == "GET" and "fields=status_code" in url:
            return 200, {}, {"status_code": seq.pop(0), "id": "9" * 17}
        if url.endswith("/media_publish"):
            return 200, {}, {"id": "18000000000000001"}
        raise AssertionError(url)

    return handler


def test_reel_flow_order_bodies_and_poll(tmp_path):
    creds = _creds(tmp_path)
    t = MockTransport(_graph())
    slept = []
    res = ig.upload(_mp4(tmp_path), "No way he did that\n\n#gta #gaming", creds, t, sleep=slept.append)
    assert res == {"platform": "instagram", "media_id": "18000000000000001", "container_id": "9" * 17}
    steps = [(c["method"], c["url"].split("?")[0].rsplit("/", 1)[-1]) for c in t.calls]
    assert steps == [
        ("POST", "media"),
        ("POST", "9" * 17),
        ("GET", "9" * 17),
        ("GET", "9" * 17),
        ("GET", "9" * 17),
        ("POST", "media_publish"),
    ]
    assert t.calls[0]["url"] == f"https://graph.facebook.com/v23.0/{UID}/media"
    assert t.calls[0]["body"] == {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": "No way he did that\n\n#gta #gaming",
    }
    up = t.calls[1]
    assert up["url"] == URI and up["headers"]["Authorization"] == "<redacted>" and up["headers"]["offset"] == "0"
    assert up["headers"]["file_size"] == str(_mp4(tmp_path).stat().st_size) and up["body"]["bytes"] == 5012
    assert t.calls[-1]["body"] == {"creation_id": "9" * 17}
    assert slept == [ig.POLL_STEP_S, ig.POLL_STEP_S]
    assert TOKEN not in json.dumps(t.calls)


def test_poll_gives_up_after_five_minutes(tmp_path):
    clock = iter(range(0, 10000, 60)).__next__
    t = MockTransport(_graph(statuses=["IN_PROGRESS"] * 50))
    with pytest.raises(PublishError, match="after 5 minutes"):
        ig.upload(_mp4(tmp_path), "c", _creds(tmp_path), t, sleep=lambda s: None, clock=clock)
    with pytest.raises(PublishError, match="could not process"):
        ig.upload(
            _mp4(tmp_path), "c", _creds(tmp_path), MockTransport(_graph(statuses=["ERROR"])), sleep=lambda s: None
        )


@pytest.mark.parametrize(
    "error,match",
    [
        ({"code": 10, "message": "Application does not have permission for this action"}, "instagram_content_publish"),
        (
            {"code": 100, "message": "The user is not an Instagram Business account", "error_subcode": 2207013},
            "professional",
        ),
        ({"code": 190, "message": "Error validating access token"}, "expired"),
        ({"code": 9, "error_subcode": 2207042, "message": "limit"}, "limit"),
    ],
)
def test_clear_errors(tmp_path, error, match):
    fail = (lambda m, u: m == "POST" and u.endswith("/media"), (400, {}, {"error": error}))
    with pytest.raises(PublishError, match=match) as e:
        ig.upload(_mp4(tmp_path), "c", _creds(tmp_path), MockTransport(_graph(fail=fail)), sleep=lambda s: None)
    assert TOKEN not in str(e.value) + e.value.hint


def test_upload_uri_must_be_meta_and_credentials_are_checked(tmp_path):
    def evil(method, url, headers, body):
        return 200, {}, {"id": "1", "uri": "https://evil.example/upload"}

    t = MockTransport(evil)
    with pytest.raises(PublishError, match="no upload address"):
        ig.upload(_mp4(tmp_path), "c", _creds(tmp_path), t)
    assert len(t.calls) == 1  # the token never went to the other host
    with pytest.raises(PublishError, match="numeric"):
        _creds(tmp_path, ig_user_id="me")
    with pytest.raises(PublishError, match="graph_version"):
        _creds(tmp_path, graph_version="latest")
    t2 = MockTransport(responses=[(200, {}, {"username": "my.channel", "id": UID})])
    assert ig.account_name(_creds(tmp_path), t2) == "my.channel"
    assert t2.calls[0]["url"] == f"https://graph.facebook.com/v23.0/{UID}?fields=username"
