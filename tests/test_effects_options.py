from argparse import Namespace
from pathlib import Path

import pytest

from streamclipper.cli import build_parser
from streamclipper.clipplan import with_effects
from streamclipper.colab import _clip_argv
from streamclipper.dashboard import jobs
from streamclipper.effects import EFFECT_DEFAULTS, any_on, check_effects
from streamclipper.options import resolve_options
from streamclipper.util import ToolError

KIT = Path(__file__).resolve().parents[1] / "docs" / "examples" / "brand_kit.json"


@pytest.mark.parametrize(
    "cmd", [["clip-stream", "--out", "w"], ["render", "--spec", "x.json"], ["build", "--spec", "x"]]
)
def test_flags_default_to_off(cmd):
    args = build_parser().parse_args(cmd)
    opts = resolve_options(args, {})
    assert opts["effects"] == EFFECT_DEFAULTS and opts["brand"] is None and not any_on(opts["effects"])


def test_flags_and_spec_keys():
    args = build_parser().parse_args(
        ["clip-stream", "--out", "w", "--zoom", "strong", "--remove-fillers", "--enhance-speech", "light"]
        + ["--brand-kit", str(KIT)]
    )
    opts = resolve_options(args, {})
    eff = opts["effects"]
    assert eff["zoom"] == "strong" and eff["remove_fillers"] and eff["enhance_speech"] == "light"
    assert eff["brand_kit"] == str(KIT.resolve()) and len(eff["brand_sha1"]) == 40
    assert opts["highlight_color"] == "yellow"  # from the kit's colours
    spec_opts = resolve_options(Namespace(), {"zoom": "light", "remove_fillers": True})
    assert spec_opts["effects"]["zoom"] == "light" and spec_opts["effects"]["remove_fillers"]
    with pytest.raises(ToolError):
        resolve_options(Namespace(), {"zoom": "huge"})
    with pytest.raises(SystemExit):
        build_parser().parse_args(["clip-stream", "--out", "w", "--zoom", "huge"])


def test_render_spec_overrides_the_plan_effects():
    plan = {"name": "c", "effects": {"zoom": "light"}, "zoom_plan": {"level": "light"}}
    args = build_parser().parse_args(["render", "--spec", "c.clip.json", "--zoom", "strong"])
    out = with_effects(plan, args)
    assert out["effects"]["zoom"] == "strong" and "zoom_plan" not in out and plan["effects"]["zoom"] == "light"
    old = {"name": "old"}  # a 5.2 plan without effects stays the same object
    assert with_effects(old, build_parser().parse_args(["render", "--spec", "x"])) is old
    assert check_effects({}) == EFFECT_DEFAULTS


def test_colab_argv_only_adds_non_default_effects(tmp_path):
    src = tmp_path / "s.mp4"
    src.write_bytes(b"x")
    base = {
        "prefix": "clip",
        "max_clips": 3,
        "min_score": 50,
        "llm": "none",
        "whisper_model": "auto",
        "device": "auto",
        "language": "auto",
        "render": True,
        "drafts": False,
        "draft_root": tmp_path,
        "resume": True,
    }
    plain = _clip_argv(str(src), tmp_path / "o", base)
    assert not any(a in plain for a in ("--zoom", "--remove-fillers", "--enhance-speech", "--brand-kit", "--hud"))
    o = dict(base, zoom="medium", remove_fillers=True, enhance_speech="strong", brand_kit=str(KIT), hud="gta5")
    argv = _clip_argv(str(src), tmp_path / "o", o)
    for flag in (["--zoom", "medium"], ["--enhance-speech", "strong"], ["--brand-kit", str(KIT)], ["--hud", "gta5"]):
        i = argv.index(flag[0])
        assert argv[i + 1] == flag[1]
    assert "--remove-fillers" in argv
    build_parser().parse_args(argv)  # clip-stream accepts all of them


def test_run_pipeline_rejects_a_missing_brand_kit(tmp_path):
    from streamclipper.colab import run_pipeline

    with pytest.raises(ToolError, match="brand kit"):
        run_pipeline(str(tmp_path / "s.mp4"), out_dir=str(tmp_path / "o"), brand_kit=str(tmp_path / "nope.json"))


def test_dashboard_options_and_argv(tmp_path):
    o = jobs.validate_options({"zoom": "light", "remove_fillers": "true", "enhance_speech": "light", "brand_kit": 1})
    assert o["zoom"] == "light" and o["remove_fillers"] is True and o["brand_kit"] is True and o["hud"] == "auto"
    d = jobs.validate_options({})
    assert (d["zoom"], d["remove_fillers"], d["enhance_speech"], d["brand_kit"]) == ("off", False, "off", False)
    for bad in ({"zoom": "max"}, {"enhance_speech": "loud"}, {"remove_fillers": "maybe"}, {"hud": "gta9"}):
        with pytest.raises(ToolError):
            jobs.validate_options(bad)
    argv = jobs.build_vod_argv("https://example.com/v", o, tmp_path / "job", KIT)
    assert argv[argv.index("--zoom") + 1] == "light" and argv[argv.index("--brand-kit") + 1] == str(KIT)
    assert "--remove-fillers" in argv
    off = jobs.build_vod_argv("https://example.com/v", d, tmp_path / "job", KIT)
    assert "--brand-kit" not in off and "--zoom" not in off
