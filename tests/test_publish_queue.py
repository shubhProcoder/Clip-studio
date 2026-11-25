from streamclipper.publish.queue import QueueStore


def test_queue_enqueue_duplicate_prevention(tmp_path):
    queue = QueueStore(tmp_path)
    first = queue.enqueue("job", "clip", "youtube", scheduled_at=1000)
    duplicate = queue.enqueue("job", "clip", "youtube", scheduled_at=2000)
    assert first["id"] == duplicate["id"]
    assert len(queue.items) == 1


def test_queue_multiple_platform_targets(tmp_path):
    queue = QueueStore(tmp_path)
    yt = queue.enqueue("job", "clip", "youtube", scheduled_at=1000)
    ig = queue.enqueue("job", "clip", "instagram", scheduled_at=1000)
    pack = queue.enqueue("job", "clip", "pack", scheduled_at=1000)
    assert {yt["platform"], ig["platform"], pack["platform"]} == {"youtube", "instagram", "pack"}


def test_queue_daily_cap_and_interval(tmp_path):
    queue = QueueStore(tmp_path)
    queue.enqueue("job1", "clip1", "youtube", account_id="yt-main", scheduled_at=1000)
    next_slot = queue.next_slot("youtube", "yt-main", now=1000, interval_hours=3, daily_cap=1)
    assert next_slot >= 1000 + 86400


def test_queue_retry_uses_bounded_backoff(tmp_path, monkeypatch):
    queue = QueueStore(tmp_path)
    item = queue.enqueue("job", "clip", "youtube", scheduled_at=1000)
    queue.claim(item["id"])
    now = 5000.0
    monkeypatch.setattr("streamclipper.publish.queue.time.time", lambda: now)
    first = queue.mark_failed(item["id"], "temporary")
    assert first["status"] == "scheduled"
    assert first["next_retry"] == now + 60
    queue.claim(item["id"])
    second = queue.mark_failed(item["id"], "temporary")
    assert second["next_retry"] == now + 120
    queue.claim(item["id"])
    third = queue.mark_failed(item["id"], "temporary")
    assert third["status"] == "failed"


def test_queue_posted_history_and_retry(tmp_path):
    queue = QueueStore(tmp_path)
    item = queue.enqueue("job", "clip", "instagram", scheduled_at=1000)
    queue.claim(item["id"])
    posted = queue.mark_posted(item["id"], "media-123", "https://example.com/post")
    assert posted["status"] == "posted"
    assert posted["post_id"] == "media-123"
    overview = queue.overview()
    assert len(overview["published"]) == 1


def test_account_registry_stores_metadata_without_tokens(tmp_path, monkeypatch):
    from streamclipper.publish import store

    monkeypatch.setattr(store, "config_dir", lambda: tmp_path)
    account = store.save_account(
        {"id": "yt-main", "platform": "youtube", "name": "Main Channel", "connected": True}
    )
    assert account["name"] == "Main Channel"
    assert store.load_accounts() == [account]
    assert store.remove_account("yt-main")
    assert store.load_accounts() == []
