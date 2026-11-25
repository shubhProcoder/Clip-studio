import json
import os
import socket
import sys
from datetime import date, datetime, time, timedelta, timezone

import pytest

from publish_util import no_net  # noqa: F401
from streamclipper.publish import http, meta, schedule, store
from streamclipper.publish.store import PublishError

pytestmark = pytest.mark.usefixtures("no_net")


def test_guard_blocks_the_network_and_the_real_transport():
    with pytest.raises(AssertionError, match="network access"):
        socket.create_connection(("www.googleapis.com", 443), timeout=1)
    with pytest.raises(AssertionError, match="UrllibTransport"):
        http.make_transport()


def test_redaction_of_every_secret_kind():
    store.remember_secret("ya29.SECRETACCESSTOKEN")
    text = (
        "Authorization: Bearer ya29.abc-DEF_123 / OAuth EAAB1234 refresh_token=1//xyz&client_secret=GOCSPX-q "
        'code=4/0Ab {"access_token": "tok-1", "client_secret":"s3"} ya29.SECRETACCESSTOKEN'
    )
    out = store.redact(text)
    for secret in ("ya29.abc-DEF_123", "EAAB1234", "1//xyz", "GOCSPX-q", "4/0Ab", "tok-1", '"s3"', "SECRETACCESS"):
        assert secret not in out, secret
    assert out.count("<redacted>") >= 8
    d = store.redact({"Authorization": "Bearer x", "nested": [{"refresh_token": "r", "code": 190}], "title": "ok"})
    assert d == {"Authorization": "<redacted>", "nested": [{"refresh_token": "<redacted>", "code": 190}], "title": "ok"}
    e = PublishError("failed for access_token=abc123", "hint Bearer zzz")
    assert "abc123" not in str(e) and "zzz" not in e.hint


def test_token_file_lives_in_the_config_dir(tmp_path):
    p = store.save_token("youtube", {"access_token": "AT-123456789", "refresh_token": "RT-123456789"})
    assert p == tmp_path / "config" / "tokens" / "youtube.json"
    assert store.load_token("youtube")["refresh_token"] == "RT-123456789"
    assert "RT-123456789" not in store.redact("x RT-123456789 y")
    if sys.platform != "win32":
        assert (os.stat(p).st_mode & 0o777) == 0o600
        assert (os.stat(p.parent).st_mode & 0o777) == 0o700
    assert store.delete_token("youtube") and store.load_token("youtube") is None
    st = store.save_settings(youtube_client="C:/x/client.json", instagram_account="")
    assert st == {"youtube_client": "C:/x/client.json"} and store.load_settings() == st


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_token_file_mode_0600():
    p = store.save_token("instagram", {"access_token": "x" * 30})
    assert (os.stat(p).st_mode & 0o777) == 0o600


def test_metadata_limits_and_shorts_tag():
    clip = {
        "title": "Title <b>1</b> " + "word " * 40,
        "description": "Desc <i>x</i>",
        "hashtags": ["#gaming", "clips", "#gaming"],
    }
    base = meta.clip_meta(clip)
    assert base["hashtags"] == ["#gaming", "#clips"] and "<" not in base["title"]
    y = meta.youtube_meta(base)
    assert len(y["title"]) <= 100 and y["title"].endswith("...")
    assert y["description"].endswith("#Shorts #gaming #clips") and "Desc x" in y["description"]
    assert y["tags"] == ["Shorts", "gaming", "clips"]
    long = meta.youtube_meta({"title": "t", "description": "é" * 6000, "hashtags": ["#a"] * 1})
    assert len(long["description"].encode("utf-8")) <= meta.YT_DESCRIPTION_MAX and "#Shorts" in long["description"]
    many = meta.youtube_meta({"title": "t", "description": "", "hashtags": [f"#tag{i:03d}xxxxxxxx" for i in range(60)]})
    assert len(",".join(many["tags"])) <= meta.YT_TAGS_MAX
    cap = meta.instagram_caption({"title": "T", "description": "d" * 3000, "hashtags": [f"#h{i}" for i in range(40)]})
    assert len(cap) <= meta.IG_CAPTION_MAX and cap.count("#") == 30 and cap.startswith("T")
    assert meta.clip_meta({}, title="Mine")["title"] == "Mine" and meta.clip_meta({})["title"] == "Stream clip"


def test_schedule_week_across_days_and_time_zones():
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 11, 20, 19, 0, tzinfo=ist)  # a Friday evening
    plan = schedule.schedule_week(list(range(10)), date(2026, 11, 20), per_day=1, tz=ist, now=now)
    ats = [s["at"] for s in plan["slots"]]
    assert ats[0] == datetime(2026, 11, 21, 11, 0, tzinfo=ist)  # Friday 18:00 is past -> Saturday 11:00 (weekend)
    assert ats[2] == datetime(2026, 11, 23, 18, 0, tzinfo=ist)  # Monday evening
    assert len(ats) == 6 and plan["left"] == [6, 7, 8, 9]  # 7 days from the start day, the first slot skipped
    assert schedule.rfc3339(ats[0]) == "2026-11-21T05:30:00Z"
    two = schedule.schedule_week(
        ["a", "b", "c"], date(2026, 11, 23), per_day=2, times="20:30, 12:00", tz=timezone.utc, now=now
    )
    assert [s["at"].time() for s in two["slots"]] == [time(12, 0), time(20, 30), time(12, 0)]
    with pytest.raises(PublishError, match="posting times"):
        schedule.schedule_week(["a"], date(2026, 11, 23), per_day=3, times="18:00", now=now)
    with pytest.raises(PublishError, match="Unknown time"):
        schedule.parse_times("6pm")
    assert schedule.parse_start("2026-11-30") == date(2026, 11, 30)
    assert schedule.parse_when("2026-11-21T18:00Z") == datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
    with pytest.raises(PublishError, match="15 minutes"):
        schedule.check_future(now + timedelta(minutes=5), now)
    txt = schedule.best_time_text(now)
    assert schedule.HEURISTIC in txt and "Next good slot: Fri 20 Nov 19:30" in txt


def test_mock_transport_records_redacted_bodies():
    t = http.MockTransport(responses=[(200, {"X-A": "1"}, {"ok": True})])
    st, h, b = t.request(
        "POST",
        "https://x.test/a?access_token=SECRETSECRET",
        {"Content-Type": "application/json", "Authorization": "Bearer abc"},
        json.dumps({"access_token": "q", "title": "t"}).encode(),
    )
    assert st == 200 and h == {"x-a": "1"} and json.loads(b) == {"ok": True}
    call = t.calls[0]
    assert call["headers"]["Authorization"] == "<redacted>" and "SECRETSECRET" not in call["url"]
    assert call["body"] == {"access_token": "<redacted>", "title": "t"}
    t2 = http.MockTransport(responses=[(200, {}, b"")])
    t2.request("PUT", "https://x.test/u", {"Content-Type": "video/mp4"}, b"\x00" * 10)
    assert t2.calls[0]["body"]["bytes"] == 10
