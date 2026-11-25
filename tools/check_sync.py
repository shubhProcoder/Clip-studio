"""
Caption sync check for a rendered clip.

    python tools/check_sync.py <clip.mp4> <clip.clip.json> [<mp4> <clip.json> ...] [--model auto]

Re-transcribes the MP4 audio with faster-whisper (word timestamps) and compares the start of every
word caption in the clip plan (timeline seconds; the active word of each caption) with the start of
the same word in the MP4. Prints the median and max start error; exits 1 when the median is above
0.12 s, when any timeline piece's median is above 0.12 s, when off words (> 0.25 s outside the heard
word) form more off bursts than 10% of the matched words, or (with >= 5 captions) when fewer than
half of the captions find their word within 0.6 s.
"""

import argparse
import difflib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from streamclipper.asr import load_model, read_wav16k
from streamclipper.util import norm_token

MEDIAN_MAX = 0.12
OFF_S = 0.25
OFF_SHARE = 0.10
MATCH_WINDOW = 0.6  # wider windows pair a caption with a different word when the two transcripts differ
MIN_MATCHED = 0.5  # a real offset > 0.6 s leaves most captions unmatched

_BRACE = re.compile(r"\{[a-z#0-9]+:([^}]*)\}")


def _tokens(text: str) -> list:
    return [t for t in (norm_token(x) for x in _BRACE.sub(r"\1", text).split()) if t]


def caption_words(plan: dict) -> list:
    """(timeline start, token) of the active word of every word caption (hook banners skipped)."""
    hook = str(plan.get("hook_text") or "")
    out, run_key, run_i = [], None, 0
    for c in plan.get("captions") or []:
        if c.get("background") or c.get("layer") or (hook and c.get("text") == hook):
            run_key = None
            continue
        toks = _tokens(c.get("text", ""))
        if not toks:
            continue
        run_i = run_i + 1 if toks == run_key else 0
        run_key = toks
        if run_i < len(toks):
            out.append((float(c["tl_start"]), toks[run_i]))
    return out


def compare(caps: list, heard: list, segments: list = ()) -> dict:
    """
    Caption words aligned with the heard words as token sequences (difflib, so repeated words like
    'wait wait wait' pair up in order even when the two transcripts count them differently); aligned
    pairs more than 0.6 s apart are alignment misses and skipped. A word counts as off when its caption
    appears more than 0.25 s before the heard word starts or more than 0.25 s after it ends (a caption
    shown while the word is spoken reads as in sync; Whisper word edges in fast shouting differ by a few
    hundred ms between two transcriptions of the same audio). Off words less than 1 s apart form one off
    burst: in machine-gun speech ('wait wait wait wait I know') two transcriptions disagree on the word
    count for a moment, while an offset bug moves every word of a piece. seg_medians: median error per
    timeline piece (segments = [(tl_start, tl_end)]) with >= 4 matched words, which shows such a shift.
    """
    a = [tok for _, tok in caps]
    b = [norm_token(w["w"]) for w in heard]
    pairs = []
    for blk in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        pairs += [(caps[blk.a + k][0], heard[blk.b + k]) for k in range(blk.size)]
    errs, off_t, timed = [], [], []
    for t, w in pairs:
        if abs(w["s"] - t) > MATCH_WINDOW:
            continue
        errs.append(abs(w["s"] - t))
        timed.append((t, abs(w["s"] - t)))
        if t < w["s"] - OFF_S or t > max(w["e"], w["s"]) + OFF_S:
            off_t.append(t)
    bursts = sum(1 for i, t in enumerate(off_t) if i == 0 or t - off_t[i - 1] >= 1.0)
    seg_medians = []
    for a0, a1 in segments:
        e = sorted(x for t, x in timed if a0 <= t < a1)
        if len(e) >= 4:
            seg_medians.append(round(float(e[len(e) // 2]), 3))
    errs.sort()
    n = len(errs)
    med = errs[n // 2] if n % 2 else ((errs[n // 2 - 1] + errs[n // 2]) / 2 if n else 0.0)
    return {
        "captions": len(caps),
        "matched": n,
        "median": med,
        "max": errs[-1] if errs else 0.0,
        "off": len(off_t),
        "bursts": bursts,
        "seg_medians": seg_medians,
    }


def transcribe_mp4(model, mp4: Path) -> list:
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(mp4),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(wav),
            ],
            check=True,
        )
        words, _ = model.run(read_wav16k(wav), language="en")
    return words


def main() -> int:
    import json

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="pairs: <mp4> <clip.json>")
    ap.add_argument("--model", default="auto", help="faster-whisper model (default auto)")
    a = ap.parse_args()
    if len(a.files) % 2:
        ap.error("pass files in pairs: <mp4> <clip.json>")
    model = load_model("auto", 60.0, a.model)
    bad = 0
    for mp4, cj in zip(a.files[::2], a.files[1::2], strict=True):
        plan = json.loads(Path(cj).read_text(encoding="utf-8"))
        caps = caption_words(plan)
        segs = [
            (float(s["tl_start"]), float(s["tl_start"]) + float(s["src_e"]) - float(s["src_s"]))
            for s in plan["segments"]
        ]
        r = compare(caps, transcribe_mp4(model, Path(mp4)), segs)
        fail = bool(r["matched"]) and (r["median"] > MEDIAN_MAX or r["bursts"] > OFF_SHARE * r["matched"])
        fail = fail or any(m > MEDIAN_MAX for m in r["seg_medians"])
        fail = fail or (r["captions"] >= 5 and r["matched"] < MIN_MATCHED * r["captions"])
        print(
            f"{'FAIL' if fail else 'PASS'}  {Path(mp4).name}: {r['matched']}/{r['captions']} caption words matched, "
            f"median start error {r['median']:.3f}s, max {r['max']:.3f}s, {r['off']} off by > {OFF_S}s "
            f"({r['bursts']} burst(s)), piece medians {r['seg_medians']}"
        )
        bad += bool(fail)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
