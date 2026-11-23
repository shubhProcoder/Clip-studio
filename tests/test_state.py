import json

from streamclipper.state import RunState, settings_hash

ST = {
    "asr": "full",
    "whisper_model": "auto",
    "device": "auto",
    "min_dur": 15.0,
    "max_dur": 45.0,
    "min_pause": 0.55,
    "db": -48.0,
    "top": 5,
}


def test_resume_keeps_done_chunks(tmp_path):
    p = tmp_path / "state.json"
    s = RunState(p, "file:a", ST)
    assert not s.resumed
    s.mark_chunk(0)
    s.mark_chunk(2)
    s2 = RunState(p, "file:a", dict(ST, top=9))  # top is not part of the resume hash
    assert s2.resumed
    assert s2.chunk_done(0) and s2.chunk_done(2) and not s2.chunk_done(1)
    assert not (tmp_path / "state.json.tmp").exists()
    assert json.loads(p.read_text(encoding="utf-8"))["stages"]["asr_chunks"] == [0, 2]


def test_changed_asr_settings_or_source_invalidate(tmp_path):
    p = tmp_path / "state.json"
    RunState(p, "file:a", ST).mark_chunk(0)
    for st, key in (
        (dict(ST, whisper_model="base"), "file:a"),
        (dict(ST, language="en"), "file:a"),
        (ST, "file:b"),
    ):
        s = RunState(p, key, st)
        assert not s.resumed
        assert not s.chunk_done(0)
    assert settings_hash(ST) != settings_hash(dict(ST, device="cpu"))


def test_chat_llm_and_clip_length_keep_the_transcript(tmp_path):
    p = tmp_path / "state.json"
    RunState(p, "file:a", ST).mark_chunk(0)
    for change in (
        {"chat": "live_chat.json:abc"},
        {"llm": "ollama"},
        {"min_dur": 10.0, "max_dur": 60.0},
        {"min_pause": 0.3, "db": -40.0, "min_score": 0},
    ):
        s = RunState(p, "file:a", dict(ST, **change))
        assert s.resumed and s.chunk_done(0), change


def test_fresh_ignores_state(tmp_path):
    p = tmp_path / "state.json"
    RunState(p, "file:a", ST).mark_chunk(0)
    s = RunState(p, "file:a", ST, fresh=True)
    assert not s.resumed and not s.chunk_done(0)


def test_open_existing_records_rendered(tmp_path):
    p = tmp_path / "state.json"
    assert RunState.open_existing(p) is None
    RunState(p, "file:a", ST).save()
    s = RunState.open_existing(p)
    s.mark_rendered(1, "clip_01_2")
    assert json.loads(p.read_text(encoding="utf-8"))["rendered"] == {"1": "clip_01_2"}


def test_made_clip_is_reused_only_with_the_same_settings_and_files(tmp_path):
    # Colab re-run after a disconnect: finished clips are kept instead of being made again as clip_01_2
    from argparse import Namespace

    from streamclipper.commands import _made_before

    s = RunState(tmp_path / "state.json", "file:a", ST)
    s.mark_rendered(1, "clip_01", key="k1")
    args = Namespace(render=True, no_draft=True, draft_root="")
    assert _made_before(s, 1, "k1", tmp_path, args) is None  # files missing
    (tmp_path / "clip_01.clip.json").write_text(json.dumps({"name": "clip_01"}), encoding="utf-8")
    (tmp_path / "clip_01.mp4").write_bytes(b"x")
    (tmp_path / "clip_01.gate.json").write_text(json.dumps({"pass": True}), encoding="utf-8")
    assert _made_before(s, 1, "k1", tmp_path, args) == ({"name": "clip_01"}, True)
    assert _made_before(s, 1, "other-style", tmp_path, args) is None  # settings changed: make it again
    assert _made_before(s, 2, "k1", tmp_path, args) is None
    again = RunState(tmp_path / "state.json", "file:a", ST)
    assert again.made(1, "k1") == "clip_01" and again.made(1, "") == ""
