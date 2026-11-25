import json

import pytest

from streamclipper.hud.profiles import list_profiles, load_profile, match_title, validate
from streamclipper.util import ToolError


def test_all_shipped_profiles_load_and_gta6_is_experimental():
    assert list_profiles() == ["generic", "gta5", "gta6"]
    profs = {n: load_profile(n) for n in list_profiles()}
    assert profs["gta6"]["experimental"] and not profs["gta5"]["experimental"]
    ids = {e["id"] for e in profs["gta5"]["events"]}
    assert {"wasted", "busted", "mission_passed", "mission_failed", "wanted_3plus"} <= ids
    assert {e["id"] for e in profs["generic"]["events"]} == {"big_text", "flash", "kill_feed_burst"}
    wasted = next(e for e in profs["gta5"]["events"] if e["id"] == "wasted")
    assert wasted["words"] == ["WASTED"] and wasted["strength"] == 1.0 and wasted["method"] == "desat_ocr"
    for p in profs.values():
        for e in p["events"]:
            x0, y0, x1, y1 = e["region"]
            assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1


def _write(tmp_path, doc, name="p.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc) if not isinstance(doc, str) else doc, encoding="utf-8")
    return p


GOOD = {
    "name": "mine",
    "events": [{"id": "ko", "strength": 0.8, "method": "ocr", "region": [0.2, 0.3, 0.8, 0.6], "words": ["K.O."]}],
}


def test_user_profile_file_loads_with_defaults(tmp_path):
    prof = load_profile(_write(tmp_path, GOOD))
    ev = prof["events"][0]
    assert prof["name"] == "mine" and not prof["experimental"]
    assert ev["words"] == ["K.O."] and ev["colour"] == "any" and ev["cooldown_s"] == 20.0 and ev["label"] == "ko"
    assert ev["reason"] == "ko on screen"


@pytest.mark.parametrize(
    "bad, msg",
    [
        ({"name": "x", "events": []}, "non-empty list"),
        ({"name": "x", "events": [dict(GOOD["events"][0], method="magic")]}, "'method'"),
        ({"name": "x", "events": [dict(GOOD["events"][0], region=[0.5, 0.2, 0.4, 0.9])]}, "'region'"),
        ({"name": "x", "events": [dict(GOOD["events"][0], strength=2)]}, "outside"),
        ({"name": "x", "events": [dict(GOOD["events"][0], words=[])]}, "needs 'words'"),
        ({"name": "x", "events": [dict(GOOD["events"][0], extra=1)]}, "unknown key"),
        ({"name": "x", "events": [GOOD["events"][0], GOOD["events"][0]]}, "used twice"),
        ({"events": GOOD["events"]}, "'name'"),
    ],
)
def test_malformed_profile_gives_a_tool_error_with_a_hint(tmp_path, bad, msg):
    with pytest.raises(ToolError) as e:
        load_profile(_write(tmp_path, bad))
    assert msg in str(e.value)
    assert "gta5.json" in e.value.hint


def test_bad_json_unknown_name_and_missing_file(tmp_path):
    with pytest.raises(ToolError, match="not valid JSON"):
        load_profile(_write(tmp_path, "{nope"))
    with pytest.raises(ToolError, match="Unknown HUD profile") as e:
        load_profile("gta7")
    assert "gta5" in e.value.hint
    with pytest.raises(ToolError, match="not found"):
        load_profile(tmp_path / "missing.json")
    with pytest.raises(ToolError):
        validate([1, 2])


def test_title_keywords_pick_gta6_before_gta5():
    shipped = [load_profile("gta6"), load_profile("gta5")]
    assert match_title("GTA 6 first look - Leonida", shipped)["name"] == "gta6"
    assert match_title("GTA V Tank Rampage", shipped)["name"] == "gta5"
    assert match_title("gta_5_thug_life", shipped)["name"] == "gta5"
    assert match_title("Grand Theft Auto VI trailer", shipped)["name"] == "gta6"
    assert match_title("Minecraft hardcore", shipped) is None
