from pathlib import Path

from streamclipper import env


def _mac_home(monkeypatch, tmp_path):
    monkeypatch.setattr(env.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CAPCUT_DRAFT_ROOT", raising=False)


def test_mac_capcut_under_movies(monkeypatch, tmp_path):
    _mac_home(monkeypatch, tmp_path)
    drafts = tmp_path / "Movies/CapCut/User Data/Projects/com.lveditor.draft"
    drafts.mkdir(parents=True)
    assert env.find_capcut_local() == tmp_path / "Movies/CapCut"
    assert env.find_draft_root() == drafts


def test_mac_capcut_falls_back_to_application_support(monkeypatch, tmp_path):
    _mac_home(monkeypatch, tmp_path)
    assert env.find_capcut_local() == tmp_path / "Library/Application Support/CapCut"
