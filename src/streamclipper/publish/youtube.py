"""
YouTube Shorts upload with the user's own OAuth client (YouTube Data API v3, no Google SDK).

Sign-in: installed-app OAuth with PKCE (S256) and a loopback redirect on 127.0.0.1:<free port>; the answer
must carry our random state and arrive within 5 minutes. The token (with the client id/secret needed to
refresh it) is saved in paths.CONFIG_DIR/tokens/youtube.json. Upload: resumable videos.insert in 8 MB chunks,
retry with backoff on 5xx and network errors (the server's Range header tells where to go on), one token
refresh on 401. A scheduled upload (publishAt) is always private: YouTube publishes it at that time.
"""

import base64
import hashlib
import json
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from streamclipper.publish import store
from streamclipper.publish.http import TransportError, json_of
from streamclipper.publish.schedule import check_future, rfc3339
from streamclipper.publish.store import PublishError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet%2Cstatus"
UPLOAD_PREFIX = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload", "openid", "email")
TOKEN_NAME = "youtube"
PRIVACY = ("private", "unlisted")
CHUNK = 8 * 1024 * 1024  # a multiple of 256 KiB, as the resumable protocol needs
MAX_RETRIES = 5
AUTH_TIMEOUT = 300.0
QUOTA_REASONS = ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded")
SETUP_HINT = "see docs/GUIDE.md, Publishing: create an OAuth client of type 'Desktop app' and download its JSON"


# ---- OAuth ------------------------------------------------------------------------------------------------


def load_client(path) -> dict:
    """client_id and client_secret from the client_secret_*.json of a 'Desktop app' OAuth client."""
    data = store.read_credentials_file(path, "YouTube OAuth client")
    inst = data.get("installed")
    if not isinstance(inst, dict):
        kind = "a 'Web application' client" if "web" in data else "not an OAuth client file"
        raise PublishError(f"This is {kind}; the loopback sign-in needs a 'Desktop app' client.", SETUP_HINT)
    cid, sec = str(inst.get("client_id") or ""), str(inst.get("client_secret") or "")
    if not cid.endswith(".apps.googleusercontent.com") or not sec:
        raise PublishError("The OAuth client file has no client_id/client_secret.", SETUP_HINT)
    store.remember_secret(sec)
    return {"client_id": cid, "client_secret": sec}


def pkce_pair() -> tuple:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    q = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_URL}?{urlencode(q)}"


_PAGE = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>StreamClipper</title></head>'
    '<body style="font-family:sans-serif;padding:40px"><h1>{title}</h1><p>{text}</p></body></html>'
)


