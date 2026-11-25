import sys
import time

import pytest

from streamclipper.dashboard import jobs
from streamclipper.util import ToolError

FAKE = [sys.executable, str(__import__("pathlib").Path(__file__).parent / "fixtures" / "dashboard_fake_cli.py")]

LOG = """Source: https://youtu.be/x
Analysing audio of x.mp4 (10:00) ...
Transcribing 10:00 of audio in 2 chunk(s) (2 to do) ...
  ASR chunk 1/2 00:00-05:00: 100 words, 1.0s
""".splitlines()


def test_parse_progress_stages():
    p = jobs.parse_progress(LOG[:1])
    assert p["stage"] == "download"
    p = jobs.parse_progress(LOG[1:2], p)
    assert p["stage"] == "analysis"
    p = jobs.parse_progress(LOG[2:], p)
    assert (p["stage"], p["k"], p["n"]) == ("transcribe", 1, 2)
    p = jobs.parse_progress(["RANK START-END SCORE", "1    01:40-02:04 79", "2    03:20 78", "Analysis time: 1s"], p, 3)
    assert p["stage"] == "scoring" and p["rank_rows"] == 2
    p = jobs.parse_progress(["Rendering out/clip_01.mp4 (24s) ..."], p, 3)
    assert (p["stage"], p["k"], p["n"]) == ("render", 1, 2)
    p = jobs.parse_progress(["EXPORT GATE: clip_01.mp4", "Rendering out/clip_02.mp4 ..."], p, 3)
    assert (p["k"], p["n"], p["detail"]) == (2, 2, "clip 2/2")
    assert jobs.parse_progress(["RENDER SUMMARY"], p)["stage"] == "finishing"
    lv = jobs.parse_progress(["Live: buffered 01:00 | 3 clips ready", "CLIP 01 | x", "CLIP 02 | y"])
    assert lv["stage"] == "live" and lv["clips"] == 2
    assert jobs.parse_progress(["ERROR: boom"])["error"] == "boom"


def _opts(**kw):
    return jobs.validate_options({"render": True, "drafts": True, "max_clips": 2, **kw})


def test_argv_lists(tmp_path):
    src = tmp_path / "x'; rm -rf & echo" / "s.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")
    a = jobs.build_vod_argv(str(src), _opts(), tmp_path / "job")
    assert a[0] == "clip-stream" and a[a.index("--input") + 1] == str(src.resolve())
    assert "--portable-drafts" in a and a[a.index("--draft-root") + 1] == str(tmp_path / "job" / "capcut_drafts")
    assert "--fresh" not in a and a[a.index("--max-clips") + 1] == "2" and "--render" in a
    url = 'https://x.tv/v?a=1"; rm -rf / &'
    a = jobs.build_vod_argv(url, _opts(mode="podcast", drafts=False), tmp_path / "job")
    assert url in a and a[a.index("--preset") + 1] == "podcast-9x16" and "--no-draft" in a
    lv = jobs.build_live_argv(url, _opts(mode="live", stop_after=2, caption_style="comic-burst"), tmp_path / "j")
    assert lv[:3] == ["live", "--url", url] and lv[lv.index("--stop-after") + 1] == "2"
    assert lv[lv.index("--caption-style") + 1] == "comic-burst" and "--render" in lv
    r = jobs.build_render_argv(tmp_path / "clip_01.clip.json", "comic-burst", "top-bar")
    assert r == [
        "render",
        "--spec",
        str(tmp_path / "clip_01.clip.json"),
        "--caption-style",
        "comic-burst",
        "--hook-style",
        "top-bar",
    ]


def test_options_are_validated_and_clamped():
    o = jobs.validate_options({"min_score": 500, "max_clips": -3})
    assert o["min_score"] == 100 and o["max_clips"] == 0
    for bad in (
        {"caption_style": "nope"},
        {"hook_style": "x"},
        {"preset": "../evil"},
        {"ratio": "4:3"},
        {"llm": "gpt"},
    ):
        with pytest.raises(ToolError):
            jobs.validate_options(bad)


