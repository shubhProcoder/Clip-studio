"""
Publishing tokens and settings in paths.CONFIG_DIR (never in a repo or workspace) and secret redaction.

Token files are written with mode 0600 on POSIX (the folder 0700). Every message that can reach a log, an
exception or the dashboard goes through redact(): it hides Authorization headers, access/refresh tokens,
client secrets, OAuth codes and every token value this process has loaded (remember_secret).
"""

import json
import os
import re
import sys
import threading
from pathlib import Path

from streamclipper import paths
from streamclipper.util import ToolError

REDACTED = "<redacted>"
SECRET_KEYS = frozenset(
    {"authorization", "access_token", "refresh_token", "client_secret", "id_token", "code", "code_verifier"}
)
_KEY_ALT = "access_token|refresh_token|client_secret|id_token|code_verifier|code"
_PATTERNS = (
    (re.compile(r"(?i)\b(Bearer|OAuth)\s+[A-Za-z0-9._~+/=\-]+"), r"\1 " + REDACTED),
    (re.compile(rf"(?i)\b({_KEY_ALT})=[^&\s\"']+"), r"\1=" + REDACTED),
    (re.compile(rf"(?i)(\"(?:{_KEY_ALT}|authorization)\"\s*:\s*\")[^\"]*"), r"\1" + REDACTED),
)
_known = set()
_known_lock = threading.Lock()


def remember_secret(value) -> None:
    """Hide this exact value in every later redact() call (tokens loaded from files or received)."""
    if isinstance(value, str) and len(value) >= 8:
        with _known_lock:
            _known.add(value)


def redact(obj):
    """A copy of str/dict/list/bytes data with every secret replaced by <redacted>."""
    if isinstance(obj, dict):
        return {
            k: (REDACTED if str(k).lower() in SECRET_KEYS and isinstance(v, str) and v else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, bytes):
        return redact(obj.decode("utf-8", errors="replace"))
    if not isinstance(obj, str):
        return obj
    text = obj
    with _known_lock:
        known = sorted(_known, key=len, reverse=True)
    for v in known:
        if v in text:
            text = text.replace(v, REDACTED)
    for rx, rep in _PATTERNS:
        text = rx.sub(rep, text)
    return text


class PublishError(ToolError):
    """A publishing failure; the message and hint are redacted when the error is made."""

    def __init__(self, msg: str, hint: str = ""):
        super().__init__(redact(str(msg)), redact(str(hint or "")))


def config_dir() -> Path:
    return Path(paths.CONFIG_DIR)


def token_path(name: str) -> Path:
    if not re.match(r"^[a-z]{2,20}$", name):
        raise ValueError(name)
    return config_dir() / "tokens" / f"{name}.json"


def _private_dir(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(d, 0o700)


def write_private_json(path: Path, data: dict) -> None:
    """Atomic write; on POSIX the file is created with mode 0600 (never readable by other users)."""
    path = Path(path)
    _private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def save_token(name: str, data: dict) -> Path:
    for k in ("access_token", "refresh_token", "client_secret"):
        remember_secret(data.get(k))
    p = token_path(name)
    write_private_json(p, data)
    return p


def load_token(name: str):
    p = token_path(name)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for k in ("access_token", "refresh_token", "client_secret"):
        remember_secret(data.get(k))
    return data


def delete_token(name: str) -> bool:
    p = token_path(name)
    if p.is_file():
        p.unlink()
        return True
    return False


def settings_path() -> Path:
    return config_dir() / "publish.json"


def load_settings() -> dict:
    """{youtube_client: path, instagram_credentials: path, instagram_account: name}: paths only, no secrets."""
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def save_settings(**changes) -> dict:
    st = load_settings()
    for k, v in changes.items():
        if v:
            st[k] = str(v)
        else:
            st.pop(k, None)
    write_private_json(settings_path(), st)
    return st


def read_credentials_file(path, what: str) -> dict:
    """A JSON credentials file the user points to (read in place, never copied)."""
    p = Path(str(path or "").strip().strip('"')).expanduser()
    if not str(path or "").strip():
        raise PublishError(f"No {what} file given.", "pass the path of the JSON file")
    if not p.is_file():
        raise PublishError(f"{what} file not found: {p}", "check the path; put quotes around paths with spaces")
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise PublishError(f"{what} file is not valid JSON: {p.name}", "download it again") from None
    if not isinstance(data, dict):
        raise PublishError(f"{what} file has the wrong format: {p.name}", "see docs/GUIDE.md, Publishing")
    return data


def accounts_path() -> Path:
    return config_dir() / "publishing_accounts.json"


def load_accounts() -> list:
    p = accounts_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def save_account(account: dict) -> dict:
    account = dict(account or {})
    aid = str(account.get("id") or "").strip()
    platform = str(account.get("platform") or "").strip().lower()
    name = str(account.get("name") or "").strip()
    if not re.match(r"^[a-zA-Z0-9_-]{1,60}$", aid):
        raise PublishError("Invalid publishing account ID.", "use a short name or generated ID")
    if platform not in ("youtube", "instagram", "tiktok"):
        raise PublishError("Unknown publishing platform.", "use YouTube, Instagram or TikTok")
    if not name:
        raise PublishError("Publishing account name is required.")
    clean = {
        "id": aid,
        "platform": platform,
        "name": name[:100],
        "credentials_file": str(account.get("credentials_file") or "")[:500],
        "connected": bool(account.get("connected", False)),
    }
    accounts = {x.get("id"): x for x in load_accounts()}
    accounts[aid] = clean
    write_private_json(accounts_path(), list(accounts.values()))
    return clean


def remove_account(account_id: str) -> bool:
    aid = str(account_id or "")
    old = load_accounts()
    accounts = [x for x in old if x.get("id") != aid]
    write_private_json(accounts_path(), accounts)
    return len(accounts) != len(old)
