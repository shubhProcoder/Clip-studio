from pathlib import Path

from streamclipper.chat import parse_youtube_live_chat, youtube_chat_line
from streamclipper.live.chat import LiveChat, parse_irc_line, parse_irc_line_full
from streamclipper.live.ingest import channel_name, resolve_source, twitch_channel

FIX = Path(__file__).parent / "fixtures"


def test_irc_privmsg_with_and_without_tags():
    tagged = (
        "@badge-info=;color=#FF0000;display-name=Viewer;emotes= :viewer!viewer@viewer.tmi.twitch.tv "
        "PRIVMSG #somechannel :KEKW no way he did that\r\n"
    )
    assert parse_irc_line(tagged) == ("msg", "KEKW no way he did that")
    assert parse_irc_line(":a!a@a.tmi.twitch.tv PRIVMSG #chan :W") == ("msg", "W")
    assert parse_irc_line(":a!a@a.tmi.twitch.tv PRIVMSG #chan :with : colon") == ("msg", "with : colon")


def test_irc_ping_and_other_lines():
    assert parse_irc_line("PING :tmi.twitch.tv") == ("ping", ":tmi.twitch.tv")
    assert parse_irc_line(":tmi.twitch.tv 001 justinfan12345 :Welcome, GLHF!") == (None, None)
    assert parse_irc_line(":justinfan1!justinfan1@justinfan1.tmi.twitch.tv JOIN #chan") == (None, None)
    assert parse_irc_line("") == (None, None)


def test_irc_sender_identity_is_normalized():
    line = (
        "@display-name=CoolViewer :coolviewer!id@id.tmi.twitch.tv "
        "PRIVMSG #chan :KEKW"
    )
    assert parse_irc_line_full(line) == ("msg", "KEKW", "coolviewer")


def test_youtube_line_parser_matches_file_parser():
    fx = FIX / "chat_youtube.jsonl"
    lines = fx.read_text(encoding="utf-8-sig").splitlines()
    per_line = []
    for line in lines:
        t, texts = youtube_chat_line(line)
        if t is not None:
            per_line.extend((t, x) for x in texts)
    assert per_line and sorted(per_line, key=lambda m: m[0]) == parse_youtube_live_chat(fx)
    assert youtube_chat_line("not json") == (None, [])
    assert youtube_chat_line("") == (None, [])


def test_live_chat_stamps_messages_with_stream_time(tmp_path):
    chat = LiveChat("twitch", "https://twitch.tv/x", lambda: 42.0, tmp_path)
    chat.add("W")
    chat.add("LUL", t=10.0)
    assert chat.messages() == [(42.0, "W"), (10.0, "LUL")] and chat.count == 2


def test_kick_chat_is_skipped_once(tmp_path, capsys):
    chat = LiveChat("kick", "https://kick.com/x", lambda: 0.0, tmp_path)
    chat.start()
    assert "Kick chat is not supported yet" in capsys.readouterr().out
    chat.stop()


def test_source_kinds(tmp_path):
    f = tmp_path / "local.mp4"
    f.write_bytes(b"x")
    assert resolve_source(str(f)) == "file"
    assert resolve_source("https://www.twitch.tv/somechannel") == "twitch"
    assert resolve_source("https://www.youtube.com/watch?v=abc123") == "youtube"
    assert resolve_source("https://youtu.be/abc123") == "youtube"
    assert resolve_source("https://kick.com/someone") == "kick"
    assert resolve_source("https://cdn.example.com/live/index.m3u8") == "hls"
    assert twitch_channel("https://www.twitch.tv/SomeChannel") == "somechannel"
    assert channel_name("https://www.youtube.com/watch?v=abc123") == "abc123"
    assert channel_name("https://www.youtube.com/@news/live") == "news"
    assert channel_name(str(f)) == "local"
