from streamclipper import quality
from streamclipper.cli import build_parser
from streamclipper.discovery import _discover_settings


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ffmpeg_output(values):
    lines = []
    for i, value in enumerate(values):
        lines.append(f"frame:{i} pts_time:{i * 2:.1f}")
        lines.append(f"lavfi.signalstats.YAVG={value:.3f}")
    return "\n".join(lines)


def test_motion_map_parses_ffmpeg_metadata(monkeypatch):
    monkeypatch.setattr(
        quality.subprocess,
        "run",
        lambda *a, **k: _Proc(stdout=_ffmpeg_output([10.0, 0.2, 0.3, 1.8, 2.1])),
    )
    got = quality.motion_map("clip.mp4", bucket=2.0)
    assert len(got) == 4
    assert got == [(2.0, 0.2), (4.0, 0.3), (6.0, 1.8), (8.0, 2.1)]


def test_frozen_screen_and_brb_ranges():
    motion = [(0.0, 0.1), (2.0, 0.1), (4.0, 0.1), (6.0, 0.1), (8.0, 1.6), (10.0, 1.7)]
    ranges = quality.ranges_from_motion(motion, bucket=2.0, threshold=0.4, min_seconds=6.0)
    assert ranges == [(0.0, 8.0)]


def test_active_gameplay_is_not_static():
    motion = [(0.0, 1.8), (2.0, 2.2), (4.0, 1.6), (6.0, 2.4)]
    assert quality.ranges_from_motion(motion, bucket=2.0, threshold=0.4, min_seconds=6.0) == []


def test_static_menu_and_loading_screen_are_detected():
    motion = [(0.0, 0.1), (2.0, 0.2), (4.0, 0.1), (6.0, 0.2), (8.0, 0.1)]
    assert quality.ranges_from_motion(motion, bucket=2.0, threshold=0.4, min_seconds=6.0) == [(0.0, 10.0)]


def test_short_pause_is_not_a_static_range():
    motion = [(0.0, 1.5), (2.0, 0.2), (4.0, 0.3), (6.0, 1.9)]
    assert quality.ranges_from_motion(motion, bucket=2.0, threshold=0.4, min_seconds=6.0) == []


def test_ffmpeg_failure_is_a_graceful_fallback(monkeypatch):
    monkeypatch.setattr(quality.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert quality.motion_map("missing.mp4") == []
    monkeypatch.setattr(quality.subprocess, "run", lambda *a, **k: _Proc(returncode=234, stderr="failed"))
    assert quality.motion_map("bad.mp4") == []


def test_mixed_gameplay_and_static_segment():
    ranges = [(20.0, 26.0)]
    got = quality.assess_static(10.0, 30.0, 12.0, ranges, motion=[(10.0, 1.5), (20.0, 0.1)], bucket=2.0)
    assert got["static_overlap_seconds"] == 6.0
    assert got["static_overlap_percentage"] == 0.3
    assert got["decision"] in ("pass", "penalize")
    assert got["decision"] != "reject"
    assert got["penalty"] > 0


def test_dominated_static_candidate_is_rejected():
    got = quality.assess_static(0.0, 30.0, 25.0, [(0.0, 30.0)], motion=[(0.0, 0.1)], bucket=2.0)
    assert got["decision"] == "reject"
    assert got["rejection_reason"]


def test_genuine_pause_near_payoff_is_not_blindly_rejected():
    got = quality.assess_static(
        0.0,
        30.0,
        15.0,
        [(10.0, 20.0)],
        motion=[(0.0, 1.5), (10.0, 0.1), (20.0, 1.5)],
        bucket=2.0,
    )
    assert got["decision"] in ("pass", "penalize")
    assert got["decision"] != "reject"
    assert got["payoff_static"]


def test_static_analysis_cli_defaults_and_overrides():
    parser = build_parser()
    args = parser.parse_args(
        [
            "discover-clips",
            "--out",
            "out/clips.json",
            "--static-analysis",
            "off",
            "--static-bucket",
            "3",
            "--static-min",
            "9",
            "--static-threshold",
            "0.7",
        ]
    )
    st = _discover_settings(args, args.transcribe)
    assert st["static_analysis"] is False
    assert st["static_bucket"] == 3.0
    assert st["static_min"] == 9.0
    assert st["static_threshold"] == 0.7
