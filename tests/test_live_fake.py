"""Fake live: synthetic 130 s stream cut into 10 s segments, fed at accelerated pace through ring, engine and worker."""

import json
import shutil
import subprocess
import threading
import time
import warnings

import pytest

from streamclipper.env import ensure_bundled_assets, find_capcut_local
from streamclipper.live.command import LiveRun
from streamclipper.live.engine import Engine
from streamclipper.live.ingest import segment_args
from streamclipper.live.ring import Ring
from streamclipper.live.worker import Worker
from streamclipper.options import resolve_options

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

TOTAL, EVENTS = 130.0, (35.0, 100.0)  # > 60 s apart: find_peaks keeps one peak per max_dur, as in VOD mode
TALK = "so we go in here and then we try the door and look what is behind it right now".split()


def _talking(t: float) -> bool:
    return (t % 2.7) < 2.2 and not any(e <= t < e + 2.8 for e in EVENTS)


def _words() -> list:
    words, k = [], 0
    t = 0.0
    while t < TOTAL:
        if _talking(t) and _talking(t + 0.25):
            words.append({"w": TALK[k % len(TALK)], "s": round(t, 3), "e": round(t + 0.25, 3)})
            k += 1
            t += 0.3
            if not _talking(t + 0.25):
                words[-1]["w"] += "."
        else:
            t += 0.05
    for e in EVENTS:
        words += [{"w": "NO", "s": e + 0.1, "e": e + 0.5}, {"w": "WAY!", "s": e + 0.6, "e": e + 1.4}]
    return sorted(words, key=lambda w: w["s"])


def _make_stream(path):
    quiet = "*".join(f"(1-between(t,{e},{e + 2.8}))" for e in EVENTS)
    burst = "+".join(f"0.6*sin(2*PI*700*t)*between(t,{e},{e + 1.6})" for e in EVENTS)
    expr = f"0.03*sin(2*PI*440*t)*lt(mod(t,2.7),2.2)*{quiet}+{burst}"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d={TOTAL:g}",
            "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d={TOTAL:g}",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-shortest", str(path),
        ],
        check=True,
    )  # fmt: skip


class FakeIngest:
    reconnects, end_reason = 0, "fake source finished"

    def __init__(self):
        self.finished = threading.Event()

    def start(self):
        pass

    def stop(self):
        self.finished.set()

    def join(self, timeout=None):
        pass


def test_fake_live_one_clip_per_planted_event(tmp_path):
    t0 = time.time()
    src = tmp_path / "stream.mp4"
    _make_stream(src)
    stage = tmp_path / "stage"
    stage.mkdir()
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
        + segment_args(stage / "all.csv", stage, 0),
        check=True,
    )
    rows = [ln for ln in (stage / "all.csv").read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) >= 11

    out = tmp_path / "out"
    ring = Ring(out / "buffer", keep_s=100.0)
    outside = tmp_path / "seg_000000.ts"  # same name as a ring segment, outside the buffer folder
    outside.write_bytes(b"not ours")
    stranger = ring.dir / "seg_000777.ts"  # inside the buffer, but no segment list of this run names it
    stranger.write_bytes(b"not ours")
    ring.watch(ring.dir / "live.csv")
    (out / "windows").mkdir(parents=True)
    decoy = out / "windows" / "my_own_video.mp4"  # not made by this run: must survive the window cleanup
    decoy.write_bytes(b"not ours")

    words = _words()

    def fake_asr(seg):
        return [w for w in words if seg.start - 2.0 <= w["s"] < seg.end], seg.start - 1.0

    engine = Engine({"min_dur": 10.0, "target_dur": 15.0}, fake_asr, keep_s=100.0, tmp_dir=out / "tmp")
    args = type("A", (), {})()
    opts = resolve_options(args, {})
    info = {
        "label": "fake-live",
        "title": "fake",
        "st": dict(engine.st, chat=False),
        "transcribed": True,
        "language": "en",
        "asr": "live",
        "llm": "none",
        "duration": lambda: ring.end,
        "t_start": t0,
    }
    assets = ensure_bundled_assets(find_capcut_local(), install=False)
    worker = Worker(out, opts, assets, info, render=True, no_draft=True, moment_lock=engine.lock)
    run = LiveRun(ring, FakeIngest(), engine, worker, None, 0.0, out)

    lines = []
    for row in rows:  # one segment "arrives" at a time
        name = row.split(",")[0]
        shutil.copy2(stage / name, ring.dir / name)
        lines.append(row + "\n")
        (ring.dir / "live.csv").write_text("".join(lines), encoding="utf-8")
        run.process_new()
    run.ingest.finished.set()
    run._loop()
    summary = run.finish()

    doc = json.loads((out / "clips.json").read_text(encoding="utf-8"))
    peaks = sorted(c["peak"] for c in doc["clips"])
    assert len(peaks) == len(EVENTS), peaks
    for e, p in zip(EVENTS, peaks, strict=True):
        assert e - 1.0 <= p <= e + 3.0
    for c in doc["clips"]:
        assert c["mp4"] and (out / c["mp4"]).is_file() and c["gate"] is True
        assert c["latency_s"] is not None and c["title"]
    assert (out / "clips.csv").is_file() and (out / "index.html").is_file()
    assert doc["mode"] == "live" and summary["clips"] == len(EVENTS)
    # deletion scope: only this run's own segments, only inside its buffer folder
    assert outside.read_bytes() == b"not ours" and stranger.read_bytes() == b"not ours"
    assert summary["segments_deleted"] >= 1
    left = sorted(p.name for p in ring.dir.glob("seg_*.ts"))
    assert "seg_000000.ts" not in left and "seg_000777.ts" in left
    # --no-draft: each window (and the copy cut from it) is deleted once its clip passed the gate
    assert sorted(p.name for p in (out / "windows").glob("*.mp4")) == ["my_own_video.mp4"]
    assert decoy.read_bytes() == b"not ours" and summary["windows_deleted"] == 2 * len(EVENTS)
    elapsed = time.time() - t0
    if elapsed > 40.0:  # soft budget: a busy CPU (parallel renders, CI, Colab) must not fail the suite
        warnings.warn(f"fake live took {elapsed:.0f} s (budget 40 s): slow or busy machine", stacklevel=1)
