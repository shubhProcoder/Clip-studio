from streamclipper.cli import build_parser
from streamclipper.copywriter import make_hashtags
from streamclipper.discovery import _discover_settings
from streamclipper.games import (
    contextual_hook,
    detect_game,
    merge_tags,
    prompt_context,
    resolve_game,
    tags_for,
)


def test_merge_tags_puts_the_canonical_tag_first_and_dedupes_case_insensitively():
    got = merge_tags(["#GTA6", "#Gaming"], ["#gta6", "#funny", "#SHORTS"])
    assert got == ["#GTA6", "#Gaming", "#funny", "#SHORTS"]
    assert merge_tags([], ["#gta6", "GTA6"]) == ["#gta6"]  # no canonical spelling: the first one wins
    assert merge_tags(["#GTA6"], ["#x"], limit=1) == ["#GTA6"]


def test_gta_vi_is_detected_and_is_first_priority():
    profile = detect_game("LIVE GTA VI | Vice City chaos", manual="")
    assert profile["name"] == "Grand Theft Auto VI"
    assert "GTA6" in profile["tags"]
    assert "police chases" in profile["taxonomy"]
    assert "NPC interactions" in profile["taxonomy"]
    assert "mission failure" in profile["taxonomy"]


def test_common_aliases_resolve():
    assert resolve_game("gta 6")["name"] == "Grand Theft Auto VI"
    assert resolve_game("GTA VI")["name"] == "Grand Theft Auto VI"
    assert resolve_game("gtav")["name"] == "Grand Theft Auto V"
    assert resolve_game("fortnite")["name"] == "Fortnite"


def test_manual_game_selection_overrides_title_detection():
    profile = detect_game("GTA VI stream", manual="Minecraft")
    assert profile["name"] == "Minecraft"
    assert "Minecraft" in profile["tags"]


def test_unknown_game_uses_generic_profile():
    profile = detect_game("New unknown game championship")
    assert profile["name"] == "Generic Gaming"
    assert "Gaming" in profile["tags"]


def test_game_tags_are_relevant_limited_and_not_trending_claimed():
    tags = tags_for(resolve_game("GTA VI"), limit=6)
    assert len(tags) <= 6
    assert "#GTA6" in tags
    assert all(tag.startswith("#") for tag in tags)
    assert not any("trending" in tag.lower() for tag in tags)


def test_contextual_hook_is_short_and_all_caps():
    profile = resolve_game("GTA VI")
    hook = contextual_hook(profile, "clutch", seed=0.2)
    words = hook.split()
    assert 2 <= len(words) <= 5
    assert hook == hook.upper()


def test_prompt_context_requires_setup_payoff_reaction_and_standalone_context():
    text = prompt_context(resolve_game("GTA VI"))
    assert "setup" in text
    assert "payoff" in text
    assert "reaction" in text
    assert "not watched" in text


def test_manual_game_cli_override_is_preserved():
    args = build_parser().parse_args(["discover-clips", "--out", "out.json", "--game", "GTA VI"])
    st = _discover_settings(args, args.transcribe)
    assert st["game"] == "GTA VI"


def test_game_tags_are_used_in_clip_copy():
    tags = make_hashtags("chaos", ["car"], text="", source_title="", game=resolve_game("GTA VI"))
    assert "#GTA6" in tags
