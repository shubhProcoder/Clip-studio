"""Persistent multi-platform publishing queue."""

from __future__ import annotations

import copy
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from streamclipper.state import write_json_atomic

OPEN = ("scheduled", "posting")
HISTORY = ("posted", "failed", "cancelled", "packed")
MAX_ATTEMPTS = 3
BASE_RETRY_S = 60.0
MAX_RETRY_S = 3600.0


def _day(timestamp: float) -> str:
    return datetime.fromtimestamp(float(timestamp), timezone.utc).strftime("%Y-%m-%d")


class QueueStore:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace)
        self.path = self.workspace / "publish_queue.json"
        self.lock = threading.RLock()
        self.items = []
        self._load()

    def _load(self) -> None:
        with self.lock:
            try:
                import json

                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.items = data if isinstance(data, list) else []
            except (OSError, ValueError):
                self.items = []
            for item in self.items:
                if item.get("status") == "posting":
                    item["status"] = "scheduled"
                    item["next_retry"] = time.time()

    def _save(self) -> None:
        write_json_atomic(self.path, self.items)

    def _duplicate(self, job_id: str, clip_key: str, platform: str, account_id: str) -> dict | None:
        for item in self.items:
            if (
                item.get("job_id") == job_id
                and item.get("clip_key") == clip_key
                and item.get("platform") == platform
                and item.get("account_id") == account_id
                and item.get("status") in OPEN + ("posted",)
            ):
                return item
        return None

    def next_slot(
        self,
        platform: str,
        account_id: str,
        now: float = None,
        interval_hours: float = 3.0,
        daily_cap: int = 4,
    ) -> float:
        now = time.time() if now is None else float(now)
        interval = max(0.25, float(interval_hours)) * 3600.0
        candidate = now
        mine = [
            item
            for item in self.items
            if item.get("platform") == platform
            and item.get("account_id") == account_id
            and item.get("status") in OPEN + ("posted",)
        ]
        candidate = max(
            candidate,
            max(
                (float(item.get("posted_at") or item.get("scheduled_at") or 0.0) + interval)
                for item in mine
            )
            if mine
            else candidate,
        )
        for _ in range(30):
            count = sum(
                1
                for item in mine
                if _day(item.get("posted_at") or item.get("scheduled_at") or candidate)
                == _day(candidate)
            )
            if count < max(1, int(daily_cap)):
                break
            candidate += 86400.0
        return candidate

    def enqueue(
        self,
        job_id: str,
        clip_key: str,
        platform: str,
        account_id: str = "",
        creator_id: str = "default",
        scheduled_at: float = None,
        interval_hours: float = 3.0,
        daily_cap: int = 4,
        account_name: str = "",
    ) -> dict | None:
        platform = str(platform).strip().lower()
        account_id = str(account_id or platform)
        duplicate = self._duplicate(job_id, clip_key, platform, account_id)
        if duplicate:
            return copy.deepcopy(duplicate)
        due = (
            float(scheduled_at)
            if scheduled_at is not None
            else self.next_slot(platform, account_id, interval_hours=interval_hours, daily_cap=daily_cap)
        )
        item = {
            "id": secrets.token_hex(8),
            "job_id": job_id,
            "clip_key": clip_key,
            "creator_id": creator_id,
            "platform": platform,
            "account_id": account_id,
            "account_name": account_name or account_id,
            "status": "scheduled",
            "scheduled_at": due,
            "created_at": time.time(),
            "posted_at": None,
            "attempts": 0,
            "next_retry": None,
            "post_id": "",
            "url": "",
            "error": None,
        }
        with self.lock:
            self.items.append(item)
            self._save()
        return copy.deepcopy(item)

    def due(self, now: float = None) -> list:
        now = time.time() if now is None else float(now)
        with self.lock:
            return [
                copy.deepcopy(item)
                for item in self.items
                if item.get("status") == "scheduled"
                and float(item.get("scheduled_at") or 0.0) <= now
                and (item.get("next_retry") is None or float(item.get("next_retry") or 0.0) <= now)
            ]

    def claim(self, queue_id: str) -> dict | None:
        with self.lock:
            item = next((x for x in self.items if x.get("id") == queue_id), None)
            if item is None or item.get("status") != "scheduled":
                return None
            item["status"] = "posting"
            item["attempts"] = int(item.get("attempts") or 0)
            self._save()
            return copy.deepcopy(item)

    def mark_posted(self, queue_id: str, post_id: str = "", url: str = "") -> dict | None:
        with self.lock:
            item = next((x for x in self.items if x.get("id") == queue_id), None)
            if item is None:
                return None
            item.update(
                status="posted",
                posted_at=time.time(),
                post_id=str(post_id or ""),
                url=str(url or ""),
                error=None,
                next_retry=None,
            )
            self._save()
            return copy.deepcopy(item)

    def mark_packed(self, queue_id: str, folder: str = "") -> dict | None:
        with self.lock:
            item = next((x for x in self.items if x.get("id") == queue_id), None)
            if item is None:
                return None
            item.update(
                status="packed",
                posted_at=time.time(),
                post_id="",
                url=str(folder or ""),
                error=None,
                next_retry=None,
            )
            self._save()
            return copy.deepcopy(item)

    def mark_failed(self, queue_id: str, error: str) -> dict:
        with self.lock:
            item = next((x for x in self.items if x.get("id") == queue_id), None)
            if item is None:
                raise KeyError(queue_id)
            attempts = int(item.get("attempts") or 0) + 1
            item["attempts"] = attempts
            item["error"] = str(error)[:500]
            if attempts < MAX_ATTEMPTS:
                delay = min(MAX_RETRY_S, BASE_RETRY_S * (2 ** (attempts - 1)))
                item.update(status="scheduled", next_retry=time.time() + delay)
            else:
                item.update(status="failed", next_retry=None)
            self._save()
            return copy.deepcopy(item)

    def retry(self, queue_id: str) -> dict:
        with self.lock:
            item = next((x for x in self.items if x.get("id") == queue_id), None)
            if item is None:
                raise KeyError(queue_id)
            if item.get("status") not in ("failed", "cancelled"):
                raise ValueError("Only failed or cancelled queue items can be retried.")
            item.update(status="scheduled", attempts=0, error=None, next_retry=None, scheduled_at=time.time())
            self._save()
            return copy.deepcopy(item)

    def cancel(self, queue_id: str) -> dict:
        with self.lock:
            item = next((x for x in self.items if x.get("id") == queue_id), None)
            if item is None:
                raise KeyError(queue_id)
            if item.get("status") not in OPEN:
                raise ValueError("Only scheduled or posting queue items can be cancelled.")
            item.update(status="cancelled", next_retry=None)
            self._save()
            return copy.deepcopy(item)

    def overview(self, creator_id: str = None) -> dict:
        with self.lock:
            items = [
                copy.deepcopy(item)
                for item in self.items
                if creator_id is None or item.get("creator_id") == creator_id
            ]
        upcoming = sorted((item for item in items if item["status"] in OPEN), key=lambda item: item["scheduled_at"])
        published = sorted(
            (item for item in items if item["status"] == "posted"),
            key=lambda item: -(item.get("posted_at") or 0),
        )
        failed = sorted((item for item in items if item["status"] == "failed"), key=lambda item: item["scheduled_at"])
        exported = sorted(
            (item for item in items if item["status"] == "packed"),
            key=lambda item: -(item.get("posted_at") or 0),
        )
        cancelled = [item for item in items if item["status"] == "cancelled"]
        return {
            "upcoming": upcoming,
            "published": published,
            "exported": exported,
            "failed": failed,
            "cancelled": cancelled,
        }
