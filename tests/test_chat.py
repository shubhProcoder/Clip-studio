import json
from pathlib import Path

import pytest

from streamclipper.chat import (
    chat_signal,
    hype_count,
    load_chat,
    normalize_username,
    parse_jsonl,
    parse_twitch_rechat,
    parse_youtube_live_chat,
    spikes,
    window_score,
)
from streamclipper.util import ToolError

FIX = Path(__file__).parent / "fixtures"


def test_youtube_live_chat_parser_reads_text_and_emoji():
    msgs = parse_youtube_live_chat(FIX / "chat_youtube.jsonl")
    assert len(msgs) == 60
    assert msgs == sorted(msgs, key=lambda m: m[0])
    texts = [t for _, t in msgs]
    assert "😂" in texts  # standard emoji run -> the emoji itself
    assert "KEKW" in texts  # custom channel emote -> its shortcut name


def test_spike_found_on_each_fixture_format():
    for name, burst in (("chat_youtube.jsonl", 90.0), ("chat_twitch_rechat.json", 60.0), ("chat_simple.jsonl", 40.0)):
        msgs = load_chat(FIX / name)
        sig = chat_signal(msgs, 150.0)
        found = spikes(sig)
        assert len(found) == 1, name
        assert burst <= found[0]["t"] <= burst + 6.0
        assert found[0]["hype"] > 0.5


def test_window_score_matches_payoff_before_the_spike():
    sig = chat_signal(parse_youtube_live_chat(FIX / "chat_youtube.jsonl"), 150.0)
    near = window_score(sig, 84.0)  # chat spike ~8 s after the payoff
    calm = window_score(sig, 30.0)
    assert near["score"] > 0.8 and near["ratio"] > 5
    assert calm["score"] == 0.0


def test_twitch_gql_layout_and_own_jsonl(tmp_path):
    gql = [{"node": {"contentOffsetSeconds": 12, "message": {"fragments": [{"text": "clip "}, {"text": "it"}]}}}]
    p = tmp_path / "gql.json"
    p.write_text(json.dumps({"data": {"video": {"comments": {"edges": gql}}}}), encoding="utf-8")
    assert parse_twitch_rechat(p) == [(12.0, "clip it")]
    assert load_chat(p) == [(12.0, "clip it")]
    j = tmp_path / "own.jsonl"
    j.write_text('{"t": 3.5, "text": "W"}\nnot json\n{"t": 1, "text": "hi"}\n', encoding="utf-8")
    assert parse_jsonl(j) == [(1.0, "hi"), (3.5, "W")]


def test_empty_chat_file_is_a_clear_error(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("\n", encoding="utf-8")
    with pytest.raises(ToolError):
        load_chat(p)


def test_hype_words_are_whole_tokens():
    assert hype_count("W") == 1
    assert hype_count("KEKW KEKW") == 1
    assert hype_count("clip it!!") >= 1
    assert hype_count("😂😂") == 1
    assert hype_count("Wow, what a car") == 0


def test_unique_viewers_strengthen_hype_more_than_one_spammer():
    diverse = [(40.0 + i * 0.01, "W", f"viewer{i}") for i in range(20)]
    spam = [(40.0 + i * 0.01, "W", "bot") for i in range(20)]
    diverse_sig = chat_signal(diverse, 150.0)
    spam_sig = chat_signal(spam, 150.0)
    diverse_score = window_score(diverse_sig, 40.0)
    spam_score = window_score(spam_sig, 40.0)
    assert diverse_sig["identified"]
    assert diverse_score["unique_ratio"] > 5.0
    assert diverse_score["score"] > spam_score["score"]
    assert spam_score["spam"] > 0.8


def test_missing_usernames_fall_back_to_message_logic():
    sig = chat_signal([(40.0, "W"), (40.2, "KEKW"), (40.4, "NO WAY")], 150.0)
    assert not sig["identified"]
    assert window_score(sig, 40.0)["score"] >= 0.0


def test_jsonl_identity_fields_are_normalized():
    assert normalize_username("  @Viewer One ") == "viewer_one"
