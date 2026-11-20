import json
import zipfile

import pytest

from streamclipper.draft import importer, rootmeta
from streamclipper.draft.writer import CAPCUT_LOCAL_TOKEN, FONTS_TOKEN, PORTABLE_MANIFEST, fonts_dir_fwd
from streamclipper.util import ToolError, fwd

COLAB_DIR = "/content/drive/MyDrive/StreamClipper/capcut_drafts/clip_01"


def _fake_draft_zip(tmp_path, name="clip_01", dup=False):
    """A portable draft as Colab writes it: Linux media paths, tokens for CapCut and the fonts."""
    src = tmp_path / "colab" / name
    (src / "media").mkdir(parents=True)
    (src / "media" / "gameplay.mp4").write_bytes(b"video")
    (src / "media" / "pop_click.wav").write_bytes(b"audio")
    text_content = json.dumps({"text": "NO WAY", "styles": [{"font": {"path": f"{FONTS_TOKEN}/Anton-Regular.ttf"}}]})
    dc = {
        "id": "DRAFT-ID-1",
        "name": name,
        "duration": 5_000_000,
        "materials": {
            "videos": [{"id": "v1", "path": f"{COLAB_DIR}/media/gameplay.mp4"}]
            + ([{"id": "v2", "path": f"{COLAB_DIR}/media/gameplay.mp4"}] if dup else []),
            "audios": [{"id": "a1", "path": f"{COLAB_DIR}/media/pop_click.wav"}],
            "texts": [{"id": "t1", "font_path": f"{FONTS_TOKEN}/Anton-Regular.ttf", "content": text_content}],
            "effects": [{"id": "e1", "path": f"{CAPCUT_LOCAL_TOKEN}/User Data/Cache/effect/1/abc"}],
        },
        "tracks": [],
    }
    (src / "draft_content.json").write_text(json.dumps(dc), encoding="utf-8")
    (src / "draft_meta_info.json").write_text(
        json.dumps({"draft_id": "DRAFT-ID-1", "draft_name": name, "draft_fold_path": COLAB_DIR}), encoding="utf-8"
    )
    manifest = {"media": {f"{COLAB_DIR}/media/gameplay.mp4": "gameplay.mp4"}, "capcut_local_token": CAPCUT_LOCAL_TOKEN}
    (src / PORTABLE_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    zp = tmp_path / "capcut_drafts.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for f in src.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(src.parent).as_posix())
    return zp


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "drafts"
    r.mkdir()
    (r / "clip_01").mkdir()  # an existing draft with the same name -> _2
    (r / "root_meta_info.json").write_text(
        json.dumps({"all_draft_store": [{"draft_id": "OTHER", "draft_name": "clip_01"}], "draft_ids": 1}),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(importer, "guard_capcut", lambda dr, close=False: calls.append(dr))
    monkeypatch.setattr(importer, "find_capcut_local", lambda: tmp_path / "CapCutLocal")
    monkeypatch.setattr(rootmeta, "_ROOT_META_BACKED_UP", set())
    return r, calls


def test_import_relinks_renames_and_registers(tmp_path, root):
    root, guard_calls = root
    zp = _fake_draft_zip(tmp_path)
    res = importer.import_draft_zip(zp, str(root))
    assert [r["name"] for r in res] == ["clip_01_2"]
    assert guard_calls == [root.resolve()]

    dst = root / "clip_01_2"
    dc = json.loads((dst / "draft_content.json").read_text(encoding="utf-8"))
    vpath = dc["materials"]["videos"][0]["path"]
    apath = dc["materials"]["audios"][0]["path"]  # not in the manifest: matched by base name in media/
    assert vpath == fwd(dst.resolve() / "media" / "gameplay.mp4")
    assert apath == fwd(dst.resolve() / "media" / "pop_click.wav")
    raw = (dst / "draft_content.json").read_text(encoding="utf-8")
    assert CAPCUT_LOCAL_TOKEN not in raw and FONTS_TOKEN not in raw
    assert fwd(tmp_path / "CapCutLocal") in raw
    assert fonts_dir_fwd() + "/Anton-Regular.ttf" in raw
    assert dc["name"] == "clip_01_2"
    assert (dst / "draft_content.json.bak").read_text(encoding="utf-8") == raw

    dmi = json.loads((dst / "draft_meta_info.json").read_text(encoding="utf-8"))
    assert dmi["draft_name"] == "clip_01_2"
    assert dmi["draft_fold_path"] == fwd(dst.resolve())

    rm = json.loads((root / "root_meta_info.json").read_text(encoding="utf-8"))
    names = [s["draft_name"] for s in rm["all_draft_store"]]
    assert names == ["clip_01_2", "clip_01"]
    assert rm["draft_ids"] == 2
    assert (root / "root_meta_info.json.streamclipper_first.bak").is_file()
    assert res[0]["missing"] == [] and res[0]["relinked"] == 2
    assert not list(root.parent.glob(".streamclipper_import_*"))  # temp folder removed


def test_relinked_counts_unique_files(tmp_path, root):
    root, _ = root
    res = importer.import_draft_zip(_fake_draft_zip(tmp_path, dup=True), str(root))
    dc = json.loads((root / res[0]["name"] / "draft_content.json").read_text(encoding="utf-8"))
    assert len(dc["materials"]["videos"]) == 2  # two video materials, one file
    assert res[0]["relinked"] == 2  # gameplay.mp4 + pop_click.wav


def test_import_twice_gets_new_id(tmp_path, root):
    root, _ = root
    zp = _fake_draft_zip(tmp_path)
    a = importer.import_draft_zip(zp, str(root))
    b = importer.import_draft_zip(zp, str(root))
    assert [a[0]["name"], b[0]["name"]] == ["clip_01_2", "clip_01_3"]
    assert a[0]["draft_id"] == "DRAFT-ID-1" and b[0]["draft_id"] != "DRAFT-ID-1"


def test_overwrite_moves_old_draft_aside(tmp_path, root):
    root, _ = root
    (root / "clip_01" / "keep.txt").write_text("old", encoding="utf-8")
    res = importer.import_draft_zip(_fake_draft_zip(tmp_path), str(root), overwrite=True)
    assert res[0]["name"] == "clip_01"
    moved = list((root.parent / importer.REPLACED_DIR).glob("clip_01_*/keep.txt"))
    assert len(moved) == 1 and moved[0].read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize("bad", ["../evil.txt", "/etc/evil.txt", "C:/evil.txt", "a/../../evil.txt"])
def test_zip_slip_is_refused(tmp_path, root, bad):
    root, _ = root
    zp = tmp_path / "bad.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("clip_01/draft_content.json", "{}")
        zf.writestr(bad, "x")
    with pytest.raises(ToolError, match="Unsafe path"):
        importer.import_draft_zip(zp, str(root))
    assert not (tmp_path / "evil.txt").exists()
