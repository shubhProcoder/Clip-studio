"""
View-only share links for the dashboard's --lan mode. <workspace>/shares.json keeps only the SHA-256 of each
token; the full link is shown once, when it is made. A link ends on its expiry date or when it is revoked.
"""

import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

from streamclipper.state import write_json_atomic
from streamclipper.util import ToolError

DAYS = (1, 7, 30)
KINDS = ("clip", "job")


def _hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


class ShareStore:
    def __init__(self, ws: Path):
        self.path = Path(ws) / "shares.json"
        self.lock = threading.Lock()

    def _load(self) -> list:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def create(self, kind: str, job: str, clip: str = None, days: int = 7) -> tuple:
        """(share id, token, days used). The token is returned only here. Missing or bad days -> 7."""
        if kind not in KINDS:
            raise ToolError("Unknown share type.", "share one clip or one job")
        if kind == "clip" and not clip:
            raise ToolError("Pick a clip to share.", "use the Share button of a clip")
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 7
        if days not in DAYS:
            raise ToolError("Links can last 1, 7 or 30 days.", "pick 1, 7 or 30")
        token = secrets.token_urlsafe(24)
        now = time.time()
        entry = {
            "id": secrets.token_hex(6),
            "token_sha256": _hash(token),
            "kind": kind,
            "job": job,
            "clip": clip if kind == "clip" else None,
            "created": now,
            "expires": now + days * 86400,
            "revoked": False,
        }
        with self.lock:
            data = self._load()
            data.append(entry)
            write_json_atomic(self.path, data)
        return entry["id"], token, days

    def resolve(self, token: str, now: float = None):
        """The share of a token, or None when it is unknown, expired or revoked."""
        if not token:
            return None
        now = time.time() if now is None else now
        h = _hash(token)
        with self.lock:
            for e in self._load():
                if hmac.compare_digest(str(e.get("token_sha256", "")), h):
                    if e.get("revoked") or float(e.get("expires") or 0) <= now:
                        return None
                    return e
        return None

    def revoke(self, share_id: str) -> bool:
        with self.lock:
            data = self._load()
            hit = False
            for e in data:
                if e.get("id") == share_id:
                    e["revoked"] = True
                    hit = True
            if hit:
                write_json_atomic(self.path, data)
        return hit

    def list(self) -> list:
        now = time.time()
        with self.lock:
            data = self._load()
        out = []
        for e in data:
            item = {k: v for k, v in e.items() if k != "token_sha256"}
            item["active"] = not e.get("revoked") and float(e.get("expires") or 0) > now
            out.append(item)
        return sorted(out, key=lambda e: e.get("created") or 0, reverse=True)
