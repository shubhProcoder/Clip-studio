import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "hud"
spec = importlib.util.spec_from_file_location("eval_hud", ROOT / "tools" / "eval_hud.py")
eval_hud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_hud)

KEYS = {"name", "split", "duration", "profile", "events", "detections", "dropped_detections"}


def test_fixtures_hold_no_media_urls_or_titles():
    files = sorted(FIX.glob("*.json"))
    assert len(files) >= 4
    assert not [p for p in FIX.iterdir() if p.suffix != ".json"]
    for p in files:
        raw = p.read_text(encoding="utf-8")
        doc = json.loads(raw)
        assert set(doc) <= KEYS, p.name
        assert doc["split"] in ("cal", "test") and doc["name"].startswith("hud_gta_")
        assert "http" not in raw and "youtube" not in raw.lower() and "www." not in raw
        for x in doc["events"] + doc["detections"] + doc.get("dropped_detections", []):
            assert set(x) <= {"t", "event", "strength"} and isinstance(x["t"], (int, float))
    splits = [json.loads(p.read_text(encoding="utf-8"))["split"] for p in files]
    assert splits.count("cal") >= 2 and splits.count("test") >= 2


def test_matching_is_one_to_one_within_2_s():
    labels = [{"t": 10.0, "event": "wasted"}, {"t": 30.0, "event": "wasted"}]
    dets = [{"t": 11.5, "event": "wasted"}, {"t": 12.0, "event": "wasted"}, {"t": 30.0, "event": "busted"}]
    assert eval_hud.match(labels, dets) == 1


def test_precision_on_the_test_videos():
    res = eval_hud.evaluate(eval_hud.load())
    test = res["test"]["_all"]
    assert test["precision"] >= 0.7 and test["labels"] >= 10
    assert res["cal"]["_all"]["labels"] >= 2
    assert eval_hud.main([]) == 0
