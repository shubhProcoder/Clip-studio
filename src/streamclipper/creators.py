"""Lightweight creator workspaces inside one StreamClipper workspace."""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from pathlib import Path

from streamclipper.state import write_json_atomic

CREATORS_FILE = "creators.json"
DEFAULT_CREATOR_ID = "default"

DEFAULT_PROFILE = {
    "id": DEFAULT_CREATOR_ID,
    "name": "Personal",
    "source_channels": [],
    "preferred_games": [],
    "brand_kit": "",
    "logo": "",
    "caption_style": "auto",
    "caption_font": "",
    "highlight_color": "",
    "hook_style": "bold-box",
    "layout": "split",
    "connected_accounts": [],
    "publishing_rules": {
        "enabled": False,
        "interval_hours": 3.0,
        "daily_caps": {"youtube": 4, "instagram": 4, "tiktok": 4},
    },
    "clip_history": [],
    "analytics_history": [],
    "created": 0.0,
}


def _clean_text(value, limit=120) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())[:limit]


def _clean_list(value, limit=30) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = _clean_text(item, 300)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug[:40] or "creator"


def clean_profile(profile: dict) -> dict:
    profile = profile or {}
    out = copy.deepcopy(DEFAULT_PROFILE)
    out.update(
        {
            "name": _clean_text(profile.get("name") or "Personal", 80) or "Personal",
            "source_channels": _clean_list(profile.get("source_channels")),
            "preferred_games": _clean_list(profile.get("preferred_games")),
            "brand_kit": _clean_text(profile.get("brand_kit"), 500),
            "logo": _clean_text(profile.get("logo"), 500),
            "caption_style": _clean_text(profile.get("caption_style") or "auto", 40),
            "caption_font": _clean_text(profile.get("caption_font"), 80),
            "highlight_color": _clean_text(profile.get("highlight_color"), 20),
            "hook_style": _clean_text(profile.get("hook_style") or "bold-box", 40),
            "layout": _clean_text(profile.get("layout") or "split", 40),
            "connected_accounts": _clean_list(profile.get("connected_accounts")),
            "clip_history": copy.deepcopy(profile.get("clip_history") or [])[-500:],
            "analytics_history": copy.deepcopy(profile.get("analytics_history") or [])[-500:],
        }
    )
    rules = profile.get("publishing_rules")
    if isinstance(rules, dict):
        daily = rules.get("daily_caps")
        if isinstance(daily, dict):
            for platform in ("youtube", "instagram", "tiktok"):
                try:
                    daily[platform] = max(1, min(50, int(daily.get(platform, 4))))
                except (TypeError, ValueError):
                    daily[platform] = 4
        try:
            interval = max(0.25, min(168.0, float(rules.get("interval_hours", 3.0))))
        except (TypeError, ValueError):
            interval = 3.0
        out["publishing_rules"] = {
            "enabled": bool(rules.get("enabled", False)),
            "interval_hours": interval,
            "daily_caps": {
                "youtube": int((daily or {}).get("youtube", 4)),
                "instagram": int((daily or {}).get("instagram", 4)),
                "tiktok": int((daily or {}).get("tiktok", 4)),
            },
        }
    out["created"] = float(profile.get("created") or 0.0)
    return out


