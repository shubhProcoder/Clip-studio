import pytest

from streamclipper import download
from streamclipper.util import ToolError


def test_slice_download_retries_a_network_drop(tmp_path, monkeypatch):
    calls, waits = [], []

    def flaky(argv, what, hint=""):
        calls.append(argv)
        if len(calls) < 3:
            raise ToolError("yt-dlp download failed: getaddrinfo failed", hint)
        raise RuntimeError("stop here: the third attempt ran")

    monkeypatch.setattr(download, "require_tool", lambda name: name)
    monkeypatch.setattr(download, "run_cmd", flaky)
    monkeypatch.setattr(download.time, "sleep", waits.append)
    with pytest.raises(RuntimeError, match="third attempt"):
        download.download_slice("https://youtu.be/x", 10.0, 20.0, tmp_path / "w.mp4")
    assert len(calls) == 3 and waits == list(download.SLICE_RETRY_WAITS)


def test_slice_download_gives_up_after_the_last_retry(tmp_path, monkeypatch):
    def down(argv, what, hint=""):
        raise ToolError("yt-dlp download failed", hint)

    monkeypatch.setattr(download, "require_tool", lambda name: name)
    monkeypatch.setattr(download, "run_cmd", down)
    monkeypatch.setattr(download.time, "sleep", lambda s: None)
    with pytest.raises(ToolError, match="yt-dlp download failed"):
        download.download_slice("https://youtu.be/x", 10.0, 20.0, tmp_path / "w.mp4")
