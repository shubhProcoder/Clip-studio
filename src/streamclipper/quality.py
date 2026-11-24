"""Source static-screen quality analysis.

FFmpeg's tblend/signalstats pipeline produces a mean luma difference map.
Low difference buckets identify frozen ads, BRB screens, loading screens and
menus.  Candidates retain a quality explanation and are only rejected when a
large share of the candidate is static.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from streamclipper.state import write_json_atomic
from streamclipper.util import fwd

STATIC_ENABLED = True
STATIC_BUCKET_S = 2.0
STATIC_MIN_S = 6.0
STATIC_THRESHOLD = 0.4
DOMINATED_OVERLAP = 0.75
PARTIAL_OVERLAP = 0.25
MAX_PENALTY = 20

_PTS = re.compile(r"pts_time:([0-9.]+)")
_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")


def motion_map(path: str | Path, bucket: float = STATIC_BUCKET_S) -> list[tuple[float, float]]:
    """Return [(bucket_start, mean_luma_difference)] or [] when FFmpeg cannot read the source."""
    bucket = max(0.1, float(bucket))
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-vf",
                "tblend=all_mode=difference,signalstats,metadata=print:file=-",
                "-an",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=900,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    times = []
    vals = []
    at = 0.0
    for line in (proc.stdout or "").splitlines():
        pts = _PTS.search(line)
        if pts:
            at = float(pts.group(1))
            continue
        yavg = _YAVG.search(line)
        if yavg:
            times.append(at)
            vals.append(float(yavg.group(1)))
    if len(vals) > 1:
        times, vals = times[1:], vals[1:]
    if not vals:
        return []
    buckets = {}
    for t, value in zip(times, vals, strict=False):
        buckets.setdefault(int(t / bucket), []).append(value)
    return [(i * bucket, sum(values) / len(values)) for i, values in sorted(buckets.items())]


def static_ranges(
    path: str | Path,
    bucket: float = STATIC_BUCKET_S,
    threshold: float = STATIC_THRESHOLD,
    min_seconds: float = STATIC_MIN_S,
) -> list[tuple[float, float]]:
    """Find consecutive low-motion bucket runs of at least min_seconds."""
    return ranges_from_motion(motion_map(path, bucket), bucket, threshold, min_seconds)


def ranges_from_motion(
    motion: list[tuple[float, float]],
    bucket: float = STATIC_BUCKET_S,
    threshold: float = STATIC_THRESHOLD,
    min_seconds: float = STATIC_MIN_S,
) -> list[tuple[float, float]]:
    if not motion:
        return []
    out = []
    start = None
    bucket = max(0.1, float(bucket))
    threshold = max(0.0, float(threshold))
    min_seconds = max(0.0, float(min_seconds))
    for t, value in motion:
        if value < threshold:
            if start is None:
                start = t
        elif start is not None:
            end = t
            if end - start >= min_seconds:
                out.append((round(start, 3), round(end, 3)))
            start = None
    if start is not None:
        end = motion[-1][0] + bucket
        if end - start >= min_seconds:
            out.append((round(start, 3), round(end, 3)))
    return out


def static_overlap(start: float, end: float, ranges: list[tuple[float, float]]) -> float:
    total = 0.0
    for a, b in ranges:
        total += max(0.0, min(end, b) - max(start, a))
    return round(total, 3)


def motion_score(start: float, end: float, motion: list[tuple[float, float]], bucket: float) -> float | None:
    """Mean luma motion of the candidate window; None when no motion buckets cover it."""
    values = []
    for t, value in motion:
        if t + bucket > start and t < end:
            values.append(value)
    return round(sum(values) / len(values), 3) if values else None


def overlap_intervals(start: float, end: float, ranges: list[tuple[float, float]]) -> list[list[float]]:
    intervals = []
    for a, b in ranges:
        lo, hi = max(start, a), min(end, b)
        if hi > lo:
            intervals.append([round(lo, 3), round(hi, 3)])
    return intervals


def assess_static(
    start: float,
    end: float,
    peak: float,
    ranges: list[tuple[float, float]],
    motion: list[tuple[float, float]] | None = None,
    bucket: float = STATIC_BUCKET_S,
    protect_payoff: bool = True,
) -> dict:
    """Return static overlap and the quality decision for one candidate.

    A clip dominated by static footage is rejected. A partially static clip is
    penalised but remains available.  A payoff at the clipped peak is protected
    from a blanket rejection: a genuine gameplay pause/setup is not treated as
    an advertisement by itself.
    """
    duration = max(1e-6, end - start)
    overlap = static_overlap(start, end, ranges)
    overlap_pct = min(1.0, overlap / duration)
    payoff_static = protect_payoff and any(a <= peak <= b for a, b in ranges)
    effective_pct = overlap_pct
    if payoff_static and overlap_pct < 0.9:
        effective_pct = overlap_pct * 0.5
    decision = "pass"
    penalty = 0
    reason = ""
    if effective_pct >= DOMINATED_OVERLAP:
        decision = "reject"
        reason = f"dominated by static footage ({overlap_pct:.0%} overlap)"
    elif effective_pct >= PARTIAL_OVERLAP:
        decision = "penalize"
        penalty = int(round(MAX_PENALTY * effective_pct))
        reason = f"partial static footage ({overlap_pct:.0%} overlap)"
    return {
        "decision": decision,
        "rejection_reason": reason if decision == "reject" else "",
        "penalty": penalty,
        "static_overlap_seconds": overlap,
        "static_overlap_percentage": round(overlap_pct, 3),
        "effective_static_percentage": round(effective_pct, 3),
        "static_interval": overlap_intervals(start, end, ranges),
        "payoff_static": payoff_static,
        "motion_score": motion_score(start, end, motion or [], bucket),
    }


def quality_stage(
    source: str | Path,
    work_dir: str | Path,
    state,
    bucket: float = STATIC_BUCKET_S,
    threshold: float = STATIC_THRESHOLD,
    min_seconds: float = STATIC_MIN_S,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Run static analysis once per source/config and cache it in state.json."""
    source = Path(source)
    work_dir = Path(work_dir)
    raw_key = json.dumps(
        {
            "source": fwd(source),
            "size": source.stat().st_size if source.is_file() else 0,
            "bucket": bucket,
            "threshold": threshold,
            "min_seconds": min_seconds,
        },
        sort_keys=True,
    )
    key = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:12]
    saved = state.stages.get("quality") or {}
    cache = work_dir / "quality"
    cache.mkdir(parents=True, exist_ok=True)
    motion_file = cache / "motion_map.json"
    ranges_file = cache / "static_ranges.json"
    if state.resumed and saved.get("key") == key and motion_file.is_file() and ranges_file.is_file():
        try:
            motion = [tuple(x) for x in json.loads(motion_file.read_text(encoding="utf-8"))]
            ranges = [tuple(x) for x in json.loads(ranges_file.read_text(encoding="utf-8"))]
            print("Resuming: static-screen analysis already done")
            return motion, ranges
        except (OSError, ValueError, TypeError):
            pass
    print("Analysing source motion for static screens...")
    motion = motion_map(source, bucket)
    ranges = ranges_from_motion(motion, bucket, threshold, min_seconds)
    write_json_atomic(motion_file, motion)
    write_json_atomic(ranges_file, ranges)
    state.set_stage(
        "quality",
        {
            "key": key,
            "bucket": bucket,
            "threshold": threshold,
            "min_seconds": min_seconds,
            "buckets": len(motion),
            "ranges": len(ranges),
        },
    )
    if motion:
        print(f"Static-screen analysis: {len(ranges)} range(s), threshold {threshold:g}")
    else:
        print("Static-screen analysis unavailable: continuing without static rejection")
    return motion, ranges