class CreatorStore:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace)
        self.path = self.workspace / CREATORS_FILE
        self.lock = threading.RLock()
        self._creators = {}
        self._load()
        self.migrate_jobs()

    def _load(self) -> None:
        with self.lock:
            data = {}
            if self.path.is_file():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except (OSError, ValueError):
                    data = {}
            for creator_id, profile in data.items():
                if isinstance(profile, dict):
                    cleaned = clean_profile(profile)
                    cleaned["id"] = str(creator_id)
                    self._creators[str(creator_id)] = cleaned
            default = clean_profile(DEFAULT_PROFILE)
            default["id"] = DEFAULT_CREATOR_ID
            default.setdefault("created", 0.0)
            self._creators.setdefault(DEFAULT_CREATOR_ID, default)

    def _save(self) -> None:
        write_json_atomic(self.path, self._creators)

    def list(self) -> list:
        with self.lock:
            ordered = sorted(
                self._creators.items(),
                key=lambda item: item[1].get("name", ""),
            )
            return [copy.deepcopy(profile) for _, profile in ordered]

    def get(self, creator_id: str) -> dict | None:
        with self.lock:
            profile = self._creators.get(str(creator_id or DEFAULT_CREATOR_ID))
            return copy.deepcopy(profile) if profile else None

    def create(self, data: dict) -> dict:
        data = data or {}
        name = _clean_text(data.get("name"), 80)
        if not name:
            raise ValueError("Creator name is required.")
        base = _slug(name)
        creator_id = base
        n = 2
        with self.lock:
            while creator_id in self._creators:
                creator_id = f"{base}-{n}"
                n += 1
            profile = clean_profile({**data, "name": name, "created": time.time()})
            profile["id"] = creator_id
            self._creators[creator_id] = profile
            self._save()
        return copy.deepcopy(profile)

    def update(self, creator_id: str, data: dict) -> dict:
        creator_id = str(creator_id)
        if creator_id == DEFAULT_CREATOR_ID:
            return self.update_default(data)
        with self.lock:
            if creator_id not in self._creators:
                raise KeyError(creator_id)
            old = self._creators[creator_id]
            profile = clean_profile({**old, **(data or {})})
            profile["id"] = creator_id
            profile["created"] = old.get("created") or time.time()
            self._creators[creator_id] = profile
            self._save()
            return copy.deepcopy(profile)

    def update_default(self, data: dict) -> dict:
        with self.lock:
            old = self._creators[DEFAULT_CREATOR_ID]
            profile = clean_profile({**old, **(data or {})})
            profile["id"] = DEFAULT_CREATOR_ID
            profile["name"] = _clean_text(profile.get("name") or "Personal", 80)
            profile["created"] = old.get("created") or time.time()
            self._creators[DEFAULT_CREATOR_ID] = profile
            self._save()
            return copy.deepcopy(profile)

    def delete(self, creator_id: str) -> bool:
        creator_id = str(creator_id)
        if creator_id == DEFAULT_CREATOR_ID:
            return False
        with self.lock:
            if creator_id not in self._creators:
                raise KeyError(creator_id)
            del self._creators[creator_id]
            self._save()
            self.migrate_jobs(default_creator=creator_id)
        return True

    def migrate_jobs(self, default_creator: str | None = None) -> int:
        jobs_dir = self.workspace / "jobs"
        if not jobs_dir.is_dir():
            return 0
        changed = 0
        for job_file in jobs_dir.glob("*/job.json"):
            try:
                job = json.loads(job_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(job, dict):
                continue
            creator_id = str(job.get("creator_id") or DEFAULT_CREATOR_ID)
            if default_creator and creator_id == default_creator:
                creator_id = DEFAULT_CREATOR_ID
            if creator_id not in self._creators:
                creator_id = DEFAULT_CREATOR_ID
            if job.get("creator_id") != creator_id:
                job["creator_id"] = creator_id
                write_json_atomic(job_file, job)
                changed += 1
        index = self.workspace / "jobs.json"
        if index.is_file():
            try:
                rows = json.loads(index.read_text(encoding="utf-8"))
                if isinstance(rows, list):
                    changed_index = False
                    for row in rows:
                        if isinstance(row, dict):
                            creator_id = str(row.get("creator_id") or DEFAULT_CREATOR_ID)
                            if default_creator and creator_id == default_creator:
                                creator_id = DEFAULT_CREATOR_ID
                            if creator_id not in self._creators:
                                creator_id = DEFAULT_CREATOR_ID
                            if row.get("creator_id") != creator_id:
                                row["creator_id"] = creator_id
                                changed_index = True
                    if changed_index:
                        write_json_atomic(index, rows)
            except (OSError, ValueError):
                pass
        return changed
