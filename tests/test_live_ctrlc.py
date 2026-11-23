"""Ctrl+C in live mode: the console signal reaches streamclipper only; the clip being rendered still finishes."""

import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

SRC = Path(__file__).resolve().parents[1] / "src"

# A stand-in for `streamclipper live`: a 4 s real-time ffmpeg "render" in a worker thread while the main
# thread waits; KeyboardInterrupt -> wait for the render, as LiveRun.run() does.
CHILD = textwrap.dedent(
    """
    import signal, sys, threading, time
    from streamclipper.live.command import _interrupt, stop_on_console_signals
    from streamclipper.util import ToolError, run_cmd

    if sys.argv[2] == "isolated":
        stop_on_console_signals()
    elif hasattr(signal, "SIGBREAK"):  # same stop path, but the render shares the console group
        signal.signal(signal.SIGBREAK, _interrupt)
    result = {}

    def render():
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
               "-i", "testsrc2=s=320x240:r=30:d=4", "-c:v", "libx264", "-preset", "ultrafast", sys.argv[1]]
        try:
            run_cmd(cmd, "render")
            result["ok"] = True
        except ToolError:
            result["ok"] = False

    th = threading.Thread(target=render)
    th.start()
    print("RENDERING", flush=True)
    try:
        while th.is_alive():
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("STOPPING", flush=True)
        th.join()
    print("RENDER OK" if result.get("ok") else "RENDER FAILED", flush=True)
    """
)


def _interrupt_run(tmp_path, mode: str) -> tuple:
    out = tmp_path / f"{mode}.mp4"
    flags = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SRC), os.environ.get("PYTHONPATH", "")]))
    p = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(out), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        **flags,
    )
    assert p.stdout.readline().strip() == "RENDERING"
    time.sleep(1.5)  # ffmpeg is encoding now
    if sys.platform == "win32":
        os.kill(p.pid, signal.CTRL_BREAK_EVENT)  # the parent's console process group only
    else:
        os.killpg(p.pid, signal.SIGINT)  # like a terminal Ctrl+C: the parent's foreground group
    text, _ = p.communicate(timeout=60)
    return p.returncode, text, out


def test_ctrl_c_stops_the_parent_and_the_render_finishes(tmp_path):
    rc, text, out = _interrupt_run(tmp_path, "isolated")
    assert rc == 0 and "STOPPING" in text and "RENDER OK" in text, text
    assert out.is_file() and out.stat().st_size > 0


def test_without_own_groups_the_signal_kills_the_render(tmp_path):
    """Control: the same signal does reach a render in the parent's group (so the test above means something)."""
    rc, text, _ = _interrupt_run(tmp_path, "shared")
    assert "STOPPING" in text and "RENDER FAILED" in text, text
