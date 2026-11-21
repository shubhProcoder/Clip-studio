import shutil
import subprocess

import pytest

from streamclipper.hud import frames as F

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_lowres_size_estimate_rules():
    info = {
        "duration": 3600,
        "formats": [
            {"vcodec": "none", "acodec": "mp4a", "filesize": 50e6},  # audio only
            {"vcodec": "avc1", "height": 1080, "filesize": 4e9},  # too high
            {"vcodec": "avc1", "height": 240, "filesize": 90e6},
            {"vcodec": "vp9", "height": 360, "filesize_approx": 2e8},
        ],
    }
    assert F.lowres_size_estimate(info) == 2e8  # the best format <= 360p
    tbr_only = {"duration": 10000, "formats": [{"vcodec": "avc1", "height": 360, "tbr": 1500}]}
    assert F.lowres_size_estimate(tbr_only) == pytest.approx(1500 * 1000 / 8 * 10000)  # 1.875 GB
    assert F.lowres_size_estimate({"formats": [{"vcodec": "avc1", "height": 360}]}) is None


def test_big_url_video_is_skipped_unless_forced(tmp_path, monkeypatch, capsys):
    big = {"duration": 36000, "formats": [{"vcodec": "avc1", "height": 360, "tbr": 1000}]}  # 4.5 GB
    called = []
    monkeypatch.setattr("streamclipper.download.download_lowres_video", lambda *a: called.append(a) or tmp_path)
    assert F.analysis_source(None, "https://example.invalid/v", tmp_path, info=big) is None
    assert "limit 1.5 GB" in capsys.readouterr().out and not called
    assert F.analysis_source(None, "https://example.invalid/v", tmp_path, force=True, info=big) == tmp_path
    assert called


@pytest.fixture(scope="module")
def testsrc(tmp_path_factory):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    p = tmp_path_factory.mktemp("hud") / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30"]
        + ["-t", "10", "-g", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)],
        check=True,
    )
    return p


@needs_ffmpeg
def test_local_video_is_its_own_source_and_keyframes_are_spaced(testsrc, tmp_path):
    assert F.analysis_source(testsrc, "", tmp_path) == testsrc
    got = list(F.keyframes(testsrc, width=320, use_cuda=False))
    ts = [t for t, _ in got]
    assert 4 <= len(got) <= 6  # one keyframe per second, at least 1.9 s apart
    assert all(b - a >= 1.85 for a, b in zip(ts, ts[1:], strict=False))
    assert got[0][1].shape == (180, 320, 3)


@needs_ffmpeg
def test_dense_pass_times_and_size(testsrc):
    got = list(F.dense(testsrc, 2.0, 4.0, fps=3, width=640))
    assert len(got) == 6
    assert [t for t, _ in got] == [2.0, 2.333, 2.667, 3.0, 3.333, 3.667]
    assert got[0][1].shape == (360, 640, 3)
    assert F.frame_at(testsrc, 1.0).shape == (360, 640, 3)


def test_audio_only_input_without_url_is_skipped(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(F, "has_video", lambda p: False)
    assert F.analysis_source(tmp_path / "a.m4a", "", tmp_path) is None
    assert "no video stream" in capsys.readouterr().out
