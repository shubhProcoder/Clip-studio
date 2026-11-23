"""Resumable run state (<work>/state.json), written atomically after every finished step."""

import hashlib
import json
import os
from pathlib import Path

SCHEMA = 1
# Only the ASR settings: changing chat, LLM or clip-length options keeps the saved transcript. The LLM
# chunk files are keyed by provider+model (llm.py) and the discovery result by all settings (discovery.py).
HASH_KEYS = ("asr", "whisper_model", "device", "language")


def settings_hash(st: dict) -> str:
    """Hash of the settings that change the saved transcript (missing keys count as None)."""
    raw = json.dumps({k: st.get(k) for k in HASH_KEYS}, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _empty(source_key: str, s_hash: str) -> dict:
    return {
        "schema": SCHEMA,
        "source_key": source_key,
        "settings_hash": s_hash,
        "stages": {
            "audio": None,
            "envelope": None,
            "quality": None,
            "chat": None,
            "asr_chunks": [],
            "llm_chunks": [],
            "discovery": None,
        },
        "rendered": {},
    }


def write_json_atomic(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=float), encoding="utf-8")
    os.replace(tmp, path)


class RunState:
    """
    state.json = {schema, source_key, settings_hash, stages{audio, envelope, chat, asr_chunks[],
    llm_chunks[], discovery}, rendered{rank: name}}. A run with the same source and settings resumes;
    fresh=True (or another source/settings) starts over.
    """

    def __init__(self, path: Path, source_key: str, st: dict, fresh: bool = False):
        self.path = Path(path)
        s_hash = settings_hash(st)
        self.data = _empty(source_key, s_hash)
        self.resumed = False
        if fresh or not self.path.is_file():
            return
        try:
            old = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if (
            isinstance(old, dict)
            and old.get("schema") == SCHEMA
            and old.get("source_key") == source_key
            and old.get("settings_hash") == s_hash
        ):
            self.data = old
            self.data.setdefault("stages", {}).setdefault("asr_chunks", [])
            self.data.setdefault("rendered", {})
            self.resumed = True

    @classmethod
    def open_existing(cls, path: Path):
        """Load a state file as-is (no source/settings check), or None when there is none."""
        path = Path(path)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            return None
        obj = cls.__new__(cls)
        obj.path, obj.data, obj.resumed = path, data, True
        obj.data.setdefault("rendered", {})
        return obj

    @property
    def stages(self) -> dict:
        return self.data["stages"]

    def save(self) -> None:
        write_json_atomic(self.path, self.data)

    def set_stage(self, name: str, value) -> None:
        self.stages[name] = value
        self.save()

    def chunk_done(self, idx: int) -> bool:
        return int(idx) in self.stages["asr_chunks"]

    def mark_chunk(self, idx: int) -> None:
        if not self.chunk_done(idx):
            self.stages["asr_chunks"].append(int(idx))
            self.stages["asr_chunks"].sort()
        self.save()

    def mark_rendered(self, rank, name: str, key: str = "") -> None:
        self.data["rendered"][str(rank)] = name
        if key:
            self.data.setdefault("made_keys", {})[str(rank)] = key
        self.save()

    def made(self, rank, key: str) -> str:
        """The name of the clip made for `rank` with the same make settings (key), or '."""
        name = self.data.get("rendered", {}).get(str(rank), "")
        return name if name and key and self.data.get("made_keys", {}).get(str(rank)) == key else ""
