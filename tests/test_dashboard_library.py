import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import dashboard_fake_cli as fake  # noqa: E402

from streamclipper.dashboard import jobs, library  # noqa: E402
from streamclipper.draft import importer, rootmeta  # noqa: E402
from streamclipper.util import ToolError  # noqa: E402


class FakeRunner:
    def __init__(self):
        self.submitted = []

    def submit(self, jid):
        self.submitted.append(jid)


@pytest.fixture
def job(tmp_path):
    store = jobs.JobStore(tmp_path / "ws")
    j = store.create("vod", "x", ["clip-stream"], {})
    store.update(j["id"], status="done")
    jdir = store.job_dir(j["id"])
    out = jdir / "out"
    out.mkdir()
    for r in (1, 2):
        fake.write_clip(out, f"clip_{r:02d}", r, str(jdir / "capcut_drafts"))
    (out / "clips.json").write_text(json.dumps(fake.clip_doc(2)), encoding="utf-8")
    (out / "my_video.mp4").write_bytes(b"user file")
    (out / "clip_01_notes.txt").write_text("user notes", encoding="utf-8")
    return store, j["id"], tmp_path / "ws"


def test_list_mark_and_delete_to_trash(job):
    store, jid, ws = job
    clips = library.list_clips(store, jid)
    assert [c["key"] for c in clips] == ["clip_01", "clip_02"]
    assert clips[0]["gate"]["pass"] and clips[0]["thumb_url"].endswith("clip_01_thumb.jpg")
    assert clips[0]["title"] == "Title <b>1</b>" and clips[0]["hashtags"] == ["#gaming", "#clips"]
    library.mark(store, jid, "clip_01", "posted")
    assert jobs.JobStore(ws).get(jid)["marks"] == {"clip_01": "posted"}
    with pytest.raises(ToolError):
        library.mark(store, jid, "clip_01", "hacked")
    with pytest.raises(ToolError, match="confirmation"):
        library.delete_clip(store, ws, jid, "clip_01")
    moved = library.delete_clip(store, ws, jid, "clip_01", confirm=True)
    out = store.job_dir(jid) / "out"
    assert sorted(moved) == sorted(
        ["clip_01.mp4", "clip_01.clip.json", "clip_01_thumb.jpg", "clip_01.gate.json", "clip_01"]
    )
    assert (
        (out / "my_video.mp4").is_file() and (out / "clip_01_notes.txt").is_file() and (out / "clip_02.mp4").is_file()
    )
    trash = list((ws / "trash" / jid).glob("*/clip_01.mp4"))
    assert len(trash) == 1 and trash[0].read_bytes()
    assert [c["key"] for c in library.list_clips(store, jid)] == ["clip_02"]
    with pytest.raises(ToolError):
        library.delete_clip(store, ws, jid, "../jobs", confirm=True)


def test_open_in_capcut_refuses_while_capcut_runs(job, monkeypatch):
    store, jid, _ws = job
    called = []
    monkeypatch.setattr(library._env, "capcut_running", lambda: True)
    monkeypatch.setattr(importer, "import_draft_zip", lambda *a, **k: called.append(a))
    with pytest.raises(library.CapCutOpen, match="Close CapCut first"):
        library.open_in_capcut(store, jid, "clip_01", "")
    assert called == []


def test_open_in_capcut_imports_the_portable_draft(job, monkeypatch, tmp_path):
    store, jid, _ws = job
    root = tmp_path / "capcut_root"
    root.mkdir()
    seen = []
    monkeypatch.setattr(library._env, "capcut_running", lambda: False)
    monkeypatch.setattr(importer, "guard_capcut", lambda dr, close=False: seen.append(close))
    monkeypatch.setattr(importer, "find_capcut_local", lambda: tmp_path / "CapCutLocal")
    monkeypatch.setattr(rootmeta, "_ROOT_META_BACKED_UP", set())
    real = importer.import_draft_zip
    kw_seen = {}

    def spy(*a, **k):
        kw_seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(importer, "import_draft_zip", spy)
    name = library.open_in_capcut(store, jid, "clip_01", str(root))
    assert name == "clip_01" and (root / "clip_01" / "draft_content.json").is_file()
    rm = json.loads((root / "root_meta_info.json").read_text(encoding="utf-8"))
    assert rm["all_draft_store"][0]["draft_name"] == "clip_01"
    assert kw_seen["close_capcut"] is False and seen == [False]
    assert library.open_in_capcut(store, jid, "clip_01", str(root)) == "clip_01_2"
    assert store.get(jid)["imported"]["clip_01"] == "clip_01_2"


def test_rerender_queues_render_spec(job):
    store, jid, _ws = job
    runner = FakeRunner()
    v = library.rerender(store, runner, jid, "clip_01", "comic-burst", "")
    assert v["name"] == "clip_01_2"
    rj = store.get(runner.submitted[0])
    assert rj["argv"][:3] == ["render", "--spec", str(store.job_dir(jid) / "out" / "clip_01.clip.json")]
    assert rj["argv"][3:] == ["--caption-style", "comic-burst"]
    assert library.rerender(store, runner, jid, "clip_01", "", "top-bar")["name"] == "clip_01_3"
    with pytest.raises(ToolError):
        library.rerender(store, runner, jid, "clip_01", "nope", "")
