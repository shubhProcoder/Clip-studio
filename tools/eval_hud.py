"""
HUD event eval on the labelled fixtures in tests/fixtures/hud/*.json: precision and recall per event type with a
2 s match window (one detection per label), for the calibration and the test videos separately.

  python tools/eval_hud.py                 # shipped gta5 profile (the fixtures' detections)
  python tools/eval_hud.py --with-dropped  # also count the detections of events dropped from the profile

Fixtures: {name, split: cal|test, duration, events: [{t, event}], detections: [{t, event, strength}]}; they hold
only times and event ids (no media, URLs or titles). The labels come from loose-threshold proposals checked on
frame strips plus a 0.2 fps skim of each video.
"""

import argparse
import json
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "hud"
WINDOW = 2.0


def match(labels: list, dets: list, window: float = WINDOW) -> int:
    """True positives: detections paired one-to-one with a label of the same event within +-window s."""
    used, tp = set(), 0
    for d in sorted(dets, key=lambda x: x["t"]):
        best = None
        for i, lab in enumerate(labels):
            if i in used or lab["event"] != d["event"] or abs(lab["t"] - d["t"]) > window:
                continue
            if best is None or abs(lab["t"] - d["t"]) < abs(labels[best]["t"] - d["t"]):
                best = i
        if best is not None:
            used.add(best)
            tp += 1
    return tp


def evaluate(docs: list, with_dropped: bool = False) -> dict:
    """{split: {event: {labels, detections, tp, precision, recall}, '_all': {...}}}."""
    out = {}
    for doc in docs:
        dets = list(doc["detections"]) + (list(doc.get("dropped_detections") or []) if with_dropped else [])
        split = out.setdefault(doc["split"], {})
        kinds = {x["event"] for x in doc["events"]} | {x["event"] for x in dets}
        for k in sorted(kinds) + ["_all"]:
            lab = [x for x in doc["events"] if k in ("_all", x["event"])]
            det = [x for x in dets if k in ("_all", x["event"])]
            row = split.setdefault(k, {"labels": 0, "detections": 0, "tp": 0})
            row["labels"] += len(lab)
            row["detections"] += len(det)
            row["tp"] += match(lab, det) if k != "_all" else 0
        split["_all"]["tp"] = sum(r["tp"] for kk, r in split.items() if kk != "_all")
    for split in out.values():
        for row in split.values():
            row["precision"] = row["tp"] / row["detections"] if row["detections"] else None
            row["recall"] = row["tp"] / row["labels"] if row["labels"] else None
    return out


def load(folder: Path = FIXTURES) -> list:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(folder).glob("*.json"))]


def _fmt(v) -> str:
    return "  -  " if v is None else f"{v:.2f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--with-dropped", action="store_true", help="count detections of events dropped from the profile")
    args = ap.parse_args(argv)
    docs = load()
    res = evaluate(docs, args.with_dropped)
    for split in ("cal", "test"):
        names = ", ".join(f"{d['name']} ({d['duration'] / 60:.0f} min)" for d in docs if d["split"] == split)
        print(f"{split}: {names}")
        print(f"  {'EVENT':<16}{'LABELS':>7}{'DETECTED':>9}{'TP':>5}{'PRECISION':>11}{'RECALL':>8}")
        rows = res.get(split, {})
        for k in sorted(rows, key=lambda k: (k == "_all", k)):
            r = rows[k]
            name = "overall" if k == "_all" else k
            print(
                f"  {name:<16}{r['labels']:>7}{r['detections']:>9}{r['tp']:>5}"
                f"{_fmt(r['precision']):>11}{_fmt(r['recall']):>8}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
