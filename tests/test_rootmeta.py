import time

from streamclipper.draft import rootmeta
from streamclipper.draft.rootmeta import backup_root_meta, unique_draft_name


def test_unique_draft_name_adds_suffixes(tmp_path):
    assert unique_draft_name(tmp_path, "GTA6_Viral_01") == "GTA6_Viral_01"
    (tmp_path / "GTA6_Viral_01").mkdir()
    assert unique_draft_name(tmp_path, "GTA6_Viral_01") == "GTA6_Viral_01_2"
    (tmp_path / "GTA6_Viral_01_2").mkdir()
    assert unique_draft_name(tmp_path, "GTA6_Viral_01") == "GTA6_Viral_01_3"


def test_backups_first_kept_rolling_capped_foreign_untouched(tmp_path, monkeypatch):
    rm = tmp_path / "root_meta_info.json"
    rm.write_text('{"v": 0}', encoding="utf-8")
    foreign = [tmp_path / "root_meta_info.json.capcut_studio.bak", tmp_path / "root_meta_info.json.my_own.bak"]
    for f in foreign:
        f.write_text("keep me", encoding="utf-8")
    base = time.mktime((2026, 11, 16, 12, 0, 0, 0, 0, -1))
    for run in range(8):
        monkeypatch.setattr(rootmeta, "_ROOT_META_BACKED_UP", set())  # a new run
        rm.write_text(f'{{"v": {run}}}', encoding="utf-8")
        backup_root_meta(rm, now=base + run * 60)
        backup_root_meta(rm, now=base + run * 60 + 1)  # same run: no second backup

    first = tmp_path / "root_meta_info.json.streamclipper_first.bak"
    assert first.read_text(encoding="utf-8") == '{"v": 0}'
    rolling = sorted(p.name for p in tmp_path.glob("root_meta_info.json.streamclipper_2*.bak"))
    assert len(rolling) == 5
    assert rolling[-1] == "root_meta_info.json.streamclipper_20261116-120700.bak"
    assert rolling[0] == "root_meta_info.json.streamclipper_20261116-120300.bak"
    assert (tmp_path / rolling[-1]).read_text(encoding="utf-8") == '{"v": 7}'
    for f in foreign:
        assert f.read_text(encoding="utf-8") == "keep me"


def test_no_backup_without_root_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(rootmeta, "_ROOT_META_BACKED_UP", set())
    backup_root_meta(tmp_path / "root_meta_info.json")
    assert list(tmp_path.iterdir()) == []
