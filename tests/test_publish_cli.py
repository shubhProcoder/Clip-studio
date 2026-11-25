import json
import time

import pytest

from publish_util import no_net  # noqa: F401
from streamclipper.cli import COMMANDS, build_parser
from streamclipper.publish import http, store
from streamclipper.publish.http import MockTransport
from streamclipper.publish.store import PublishError

pytestmark = pytest.mark.usefixtures("no_net")


def run(*argv):
    args = build_parser().parse_args(list(argv))
    COMMANDS[args.cmd](args)


@pytest.fixture
def clips(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    doc = {"clips": []}
    for r in (1, 2, 3):
        (out / f"clip_{r:02d}.mp4").write_bytes(b"\x00" * (1000 + r))
        doc["clips"].append(
            {
                "rank": r,
                "title": f"Clip {r} <b>wow</b>",
                "description": f"Desc {r}",
                "hashtags": ["#gta", "#funny"],
                "mp4": f"clip_{r:02d}.mp4",
            }
        )
    doc["clips"].append({"rank": 4, "title": "no mp4"})
    p = out / "clips.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


@pytest.fixture
def recorder(monkeypatch):
    t = MockTransport(responses=[])
    monkeypatch.setattr(http, "make_transport", lambda: t)
    return t


def test_dry_runs_print_metadata_and_send_nothing(clips, recorder, capsys):
    run("publish", "youtube", "--clips", str(clips), "--rank", "2", "--dry-run", "--publish-at", "2099-01-02T18:00")
    out = capsys.readouterr().out
    doc = json.loads(out[: out.rindex("}") + 1])
    assert doc["request"]["snippet"]["title"] == "Clip 2 wow" and "#Shorts" in doc["request"]["snippet"]["description"]
    assert doc["request"]["status"]["privacyStatus"] == "private" and doc["request"]["status"]["publishAt"].startswith(
        "2099-01-02T"
    )
    assert "nothing was sent" in out
    run("publish", "instagram", "--clips", str(clips), "--dry-run")
    out = capsys.readouterr().out
    assert '"media_type": "REELS"' in out and "#gta #funny" in out
    run("publish", "schedule", "--clips", str(clips), "--start", "2099-01-05", "--dry-run")
    plan = json.loads((clips.parent / "publish_schedule.json").read_text(encoding="utf-8"))
    assert [s["rank"] for s in plan["slots"]] == [1, 2, 3] and plan["dry_run"] is True
    assert [s["at"][:16] for s in plan["slots"]] == ["2099-01-05T18:00", "2099-01-06T18:00", "2099-01-07T18:00"]
    assert recorder.calls == [] and not (store.config_dir() / "tokens").exists()


def test_pack_folder_contents(clips, tmp_path):
    run("publish", "pack", "--clips", str(clips), "--out", str(tmp_path / "packs"))
    folders = sorted((tmp_path / "packs" / "publish_pack").iterdir())
    assert [f.name for f in folders] == ["clip_01", "clip_02", "clip_03"]
    f = folders[0]
    assert sorted(x.name for x in f.iterdir()) == ["best_time.txt", "caption.txt", "clip_01.mp4", "hashtags.txt"]
    assert (f / "clip_01.mp4").read_bytes() == (clips.parent / "clip_01.mp4").read_bytes()
    assert (f / "caption.txt").read_text(encoding="utf-8").startswith("Clip 1 wow\n\nDesc 1\n\n#gta #funny")
    assert (f / "hashtags.txt").read_text(encoding="utf-8").strip() == "#gta #funny"
    assert "not measured" in (f / "best_time.txt").read_text(encoding="utf-8")


def test_schedule_uploads_with_publish_at(clips, monkeypatch, capsys):
    store.save_token(
        "youtube",
        {"client_id": "c.apps.googleusercontent.com", "client_secret": "s" * 10, "access_token": "ya29.A",
         "refresh_token": "1//R", "expires_at": time.time() + 3600, "account": "me@example.com"},
    )  # fmt: skip
    n = {"v": 0}

    def yt(method, url, headers, body):
        if method == "POST":
            return 200, {"Location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=1"}, b""
        n["v"] += 1
        return 200, {}, {"id": f"V{n['v']}", "status": {"privacyStatus": "private", "publishAt": "x"}}

    t = MockTransport(yt)
    monkeypatch.setattr(http, "make_transport", lambda: t)
    run("publish", "schedule", "--clips", str(clips), "--start", "2099-03-02", "--times", "12:00", "--per-day", "1")
    plan = json.loads((clips.parent / "publish_schedule.json").read_text(encoding="utf-8"))
    assert [s["state"] for s in plan["slots"]] == ["scheduled"] * 3
    starts = [c for c in t.calls if c["method"] == "POST"]
    assert [c["body"]["status"]["privacyStatus"] for c in starts] == ["private"] * 3
    assert all(c["body"]["status"]["publishAt"].endswith("Z") for c in starts)
    assert "Scheduled on YouTube" in capsys.readouterr().out


def test_errors_are_clear(clips, recorder):
    with pytest.raises(PublishError, match="not connected"):
        run("publish", "youtube", "--clips", str(clips))
    with pytest.raises(PublishError, match="no MP4"):
        run("publish", "youtube", "--clips", str(clips), "--rank", "4", "--dry-run")
    with pytest.raises(PublishError, match="no scheduled posts"):
        run("publish", "schedule", "--clips", str(clips), "--platform", "instagram", "--dry-run")
    with pytest.raises(PublishError, match="credentials"):
        run("publish", "instagram", "--clips", str(clips))
    assert recorder.calls == []