class _Catch(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # the request line holds the OAuth code: never logged
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        ok = url.path == "/" and ("code" in q or "error" in q)
        if ok and self.server.result is None:
            self.server.result = q
        good = ok and "code" in q and q.get("state") == self.server.state
        title = "StreamClipper is connected to YouTube" if good else "Sign-in did not finish"
        text = "You can close this tab." if good else "Go back to StreamClipper and try again."
        body = _PAGE.format(title=title, text=text).encode("utf-8")
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _account(id_token: str) -> str:
    """The e-mail in the ID token's payload (received directly from Google's token endpoint over HTTPS)."""
    try:
        payload = id_token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(data.get("email") or "")
    except (IndexError, ValueError, AttributeError):
        return ""


def _token_error(status: int, body: bytes, what: str) -> PublishError:
    err = json_of(body)
    code = str(err.get("error") or "")
    if code == "invalid_grant":
        return PublishError("YouTube access has expired or was removed.", "connect YouTube again")
    if code in ("invalid_client", "unauthorized_client"):
        return PublishError("Google does not accept this OAuth client.", SETUP_HINT)
    return PublishError(f"Google refused the {what} (HTTP {status}: {code or 'error'}).", "connect YouTube again")


def _save(tok: dict, client: dict, prev: dict = None) -> dict:
    prev = prev or {}
    data = {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token") or prev.get("refresh_token") or "",
        "expires_at": time.time() + float(tok.get("expires_in") or 3600),
        "scope": tok.get("scope") or prev.get("scope") or "",
        "account": _account(tok.get("id_token") or "") or prev.get("account") or "",
    }
    store.save_token(TOKEN_NAME, data)
    return data


def exchange_code(client: dict, code: str, verifier: str, redirect_uri: str, transport) -> dict:
    store.remember_secret(code)
    form = {
        "code": code,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    st, _h, body = transport.request(
        "POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, urlencode(form).encode()
    )
    tok = json_of(body)
    if st != 200 or not tok.get("access_token"):
        raise _token_error(st, body, "sign-in")
    store.remember_secret(tok.get("access_token"))
    store.remember_secret(tok.get("refresh_token"))
    if not tok.get("refresh_token"):
        raise PublishError(
            "Google sent no refresh token, so the connection would stop working in an hour.",
            "remove StreamClipper's access at https://myaccount.google.com/permissions, then connect again",
        )
    return _save(tok, client)


def authorize(client: dict, transport, open_browser=None, timeout: float = AUTH_TIMEOUT, port: int = 0) -> dict:
    """Browser sign-in on this PC (loopback redirect + PKCE + state); saves and returns the token."""
    if open_browser is None:
        import webbrowser

        open_browser = webbrowser.open
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    srv = HTTPServer(("127.0.0.1", int(port)), _Catch)
    srv.timeout = 0.5
    srv.result, srv.state = None, state
    redirect = f"http://127.0.0.1:{srv.server_port}/"
    try:
        url = auth_url(client["client_id"], redirect, challenge, state)
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()
        print("Sign in to Google in the browser window that opened (5 minutes) ...")
        deadline = time.monotonic() + timeout
        while srv.result is None and time.monotonic() < deadline:
            srv.handle_request()
    finally:
        srv.server_close()
    q = srv.result
    if q is None:
        raise PublishError("Google sign-in timed out after 5 minutes.", "click Connect YouTube again and sign in")
    if q.get("state") != state:
        raise PublishError("The sign-in answer did not match this request (state check).", "connect YouTube again")
    if "error" in q or not q.get("code"):
        raise PublishError(f"Google sign-in was not completed ({q.get('error') or 'no code'}).", "connect again")
    return exchange_code(client, q["code"], verifier, redirect, transport)


def refresh(token: dict, transport) -> dict:
    form = {
        "client_id": token["client_id"],
        "client_secret": token["client_secret"],
        "refresh_token": token.get("refresh_token") or "",
        "grant_type": "refresh_token",
    }
    st, _h, body = transport.request(
        "POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, urlencode(form).encode()
    )
    tok = json_of(body)
    if st != 200 or not tok.get("access_token"):
        err = _token_error(st, body, "token refresh")
        if json_of(body).get("error") == "invalid_grant":
            store.delete_token(TOKEN_NAME)
        raise err
    store.remember_secret(tok["access_token"])
    return _save(tok, token, token)


def connected() -> dict:
    tok = store.load_token(TOKEN_NAME)
    ok = bool(tok and tok.get("refresh_token"))
    return {"connected": ok, "account": (tok or {}).get("account", "") if ok else ""}


def disconnect() -> bool:
    return store.delete_token(TOKEN_NAME)


def get_token(transport, client_file: str = "", authorize_fn=None) -> dict:
    """The saved token (signed in again with client_file when there is none or it belongs to another client)."""
    tok = store.load_token(TOKEN_NAME)
    client = load_client(client_file) if client_file else None
    if tok and tok.get("refresh_token") and (client is None or client["client_id"] == tok.get("client_id")):
        return tok
    if client is None:
        raise PublishError(
            "YouTube is not connected yet.",
            "pass --youtube-credentials client_secret.json once (or Settings > Publishing > Connect YouTube)",
        )
    return (authorize_fn or authorize)(client, transport)


# ---- upload -----------------------------------------------------------------------------------------------


def video_body(meta: dict, privacy: str = "private", publish_at=None, now: datetime = None) -> dict:
    """videos.insert resource: snippet {title, description, tags}; status {privacyStatus, publishAt}."""
    if privacy not in PRIVACY:
        raise PublishError(f"Unknown privacy: {privacy}", "use private or unlisted")
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if publish_at:
        status["publishAt"] = rfc3339(check_future(publish_at, now))
        status["privacyStatus"] = "private"  # YouTube publishes a scheduled video only from private
    return {
        "snippet": {"title": meta["title"], "description": meta["description"], "tags": list(meta.get("tags") or [])},
        "status": status,
    }


def _api_error(status: int, body: bytes) -> PublishError:
    err = json_of(body).get("error")
    err = err if isinstance(err, dict) else {}
    reasons = [str(e.get("reason") or "") for e in err.get("errors") or [] if isinstance(e, dict)]
    msg = str(err.get("message") or "")[:300]
    if any(r in QUOTA_REASONS for r in reasons):
        return PublishError(
            "YouTube's upload quota for this Google Cloud project is used up for today.",
            "quotas reset at midnight Pacific Time; try again then, or ask for more quota in the Google Cloud console",
        )
    if "youtubeSignupRequired" in reasons:
        return PublishError("This Google account has no YouTube channel yet.", "create a channel on youtube.com first")
    if status in (401, 403):
        return PublishError(f"YouTube refused the upload (HTTP {status}): {msg}", "connect YouTube again")
    return PublishError(f"YouTube refused the upload (HTTP {status}): {msg or 'no details'}", "check the title")


def _next_offset(headers: dict) -> int:
    rng = headers.get("range", "")  # "bytes=0-8388607"
    try:
        return int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
    except (IndexError, ValueError):
        return 0


def _result(body: bytes) -> dict:
    v = json_of(body)
    vid = str(v.get("id") or "")
    stt = v.get("status") if isinstance(v.get("status"), dict) else {}
    return {
        "platform": "youtube",
        "video_id": vid,
        "url": f"https://www.youtube.com/shorts/{vid}" if vid else "",
        "privacy": stt.get("privacyStatus") or "",
        "publish_at": stt.get("publishAt") or "",
    }


def upload(mp4, body: dict, transport, token: dict, sleep=time.sleep, chunk: int = CHUNK, on_progress=None) -> dict:
    """Resumable videos.insert; returns {platform, video_id, url, privacy, publish_at}."""
    mp4 = Path(mp4)
    size = mp4.stat().st_size
    if size <= 0:
        raise PublishError(f"{mp4.name} is empty.", "render the clip again")
    tok = dict(token)
    if float(tok.get("expires_at") or 0) < time.time() + 60:
        tok = refresh(tok, transport)
    meta_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def auth():
        return {"Authorization": f"Bearer {tok['access_token']}"}

    def start():
        hdrs = {
            **auth(),
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        }
        return transport.request("POST", UPLOAD_URL, hdrs, meta_bytes)

    tries = 0
    while True:
        try:
            st, h, b = start()
        except TransportError:
            st, h, b = None, {}, b""
        if st == 401 and tries == 0:
            tok = refresh(tok, transport)
            tries += 1
            continue
        if st is None or st >= 500:
            tries += 1
            if tries > MAX_RETRIES:
                raise PublishError("YouTube did not answer the upload request.", "try again in a few minutes")
            sleep(min(2 ** (tries - 1), 32))
            continue
        break
    if st != 200:
        raise _api_error(st, b)
    session = h.get("location", "")
    if not session.startswith(UPLOAD_PREFIX):
        raise PublishError("YouTube sent no upload address.", "try again in a few minutes")

    offset, tries, refreshed = 0, 0, False
    with open(mp4, "rb") as f:
        while True:
            f.seek(offset)
            data = f.read(chunk)
            hdrs = {
                **auth(),
                "Content-Type": "video/mp4",
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{size}",
            }
            try:
                st, h, b = transport.request("PUT", session, hdrs, data)
            except TransportError:
                st, h, b = None, {}, b""
            if st in (200, 201):
                if on_progress:
                    on_progress(size, size)
                return _result(b)
            if st == 308:
                offset, tries = _next_offset(h), 0
                if on_progress:
                    on_progress(offset, size)
                continue
            if st == 401 and not refreshed:
                tok, refreshed = refresh(tok, transport), True
                continue
            if st is None or st >= 500:
                tries += 1
                if tries > MAX_RETRIES:
                    raise PublishError("The upload stopped: YouTube did not answer.", "try again in a few minutes")
                sleep(min(2 ** (tries - 1), 32))
                st, h, b = _ask_offset(transport, session, auth(), size)
                if st in (200, 201):
                    return _result(b)
                if st == 308:
                    offset = _next_offset(h)
                continue
            raise _api_error(st, b)


def _ask_offset(transport, session: str, auth: dict, size: int) -> tuple:
    """After an error: how many bytes YouTube has (PUT with Content-Range: bytes */size)."""
    try:
        return transport.request(
            "PUT", session, {**auth, "Content-Length": "0", "Content-Range": f"bytes */{size}"}, b""
        )
    except TransportError:
        return None, {}, b""
