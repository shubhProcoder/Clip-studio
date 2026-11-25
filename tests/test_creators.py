import json

from streamclipper.creators import DEFAULT_CREATOR_ID, CreatorStore


def test_default_workspace_and_existing_job_migration(tmp_path):
    jobs = tmp_path / "jobs" / "old"
    jobs.mkdir(parents=True)
    (jobs / "job.json").write_text(json.dumps({"id": "old", "status": "done"}), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps([{"id": "old", "status": "done"}]), encoding="utf-8")
    store = CreatorStore(tmp_path)
    assert store.get(DEFAULT_CREATOR_ID)["name"] == "Personal"
    job = json.loads((jobs / "job.json").read_text(encoding="utf-8"))
    assert job["creator_id"] == DEFAULT_CREATOR_ID


def test_creator_settings_are_isolated(tmp_path):
    store = CreatorStore(tmp_path)
    one = store.create({"name": "One", "caption_style": "bold-pop", "preferred_games": ["GTA VI"]})
    two = store.create({"name": "Two", "caption_style": "karaoke-highlight", "preferred_games": ["Minecraft"]})
    store.update(one["id"], {"caption_font": "Anton"})
    assert store.get(one["id"])["caption_font"] == "Anton"
    assert store.get(two["id"])["caption_font"] == ""
    assert store.get(one["id"])["preferred_games"] == ["GTA VI"]
    assert store.get(two["id"])["preferred_games"] == ["Minecraft"]


def test_deleting_creator_migrates_jobs_and_preserves_media(tmp_path):
    media = tmp_path / "shared.mp4"
    media.write_bytes(b"shared")
    jobs = tmp_path / "jobs" / "project"
    jobs.mkdir(parents=True)
    job_file = jobs / "job.json"
    job_file.write_text(json.dumps({"id": "project", "creator_id": "one", "media": str(media)}), encoding="utf-8")
    store = CreatorStore(tmp_path)
    one = store.create({"name": "One"})
    job = json.loads(job_file.read_text(encoding="utf-8"))
    job["creator_id"] = one["id"]
    job_file.write_text(json.dumps(job), encoding="utf-8")
    assert store.delete(one["id"])
    migrated = json.loads(job_file.read_text(encoding="utf-8"))
    assert migrated["creator_id"] == DEFAULT_CREATOR_ID
    assert media.is_file()
    assert store.get(one["id"]) is None
