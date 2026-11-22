"""
Selection eval on the labelled fixtures in tests/fixtures/eval/*.json: score (scoring.combine) + quality bar
(scoring.passes_bar) + greedy non-overlap (scoring.select_clips), as discover() does with the shipped defaults.

  python tools/eval_selection.py                         # shipped defaults, every fixture
  python tools/eval_selection.py --split holdout         # only the held-out streams (tune | holdout | all)
  python tools/eval_selection.py --split holdout --before  # 5.2.1 defaults without the HUD and face signals
  python tools/eval_selection.py --before-llm            # without the strong-LLM exemption from the loudness floor
  python tools/eval_selection.py --no-bar                # v2 behaviour: score >= 50 only
  python tools/eval_selection.py --bar bar.json --weights weights.json   # try other values

Prints per fixture and per split: candidates, selected, precision (post-worthy among selected), recall of big
moments and recall of all post-worthy moments. Fixtures carry "split" (eval_stream_a/b count as tune) and
"content" (gaming | talk: talk fixtures use TALK_BAR and TALK_WEIGHTS).
--bar FILE: {"bar": {DEFAULT_BAR keys}, "talk_bar": {TALK_BAR keys}} (a flat object = DEFAULT_BAR keys).
--weights FILE: {SCORE_WEIGHTS keys, "face_motion": weight of the face part inside emotion}.
"""

import argparse
import json
import sys
from pathlib import Path
from unittest import mock

from streamclipper import scoring
from streamclipper.scoring import DEFAULT_BAR, NO_BAR, TALK_BAR, combine, passes_bar, select_clips

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "eval"
SPLITS = ("tune", "holdout", "all")
# 'before' = the shipped 5.2.1 values (no hud component, no face signal), frozen here so tuning cannot move them
BEFORE_WEIGHTS = {"hook": 20, "story": 25, "emotion": 25, "pacing": 10, "engagement": 20}
BEFORE_BAR = {
    "min_score": 50.0,
    "cue_min_score": 40.0,
    "scream_db": 24.0,
    "min_peak_db": 12.5,
    "min_setup_s": 3.0,
    "min_reaction": 0.1,
    "min_pacing": 0.25,
    "min_words": 6,
    "loop_min_words": 20,
    "min_distinct": 0.2,
    "llm_min_speech_rate": 1.2,
    "music_max_rate": 0.6,
    "music_min_voiced": 0.9,
    "laugh_exempt": 0.5,
    "hud_cue": 999.0,
    "llm_story_min": 1000.0,  # a strong LLM pick skipping min_peak_db came later: off here
    "llm_story_min_soft": 1000.0,
}
BEFORE_TALK_BAR = {
    "talk": True,
    "min_score": 50.0,
    "min_setup_s": 3.0,
    "talk_min_words": 25,
    "talk_min_rate": 1.5,
    "talk_laugh": 0.3,
    "talk_min_statement": 0.5,
    "loop_min_words": 20,
    "min_distinct": 0.2,
}
# --no-bar on talk fixtures: score >= 50 only
NO_TALK_BAR = {
    "min_score": 50.0,
    "min_setup_s": 0.0,
    "talk_min_words": 0,
    "talk_min_rate": 0.0,
    "talk_laugh": 0.0,
    "loop_min_words": 10**9,
    "min_distinct": 0.0,
}
_HUD_SIGNALS = ("hud", "hud_reason", "face_motion", "face_seen")


def _mmss(t: float) -> str:
    t = int(t)
    return f"{t // 60:02d}:{t % 60:02d}"


def split_of(doc: dict) -> str:
    return doc.get("split") or "tune"  # eval_stream_a/b were the tuning set of 5.2


def bar_text(c: dict) -> str:
    """The text the quality bar reads. v53 fixtures keep <= 12 neutral tokens plus the token counts
    ({"n", "distinct"}); the rest is rebuilt from codes so word count, distinct share and milestone lines match."""
    tok = c.get("tokens")
    if not tok:
        return c["text"]
    base = c["text"].split()
    have = {scoring.norm_token(t) for t in base}
    codes = [f"w{i}" for i in range(1, max(0, tok["distinct"] - len(have)) + 1)]
    out = base + codes
    out += [out[0] if out else "w0"] * (tok["n"] - len(out))
    return " ".join(out)


def _emotion(comp: dict, signals: dict, face_w: float) -> dict:
    """comp with the face part added to emotion at weight face_w (scoring.score_parts' parts: 1.0, +0.25 with a
    transcript; the stored emotion was computed with the face weight 0)."""
    fm = signals.get("face_motion")
    if not face_w or fm is None or comp.get("emotion") is None or signals.get("content") == "talk":
        return comp
    w = 1.25 if comp.get("hook") is not None else 1.0
    return dict(comp, emotion=(comp["emotion"] * w + face_w * float(fm)) / (w + face_w))


