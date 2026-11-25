import zipfile
from pathlib import Path

import pytest

from streamclipper import colab, commands
from streamclipper.util import ToolError


def _fake_clip_stream(calls):
    def fake(args):
        calls.append(args)
        out = Path(args.out)
        (out / "clip_01.mp4").write_bytes(b"mp4")
        (out / "clip_01_thumb.jpg").write_bytes(b"jpg")
        draft = Path(args.draft_root) / "clip_01"
        (draft / "media").mkdir(parents=True, exist_ok=True)
        (draft / "draft_content.json").write_text("{}", encoding="utf-8")
        (draft / "media" / "gameplay.mp4").write_bytes(b"v")
        doc = {"clips": [{"rank": 1, "title": "No Way"}]}
        return {"doc": doc, "plans": [{"name": "clip_01", "draft": "clip_01"}], "gate": [("clip_01", True)]}

    return fake


def test_run_pipeline_builds_the_clip_stream_argv_and_collects_outputs(tmp_path, monkeypatch):
    src = tmp_path / "my_stream.mp4"
    src.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(commands, "clip_stream", _fake_clip_stream(calls))
    out = tmp_path / "out"
    res = colab.run_pipeline(str(src), out_dir=str(out), max_clips=0, min_score=55, llm="ollama", resume=False)
    (args,) = calls
    assert args.input == str(src.resolve()) and not args.url
    assert args.max_clips == 0 and args.min_score == 55.0 and args.llm == "ollama"
    assert args.render and args.portable_drafts and not args.no_draft and args.fresh
    assert args.draft_root == str((out / "capcut_drafts").resolve())
    assert res["mp4s"] == [out.resolve() / "clip_01.mp4"]
    assert res["thumbs"] == [out.resolve() / "clip_01_thumb.jpg"]
    assert res["clips"][0]["title"] == "No Way"
    with zipfile.ZipFile(res["drafts_zip"]) as zf:
        assert sorted(zf.namelist()) == ["clip_01/draft_content.json", "clip_01/media/gameplay.mp4"]


def test_run_pipeline_url_without_drafts_and_failed_gate(tmp_path, monkeypatch):
    calls = []

    def fake(args):
        calls.append(args)
        return {"doc": {"clips": []}, "plans": [], "gate": [("clip_01", False)]}

    monkeypatch.setattr(commands, "clip_stream", fake)
    with pytest.raises(ToolError, match="failed the export gate"):
        colab.run_pipeline("https://youtu.be/abc", out_dir=str(tmp_path / "o"), drafts=False, render=True)
    assert calls[0].url == "https://youtu.be/abc" and calls[0].no_draft and not calls[0].fresh


def test_missing_local_source_is_a_clear_error(tmp_path):
    with pytest.raises(ToolError, match="Source video not found"):
        colab.run_pipeline(str(tmp_path / "nope.mp4"), out_dir=str(tmp_path / "o"))


def test_zip_outputs_has_only_the_results(tmp_path):
    out = tmp_path / "StreamClipper"
    (out / "windows").mkdir(parents=True)
    for name in ("clip_01.mp4", "clip_01_thumb.jpg", "clips.csv", "clips.json", "index.html", "capcut_drafts.zip"):
        (out / name).write_bytes(b"x")
    (out / "windows" / "w.mp4").write_bytes(b"big")
    (out / "clip_02.part.mp4").write_bytes(b"half")
    (out / "state.txt").write_bytes(b"s")
    zp = colab.zip_outputs(out, tmp_path / "streamclipper_outputs.zip")
    with zipfile.ZipFile(zp) as zf:
        assert sorted(zf.namelist()) == sorted(
            ["clip_01.mp4", "clip_01_thumb.jpg", "clips.csv", "clips.json", "index.html", "capcut_drafts.zip"]
        )
        assert zf.getinfo("clip_01.mp4").compress_type == zipfile.ZIP_STORED


def test_results_html_cards(tmp_path):
    (tmp_path / "clip_01.mp4").write_bytes(b"mp4")
    clips = [
        {"rank": 1, "score": 77, "category": "funny", "title": "<b>x</b>", "description": "d", "hashtags": ["#a"],
         "mp4": "clip_01.mp4", "drive_link": "https://drive.google.com/file/d/ID/view"},
        {"rank": 2, "score": 60, "title": "two"},
    ]  # fmt: skip
    page = colab.results_html({"out_dir": tmp_path, "clips": clips}, previews=1)
    assert page.count("<video") == 1 and "&lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page
    assert page.count(">Copy</button>") == 4 and "navigator.clipboard.writeText" in page
    assert "https://drive.google.com/file/d/ID/view" in page and "score 77" in page


def test_drive_out_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMCLIPPER_OUT", str(tmp_path))
    assert colab.drive_out_dir("StreamClipper/live/x") == tmp_path / "StreamClipper" / "live" / "x"
    monkeypatch.delenv("STREAMCLIPPER_OUT")
    monkeypatch.chdir(tmp_path)
    assert colab.drive_out_dir() == tmp_path / "StreamClipper"


def test_live_run_keeps_the_buffer_local_and_copies_only_results(tmp_path, monkeypatch):
    monkeypatch.delenv("STREAMCLIPPER_WORK", raising=False)
    monkeypatch.chdir(tmp_path)
    assert colab.live_work_dir("chan_2026-11-19") == tmp_path / "streamclipper_live" / "chan_2026-11-19"
    work, drive = tmp_path / "work", tmp_path / "drive"
    local = ("buffer/seg_000001.ts", "windows/live_1_2.mp4", "panels/p.mp4", "LIVE_02.part.mp4", "LIVE_01.clip.json")
    for rel in local:
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_bytes(b"local only")
    for name in ("LIVE_01.mp4", "LIVE_01_thumb.jpg", "clips.json", "clips.csv", "index.html"):
        (work / name).write_bytes(b"result")
    assert sorted(colab.sync_live_outputs(work, drive)) == sorted(
        ["LIVE_01.mp4", "LIVE_01_thumb.jpg", "clips.csv", "clips.json", "index.html"]
    )
    assert sorted(p.name for p in drive.rglob("*")) == sorted(
        ["LIVE_01.mp4", "LIVE_01_thumb.jpg", "clips.csv", "clips.json", "index.html"]
    )
    assert colab.sync_live_outputs(work, drive) == []  # unchanged files are not copied again
    (work / "clips.json").write_bytes(b"updated after clip 2")
    assert colab.sync_live_outputs(work, drive) == ["clips.json"]


def test_linux_cuda_libraries_of_the_gpu_extra_are_preloaded(tmp_path, monkeypatch):
    # real Colab T4 run (Nov 2026 image): "Library libcublas.so.12 is not found" -> Whisper fell back to the CPU
    from streamclipper import env

    for sub, name in (("cublas", "libcublasLt.so.12"), ("cublas", "libcublas.so.12"), ("cudnn", "libcudnn.so.9")):
        d = tmp_path / "nvidia" / sub / "lib"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(b"")
    monkeypatch.setattr(env, "_system_has_lib", lambda name: False)
    loaded = []
    used = env.preload_linux_cuda([str(tmp_path / "missing"), str(tmp_path)], cdll=loaded.append)
    assert [p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in loaded] == [
        "libcublasLt.so.12",
        "libcublas.so.12",
        "libcudnn.so.9",
    ]
    assert len(used) == 2
    monkeypatch.setattr(env, "_system_has_lib", lambda name: True)  # the system CUDA 12 wins
    loaded.clear()
    assert env.preload_linux_cuda([str(tmp_path)], cdll=loaded.append) == [] and loaded == []
