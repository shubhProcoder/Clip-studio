"""
Opt-in Google Drive share links on Colab (the notebook's share_on_drive box, default off). Uses the user's own
Google login inside Colab (google.colab.auth) and the Drive API; files outside Drive are uploaded into
MyDrive/StreamClipper_shared/<run>/. Each file gets "anyone with the link can view". Nothing goes to any other
service, and StreamClipper has no server.
"""

import time
from pathlib import Path

from streamclipper.util import ToolError, fwd

DRIVE_ROOT = Path("/content/drive/MyDrive")
SHARED_FOLDER = "StreamClipper_shared"
FOLDER_MIME = "application/vnd.google-apps.folder"
WARNING = "Anyone with these links can see the clips. Remove sharing in Google Drive to stop it."


def colab_drive_service():
    from streamclipper.colab import is_colab

    if not is_colab():
        raise ToolError(
            "Drive links work only on Google Colab.",
            "on your PC use 'streamclipper dashboard --lan' and the Share button",
        )
    from google.colab import auth  # preinstalled on Colab

    auth.authenticate_user()
    from googleapiclient.discovery import build

    return build("drive", "v3", cache_discovery=False)


def _q(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


def _find(service, name: str, parent: str, folder: bool = False):
    q = f"name = '{_q(name)}' and '{parent}' in parents and trashed = false"
    if folder:
        q += f" and mimeType = '{FOLDER_MIME}'"
    res = service.files().list(q=q, fields="files(id, name)", pageSize=10).execute()
    files = res.get("files") or []
    return files[0]["id"] if files else None


def _drive_id(service, path: Path, drive_root: Path, wait_s: float = 60.0, step_s: float = 3.0):
    """Drive file id of a file under the Drive mount, found by walking its folder path (retried for Drive sync)."""
    parts = Path(path).relative_to(drive_root).parts
    t0 = time.time()
    while True:
        parent = "root"
        for i, part in enumerate(parts):
            parent = _find(service, part, parent, folder=i < len(parts) - 1)
            if parent is None:
                break
        if parent is not None:
            return parent
        if time.time() - t0 >= wait_s:
            return None
        time.sleep(step_s)


def _folder(service, names: list) -> str:
    parent = "root"
    for name in names:
        found = _find(service, name, parent, folder=True)
        if found is None:
            body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}
            found = service.files().create(body=body, fields="id").execute()["id"]
        parent = found
    return parent


def share_file(service, path, drive_root: Path = DRIVE_ROOT, upload_folder: str = "", wait_s: float = 60.0) -> str:
    """'Anyone with the link can view' link of one file (uploaded first when it is not on Drive)."""
    path = Path(path)
    drive_root = Path(drive_root)
    fid = None
    try:
        path.resolve().relative_to(drive_root.resolve())
        on_drive = True
    except ValueError:
        on_drive = False
    if on_drive:
        fid = _drive_id(service, path.resolve(), drive_root.resolve(), wait_s=wait_s)
    if fid is None:
        from googleapiclient.http import MediaFileUpload

        parent = _folder(service, [SHARED_FOLDER] + [p for p in str(upload_folder).split("/") if p])
        media = MediaFileUpload(str(path), resumable=True)
        fid = (
            service.files()
            .create(body={"name": path.name, "parents": [parent]}, media_body=media, fields="id")
            .execute()["id"]
        )
    service.permissions().create(fileId=fid, body={"type": "anyone", "role": "reader"}).execute()
    return service.files().get(fileId=fid, fields="webViewLink").execute()["webViewLink"]


def share_run(result: dict, service=None, drive_root: Path = DRIVE_ROOT, wait_s: float = 60.0) -> dict:
    """
    Share every rendered MP4 of a run_pipeline() result, then index.html and clips.csv. The links go into
    clips.json (clip['drive_link'], doc['drive_links']), clips.csv (column drive_link) and index.html.
    """
    import json

    from streamclipper.commands import save_clips

    service = service or colab_drive_service()
    out = Path(result["out_dir"])
    clips_json = out / "clips.json"
    doc = json.loads(clips_json.read_text(encoding="utf-8"))
    folder = out.name
    links = {}
    for c in doc.get("clips") or []:
        if c.get("mp4") and (out / c["mp4"]).is_file():
            c["drive_link"] = share_file(service, out / c["mp4"], drive_root, folder, wait_s)
            links[c["mp4"]] = c["drive_link"]
    save_clips(clips_json, doc)  # clips.csv and index.html now carry the links
    doc["drive_links"] = {}
    for name in ("index.html", "clips.csv"):
        if (out / name).is_file():
            doc["drive_links"][name] = share_file(service, out / name, drive_root, folder, wait_s)
    save_clips(clips_json, doc)
    result["clips"] = doc.get("clips") or []
    result["drive_links"] = doc["drive_links"]
    print(f"Drive links for {len(links)} clip(s) are in {fwd(out / 'clips.csv')} and index.html.")
    for name, link in doc["drive_links"].items():
        print(f"  {name:<11} {link}")
    print(WARNING)
    return doc["drive_links"]