def test_booleans_are_strict():
    for v, want in [(True, True), (False, False), (1, True), (0, False), ("true", True), ("FALSE", False)]:
        assert jobs.parse_bool(v, "render") is want
    for v, want in [("1", True), ("0", False), ("Yes", True), (" no ", False), (None, True)]:
        assert jobs.parse_bool(v, "render", True) is want
    o = jobs.validate_options({"render": "false", "drafts": "yes"})
    assert o["render"] is False and o["drafts"] is True
    assert jobs.validate_options({})["render"] is True and jobs.validate_options({})["drafts"] is False
    for bad in ("maybe", "", 2, -1, 0.5, [], {}, "truee"):
        with pytest.raises(ToolError, match="render must be true or false"):
            jobs.validate_options({"render": bad})


def _runner(tmp_path, monkeypatch, sleep="0.05"):
    monkeypatch.setattr(jobs, "CLI_PREFIX", FAKE)
    monkeypatch.setattr(jobs, "POLL_S", 0.05)
    monkeypatch.setenv("SC_FAKE_SLEEP", sleep)
    store = jobs.JobStore(tmp_path / "ws")
    return store, jobs.Runner(store)


def _wait(store, jid, statuses, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if store.get(jid)["status"] in statuses:
            return store.get(jid)
        time.sleep(0.05)
    raise AssertionError(store.get(jid))


def _vod(store, tmp_path, fresh=False):
    jid = jobs.new_job_id()
    argv = jobs.build_vod_argv("https://example.com/v", _opts(drafts=False), store.job_dir(jid))
    if fresh:
        argv.append("--fresh")
    return store.create("vod", "v", argv, {"max_clips": 2}, job_id=jid)["id"]


def test_queue_runs_one_at_a_time_cancel_resume_restart(tmp_path, monkeypatch):
    store, runner = _runner(tmp_path, monkeypatch, sleep="0.3")
    a, b = _vod(store, tmp_path, fresh=True), _vod(store, tmp_path)
    runner.submit(a)
    runner.submit(b)
    _wait(store, a, ("running",))
    assert store.get(b)["status"] == "queued"
    assert runner.cancel(a) == "cancelled"
    assert _wait(store, a, ("cancelled",))["status"] == "cancelled"
    _wait(store, b, ("running", "done"))
    runner.resume(a)
    assert "--fresh" not in store.get(a)["argv"]
    assert _wait(store, a, ("done",))["returncode"] == 0
    assert (store.job_dir(a) / "out" / "clips.json").is_file()
    _wait(store, b, ("done",))
    store.update(b, status="running")
    store2 = jobs.JobStore(tmp_path / "ws")
    assert store2.get(b)["status"] == "interrupted"
    assert {j["id"] for j in store2.list()} == {a, b}
    runner.shutdown(5)


def test_live_stop_after_stop_after_sends_no_signal(tmp_path, monkeypatch):
    store, runner = _runner(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(jobs, "_signal_child", lambda p: sent.append(p) or True)
    jid = store.create("live", "x", ["live"], {})["id"]

    class P:
        def wait(self, timeout=None):
            return 0

    runner.current = (jid, P())
    log = store.job_dir(jid) / "log.txt"
    log.write_text("\n=== start ===\nLive: buffered 02:00\n", encoding="utf-8")
    assert runner.stop(jid) == "stopped" and len(sent) == 1
    log.write_text("\n=== start ===\nLive: --stop-after reached (2 min); stopping the ingest ...\n", encoding="utf-8")
    assert runner.stop(jid) == "stopped" and len(sent) == 1  # finishing on its own: no signal
    runner.current = None
    runner.shutdown(1)


def test_popen_gets_a_list_and_no_shell(tmp_path, monkeypatch):
    store, runner = _runner(tmp_path, monkeypatch)
    calls = []

    class P:
        returncode, pid = 0, 1

        def poll(self):
            return 0

    def fake_popen(argv, **kw):
        calls.append((argv, kw))
        return P()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    evil = 'https://x.tv/"; rm -rf / &'
    jid = jobs.new_job_id()
    argv = jobs.build_vod_argv(evil, _opts(drafts=False), store.job_dir(jid))
    store.create("vod", "x", argv, {}, job_id=jid)
    runner.submit(jid)
    _wait(store, jid, ("done",))
    ((argv, kw),) = calls
    assert isinstance(argv, list) and not kw.get("shell") and evil in argv
    runner.shutdown(1)
