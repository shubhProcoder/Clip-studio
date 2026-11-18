"""Loudness, RMS envelope, speech islands and peak finding."""

import re
from pathlib import Path

from streamclipper.util import DRAFT_FPS, TOOL_HINTS, ToolError, file_tag, run_cmd


def measure_loudness(path: Path, start: float = None, dur: float = None):
    """Integrated loudness (LUFS) via ffmpeg ebur128, or None when there is no audio."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-i", str(path), "-vn", "-af", "ebur128", "-f", "null", "-"]
    res = run_cmd(cmd, f"loudness scan of {Path(path).name}")
    vals = re.findall(r"^\s*I:\s*(-?[\d.]+|-inf)\s*LUFS", res.stderr or "", flags=re.M)
    if not vals or vals[-1] == "-inf":
        return None
    return float(vals[-1])


def loudness_gain(lufs, target: float = -14.0) -> float:
    if lufs is None:
        return 1.0
    return round(max(0.5, min(2.5, 10 ** ((target - lufs) / 20.0))), 3)


def extract_rms_islands(
    video_path: Path,
    raw_out: Path,
    silence_db: float = -48.0,
    min_pause_s: float = 0.20,
    pad_before_s: float = 0.05,
    pad_after_s: float = 0.08,
):
    """
    10 ms RMS speech islands. Switches to the voice band (220-3400 Hz) + adaptive percentile gate
    when continuous game audio/BGM fills the full band. Envelopes are cached next to raw_out.
    """
    env = audio_envelope(video_path, Path(raw_out).parent)
    return islands_from_env(env, silence_db, min_pause_s, pad_before_s, pad_after_s)


def extract_vocal_bandpass_islands(
    video_path: Path,
    raw_out: Path,
    silence_db: float = -48.0,
    min_pause_s: float = 0.20,
    pad_before_s: float = 0.05,
    pad_after_s: float = 0.08,
):
    """Voice-band (220-3400 Hz) islands with the adaptive percentile noise-floor gate."""
    env = audio_envelope(video_path, Path(raw_out).parent)
    return _islands_from_db(env["vox"], vox_gate(env["vox"], silence_db), min_pause_s, pad_before_s, pad_after_s, 0.25)


HOP_S = 0.01


def _np():
    try:
        import numpy
    except ImportError:
        raise ToolError("numpy is not installed (needed for audio analysis).", TOOL_HINTS["numpy"]) from None
    return numpy


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _mmss(t: float) -> str:
    t = int(max(0.0, float(t)))
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def _q(t: float) -> float:
    """Quantise to the 1/30 s draft frame grid."""
    return round(float(t) * DRAFT_FPS) / DRAFT_FPS


def _rms_db(raw: Path):
    """s16le mono 16 kHz file -> float32 dB per 10 ms window (-96 for digital silence)."""
    np = _np()
    n = Path(raw).stat().st_size // 320
    out = np.full(n, -96.0, dtype=np.float32)
    step = 60000  # 10 minutes per chunk keeps memory small for multi-hour streams
    with open(raw, "rb") as f:
        for a in range(0, n, step):
            b = min(n, a + step)
            x = np.fromfile(f, dtype="<i2", count=(b - a) * 160).astype(np.float32).reshape(-1, 160)
            rms = np.sqrt((x * x).mean(axis=1))
            out[a:b] = np.where(rms > 1.0, 20.0 * np.log10(np.maximum(rms, 1.0) / 32768.0), -96.0)
    return out


def envelope_arrays(path: Path, tmp_dir: Path = None) -> tuple:
    """
    (full, vox): 10 ms RMS envelopes in dB of the whole file, full band and voice band (highpass 220 /
    lowpass 3400 Hz), from one ffmpeg pass at 16 kHz mono. No cache (audio_envelope caches; live mode
    calls this once per segment). The temporary .raw files go to tmp_dir (default: next to the file).
    """
    path = Path(path)
    tmp_dir = Path(tmp_dir) if tmp_dir else path.parent
    tmp_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{path.stem}_{file_tag(path, 'env1')}"
    raw_full, raw_vox = tmp_dir / f"{stem}_full.raw", tmp_dir / f"{stem}_vox.raw"
    try:
        run_cmd(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-vn",
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "s16le",
                str(raw_full),
                "-map",
                "0:a:0",
                "-af",
                "highpass=f=220,lowpass=f=3400",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "s16le",
                str(raw_vox),
            ],
            f"audio analysis of {path.name}",
            hint="check that the file has an audio track",
        )
        return _rms_db(raw_full), _rms_db(raw_vox)
    finally:
        raw_full.unlink(missing_ok=True)
        raw_vox.unlink(missing_ok=True)


def audio_envelope(path: Path, work_dir: Path) -> dict:
    """
    envelope_arrays() cached as .npy (keyed by file stem + size/mtime hash) in work_dir:
    {full, vox, hop, dur}.
    """
    np = _np()
    path, work_dir = Path(path), Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{path.stem}_{file_tag(path, 'env1')}"
    npy_full, npy_vox = work_dir / f"{stem}_full.npy", work_dir / f"{stem}_vox.npy"
    if not (npy_full.is_file() and npy_vox.is_file()):
        full, vox = envelope_arrays(path, work_dir)
        np.save(npy_full, full)
        np.save(npy_vox, vox)
    full, vox = np.load(npy_full), np.load(npy_vox)
    n = min(len(full), len(vox))
    return {"full": full[:n], "vox": vox[:n], "hop": HOP_S, "dur": n * HOP_S}


def vox_gate(vox, silence_db: float) -> float:
    """Legacy percentile gate (p75 - 2.2 / p32 + 3.5), never below the noise floor (p05) + 6 dB."""
    np = _np()
    if len(vox) == 0:
        return silence_db
    s = np.sort(vox)
    p05, p32, p75 = (float(s[int(q * (len(s) - 1))]) for q in (0.05, 0.32, 0.75))
    return max(silence_db, min(p75 - 2.2, p32 + 3.5), p05 + 6.0)


def _islands_from_db(
    dbs, gate: float, min_pause_s: float, pad_before_s: float, pad_after_s: float, min_len_s: float
) -> list:
    np = _np()
    n = len(dbs)
    if n == 0:
        return []
    edges = np.diff(np.concatenate(([0], (dbs > gate).astype(np.int8), [0])))
    starts, ends = np.flatnonzero(edges == 1).tolist(), np.flatnonzero(edges == -1).tolist()
    gap_w = max(1, int(round(min_pause_s / HOP_S)))
    runs = []
    for s, e in zip(starts, ends, strict=False):
        if runs and s - runs[-1][1] < gap_w:
            runs[-1][1] = e
        else:
            runs.append([s, e])
    total = n * HOP_S
    islands = []
    for s, e in runs:
        if (e - s) * HOP_S < min_len_s:
            continue
        seg, after = dbs[s:e], dbs[e : min(n, e + 50)]
        peak = float(seg.max())
        ss, se = s * HOP_S, e * HOP_S
        cs, ce = max(0.0, ss - pad_before_s), min(total, se + pad_after_s)
        islands.append(
            {
                "cut_start": round(cs, 3),
                "cut_end": round(ce, 3),
                "speech_start": round(ss, 3),
                "speech_end": round(se, 3),
                "dur": round(ce - cs, 3),
                "peak_db": round(peak, 1),
                "mean_db": round(float(seg.mean()), 1),
                "shock_contrast": round(max(0.0, peak - (float(after.mean()) if len(after) else -60.0)), 1),
            }
        )
    return islands


def islands_from_env(
    env: dict,
    silence_db: float = -48.0,
    min_pause_s: float = 0.20,
    pad_before_s: float = 0.05,
    pad_after_s: float = 0.08,
) -> list:
    """Full-band speech islands; voice band + adaptive gate when a steady noise/BGM bed fills the audio."""
    islands = _islands_from_db(env["full"], silence_db, min_pause_s, pad_before_s, pad_after_s, 0.22)
    total = env["dur"]
    if total > 6.0 and (len(islands) <= 1 or sum(x["dur"] for x in islands) > 0.80 * total):
        vox = _islands_from_db(
            env["vox"], vox_gate(env["vox"], silence_db), min_pause_s, pad_before_s, pad_after_s, 0.25
        )
        if len(vox) > len(islands):
            return vox
    return islands


def _frames_db(db, k: int):
    """Average power of k consecutive 10 ms windows, in dB."""
    np = _np()
    m = len(db) // k
    if m == 0:
        return np.zeros(0)
    p = np.power(10.0, db[: m * k].astype(np.float64) / 10.0).reshape(-1, k).mean(axis=1)
    return 10.0 * np.log10(p + 1e-12)


def _peak_info(env: dict, i: int) -> dict:
    """Peak dict for 100 ms frame i (find_peaks must have run, it stores env['exc'])."""
    exc, v100, f100 = env["exc"], env["v100"], env["f100"]
    i = max(0, min(len(exc) - 1, int(i)))
    after = f100[i + 10 : i + 30]
    before = f100[max(0, i - 50) : max(0, i - 10)]
    setup = v100[max(0, i - 200) : max(0, i - 30)]
    pk_full = float(f100[max(0, i - 5) : i + 6].max())
    return {
        "t": round(i * 0.1 + 0.05, 2),
        "e_peak": round(float(exc[i]), 1),
        "shock_contrast": round(max(0.0, pk_full - float(after.mean())), 1) if len(after) else 0.0,
        "jump_db": round(pk_full - float(before.mean()), 1) if len(before) else 0.0,
        "setup_db": round(float(setup.mean()), 1) if len(setup) else None,
    }


def peak_near(env: dict, t: float, radius: float = 2.0) -> dict:
    """Peak dict at the loudest 100 ms frame within t +- radius (for chat and LLM candidates)."""
    exc = env["exc"]
    a, b = max(0, int((t - radius) * 10)), min(len(exc), int((t + radius) * 10) + 1)
    if b <= a:
        return _peak_info(env, int(t * 10))
    return _peak_info(env, a + int(exc[a:b].argmax()))


def laughter_bursts(env: dict, silence_db: float = -48.0) -> list:
    """
    Laughter-like bursts on the 10 ms voice envelope: >= 4 voiced bursts of 80-250 ms with gaps < 300 ms,
    the first four inside 2 s. Returns [(t, strength 0..1)], t = start of the burst train.
    """
    np = _np()
    vox = env["vox"]
    if len(vox) == 0:
        return []
    on = vox > vox_gate(vox, silence_db)
    edges = np.diff(np.concatenate(([0], on.astype(np.int8), [0])))
    runs = list(zip(np.flatnonzero(edges == 1).tolist(), np.flatnonzero(edges == -1).tolist(), strict=False))
    out, train = [], []

    def flush():
        if len(train) >= 4 and train[3][1] - train[0][0] <= 200:
            out.append((round(train[0][0] * HOP_S, 2), round(min(1.0, (len(train) - 3) / 5.0), 2)))

    for s, e in runs:
        if not 8 <= e - s <= 25:
            flush()
            train = []
            continue
        if train and s - train[-1][1] >= 30:
            flush()
            train = []
        train.append((s, e))
    flush()
    return out


def find_peaks(env: dict, top_n: int, min_sep: float) -> list:
    """
    Payoff candidates. Excitement E(t) = voice-band dB (100 ms frames) minus a rolling 60 s median
    baseline (1 s blocks), smoothed over 1 s. Local maxima with E >= 6 dB, greedy non-maximum
    suppression at min_sep seconds; keeps max(top_n * 4, 8), or every peak when top_n is 0.
    Adds 100 ms arrays (v100, f100, exc) to env for scoring.
    """
    np = _np()
    v100, f100 = _frames_db(env["vox"], 10), _frames_db(env["full"], 10)
    n = min(len(v100), len(f100))
    v100, f100 = v100[:n], f100[:n]
    env["v100"], env["f100"] = v100, f100
    if n < 30:
        return []
    v1 = _frames_db(env["vox"], 100)
    if len(v1) == 0:
        v1 = np.array([float(np.median(v100))])
    w = min(60, len(v1))
    padded = np.pad(v1, (w // 2, w - 1 - w // 2), mode="edge")
    base1 = np.median(np.lib.stride_tricks.sliding_window_view(padded, w), axis=1)
    base = np.repeat(base1, 10)
    base = np.pad(base, (0, max(0, n - len(base))), mode="edge")[:n]
    exc = np.convolve(v100 - base, np.ones(10) / 10.0, mode="same")
    env["exc"] = exc
    keep = max(int(top_n) * 4, 8) if top_n else len(exc)
    picked = []
    blocked = np.zeros(n, dtype=bool)
    k = max(1, int(np.ceil(min_sep * 10.0 - 1e-9)))  # frames j with |i - j| * 0.1 < min_sep
    for i in np.argsort(-exc, kind="stable").tolist():
        if exc[i] < 6.0 or len(picked) >= keep:
            break
        if blocked[i]:
            continue
        blocked[max(0, i - k + 1) : i + k] = True
        picked.append(_peak_info(env, i))
    return picked