def evaluate(
    doc: dict,
    bar: dict = None,
    talk_bar: dict = None,
    weights: dict = None,
    face_w: float = None,
    before: bool = False,
) -> dict:
    """Selection on one fixture. bar/talk_bar default to the shipped DEFAULT_BAR/TALK_BAR, weights to
    SCORE_WEIGHTS, face_w to scoring.FACE_MOTION_WEIGHT; before=True uses the 5.2.1 values and drops the hud
    component, the face signal and HUD-only candidates."""
    talk = (doc.get("content") or "gaming") == "talk"
    if before:
        bar, talk_bar, weights, face_w = BEFORE_BAR, BEFORE_TALK_BAR, BEFORE_WEIGHTS, 0.0
    bar = dict(DEFAULT_BAR if bar is None else bar)
    talk_bar = dict(TALK_BAR, **(talk_bar or {}))
    talk_bar["talk"] = True
    face_w = scoring.FACE_MOTION_WEIGHT if face_w is None else face_w
    w_all = dict(scoring.SCORE_WEIGHTS, **(weights or {}))
    cands = []
    with mock.patch.dict(scoring.SCORE_WEIGHTS, w_all, clear=True):
        for c in doc["candidates"]:
            if before and (c.get("sources") or []) == ["hud"]:
                continue
            comp, sig = dict(c["comp"]), dict(c["signals"])
            if before:
                comp.pop("hud", None)
                for k in _HUD_SIGNALS:
                    sig.pop(k, None)
            comp = _emotion(comp, sig, face_w)
            c = dict(c, comp=comp, signals=sig, text=bar_text(c), score=combine(comp, sig))
            ok, why = passes_bar(c, talk_bar if talk else bar)
            cands.append(dict(c, ok=ok, why=why))
    picked = select_clips([c for c in cands if c["ok"]], 0, 0)
    ids = {id(c) for c in picked}
    good = [c for c in picked if c["label"]]
    big = [c for c in cands if c["big"]]
    worthy = [c for c in cands if c["label"]]
    return {
        "candidates": len(cands),
        "selected": len(picked),
        "good": len(good),
        "big": len(big),
        "big_hit": sum(1 for c in big if id(c) in ids),
        "worthy": len(worthy),
        "precision": len(good) / len(picked) if picked else 1.0,
        "big_recall": sum(1 for c in big if id(c) in ids) / len(big) if big else 1.0,
        "recall": len(good) / len(worthy) if worthy else 1.0,
        "picked": picked,
        "missed_big": [c for c in big if id(c) not in ids],
        "rows": cands,
    }


def pooled(results: list) -> dict:
    """Totals over several fixtures: precision = good / selected, recalls over all big / post-worthy moments."""
    t = {k: sum(r[k] for r in results) for k in ("candidates", "selected", "good", "big", "big_hit", "worthy")}
    t["precision"] = t["good"] / t["selected"] if t["selected"] else 1.0
    t["big_recall"] = t["big_hit"] / t["big"] if t["big"] else 1.0
    t["recall"] = t["good"] / t["worthy"] if t["worthy"] else 1.0
    t["streams"] = len(results)
    return t


def load(split: str = "all") -> list:
    """[(name, doc)] of the fixtures in one split (tune | holdout | all), sorted by name."""
    out = []
    for f in sorted(FIXTURES.glob("*.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        if split == "all" or split_of(doc) == split:
            out.append((f.stem, doc))
    return out


def _read(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else {}


def _line(name: str, r: dict) -> str:
    return (
        f"{name:<20} candidates {r['candidates']:>3}  selected {r['selected']:>3}  precision {r['precision']:.2f}  "
        f"big recall {r['big_recall']:.2f} ({r['big_hit']}/{r['big']})  post-worthy recall {r['recall']:.2f}"
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--split", choices=SPLITS, default="all", help="which fixtures (default all)")
    ap.add_argument("--before", action="store_true", help="5.2.1 defaults without the hud and face signals")
    ap.add_argument(
        "--before-llm", action="store_true", help="without the strong-LLM exemption from the payoff loudness floor"
    )
    ap.add_argument("--no-bar", action="store_true", help="v2 behaviour (score >= 50, no quality bar)")
    ap.add_argument("--bar", default="", help="JSON with bar overrides")
    ap.add_argument("--weights", default="", help="JSON with SCORE_WEIGHTS overrides and face_motion")
    ap.add_argument("--verbose", action="store_true", help="list the picked and missed windows of each fixture")
    args = ap.parse_args(argv)
    bars, weights = _read(args.bar), _read(args.weights)
    if bars and "bar" not in bars and "talk_bar" not in bars:
        bars = {"bar": bars}
    if args.before_llm:
        bars = {
            **bars,
            "bar": dict(bars.get("bar") or {}, llm_story_min=1000.0, llm_story_min_soft=1000.0),
        }
    face_w = weights.pop("face_motion", None)
    if args.no_bar:
        mode = "no bar (score >= 50 only)"
    elif args.before:
        mode = "before (5.2.1 defaults, no hud/face signals, no HUD-only candidates)"
    elif args.before_llm:
        mode = "shipped defaults without the strong-LLM loudness exemption"
    else:
        mode = "shipped defaults" + (" + overrides" if bars or weights or face_w is not None else "")
    print(f"bar: {mode}")
    per_split = {}
    for name, doc in load(args.split):
        if args.no_bar:
            r = evaluate(doc, NO_BAR, talk_bar=NO_TALK_BAR)
        else:
            r = evaluate(
                doc,
                dict(DEFAULT_BAR, **bars.get("bar", {})),
                bars.get("talk_bar"),
                weights,
                face_w,
                before=args.before,
            )
        per_split.setdefault(split_of(doc), []).append(r)
        print(f"[{split_of(doc):<7}] " + _line(name, r))
        if args.verbose:
            for c in sorted(r["picked"], key=lambda c: c["start"]):
                mark = "ok " if c["label"] else "BAD"
                print(f"    {mark} {_mmss(c['start'])} score {c['score']:>3}  {c['why']:<28} {c['reason']}")
            for c in r["missed_big"]:
                print(f"    MISSED BIG {_mmss(c['start'])} score {c['score']}: {c['why']} ({c['reason']})")
    print("summary (pooled over the streams of each split):")
    for sp in ("tune", "holdout"):
        if sp in per_split:
            t = pooled(per_split[sp])
            print(f"  {sp:<8} {t['streams']} streams  " + _line("", t).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
