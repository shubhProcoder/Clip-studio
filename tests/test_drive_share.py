import csv
import json
import sys
import types

import pytest

from streamclipper import drive_share
from streamclipper.util import ToolError


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeDrive:
    """Records Drive API calls. Folder tree: MyDrive/StreamClipper/clip_01.mp4 exists on 'Drive'."""

    def __init__(self, tree):
        self.tree = tree  # (parent id, name) -> id
        self.calls = []
        self.n = 0

    def files(self):
        return self

    def permissions(self):
        drive = self

        class P:
            def create(self, fileId, body):
                drive.calls.append(("perm", fileId, body))
                return _Exec({})

        return P()

    def list(self, q, fields, pageSize):
        self.calls.append(("list", q))
        name = q.split("name = '")[1].split("' and")[0]
        parent = q.split("and '")[1].split("' in parents")[0]
        fid = self.tree.get((parent, name))
        return _Exec({"files": [{"id": fid, "name": name}] if fid else []})

    def create(self, body, fields, media_body=None):
        self.n += 1
        fid = f"new{self.n}"
        self.tree[(body["parents"][0], body["name"])] = fid
        self.calls.append(("create", body["name"], media_body is not None))
        return _Exec({"id": fid})

    def get(self, fileId, fields):
        return _Exec({"webViewLink": f"https://drive.google.com/file/d/{fileId}/view"})


@pytest.fixture
def fake_upload(monkeypatch):
    http = types.ModuleType("googleapiclient.http")
    http.MediaFileUpload = lambda path, resumable: ("media", path, resumable)
    pkg = types.ModuleType("googleapiclient")
    monkeypatch.setitem(sys.modules, "googleapiclient", pkg)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http)


def _run(tmp_path, on_drive: bool):
    root = tmp_path / "MyDrive"
    out = (root / "StreamClipper") if on_drive else (tmp_path / "content" / "StreamClipper")
    out.mkdir(parents=True)
    (out / "clip_01.mp4").write_bytes(b"mp4")
    doc = {
        "title": "t",
        "clips": [{"rank": 1, "start": 0, "end": 20, "duration": 20, "score": 70, "mp4": "clip_01.mp4", "title": "A"}],
    }
    from streamclipper.commands import save_clips

    save_clips(out / "clips.json", doc)
    tree = {
        ("root", "StreamClipper"): "F1",
        ("F1", "clip_01.mp4"): "ID1",
        ("F1", "index.html"): "ID2",
        ("F1", "clips.csv"): "ID3",
    }
    return out, root, FakeDrive(tree if on_drive else {})


def test_files_on_drive_are_found_by_path(tmp_path, fake_upload):
    out, root, svc = _run(tmp_path, on_drive=True)
    links = drive_share.share_run({"out_dir": out}, service=svc, drive_root=root, wait_s=0)
    assert ("perm", "ID1", {"type": "anyone", "role": "reader"}) in svc.calls
    assert not any(c[0] == "create" for c in svc.calls)
    doc = json.loads((out / "clips.json").read_text(encoding="utf-8"))
    assert doc["clips"][0]["drive_link"] == "https://drive.google.com/file/d/ID1/view"
    assert links["index.html"].endswith("ID2/view") and doc["drive_links"]["clips.csv"].endswith("ID3/view")
    rows = list(csv.DictReader((out / "clips.csv").read_text(encoding="utf-8-sig").splitlines()))
    assert rows[0]["drive_link"].endswith("ID1/view")
    assert "Drive link" in (out / "index.html").read_text(encoding="utf-8")


def test_files_outside_drive_are_uploaded(tmp_path, fake_upload):
    out, root, svc = _run(tmp_path, on_drive=False)
    drive_share.share_run({"out_dir": out}, service=svc, drive_root=root, wait_s=0)
    created = [c for c in svc.calls if c[0] == "create"]
    assert ("create", "StreamClipper_shared", False) in created and ("create", "clip_01.mp4", True) in created
    assert all(c[2] == {"type": "anyone", "role": "reader"} for c in svc.calls if c[0] == "perm")


def test_outside_colab_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("COLAB_RELEASE_TAG", raising=False)
    monkeypatch.delitem(sys.modules, "google.colab", raising=False)
    with pytest.raises(ToolError, match="only on Google Colab"):
        drive_share.colab_drive_service()
