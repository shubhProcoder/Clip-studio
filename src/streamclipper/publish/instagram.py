"""
Instagram Reels through the Instagram API with Facebook Login (Graph API, no Meta SDK).

Flow (Meta's resumable upload guide): POST graph.facebook.com/<ver>/<ig-user-id>/media with media_type=REELS,
upload_type=resumable and the caption -> {id, uri}; POST the MP4 bytes to that rupload.facebook.com URI;
poll GET /<container-id>?fields=status_code until FINISHED (at most 5 minutes, every 5 s); then
POST /<ig-user-id>/media_publish with creation_id. Needs a professional (Business or Creator) Instagram
account linked to a Facebook Page and a token with instagram_content_publish.
Credentials file (user's own, read in place): {"ig_user_id": "...", "access_token": "...", "graph_version": "v23.0"}
"""

import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode

from streamclipper.publish import store
from streamclipper.publish.http import json_of
from streamclipper.publish.store import PublishError

GRAPH = "https://graph.facebook.com"
RUPLOAD_PREFIX = "https://rupload.facebook.com/ig-api-upload/"
DEFAULT_VERSION = "v23.0"
POLL_MAX_S = 300.0
POLL_STEP_S = 5.0
SETUP_HINT = "see docs/GUIDE.md, Publishing > Instagram Reels"


def load_credentials(path) -> dict:
    data = store.read_credentials_file(path, "Instagram credentials")
    uid = str(data.get("ig_user_id") or "").strip()
    token = str(data.get("access_token") or "").strip()
    ver = str(data.get("graph_version") or DEFAULT_VERSION).strip()
    if not re.match(r"^\d{5,25}$", uid):
        raise PublishError("ig_user_id must be the numeric Instagram professional account ID.", SETUP_HINT)
    if len(token) < 20:
        raise PublishError("access_token is missing in the Instagram credentials file.", SETUP_HINT)
    if not re.match(r"^v\d{1,3}\.\d$", ver):
        raise PublishError(f"graph_version must look like {DEFAULT_VERSION}.", SETUP_HINT)
    store.remember_secret(token)
    return {"ig_user_id": uid, "access_token": token, "graph_version": ver}


def _graph_error(status: int, body: bytes, what: str) -> PublishError:
    err = json_of(body).get("error")
    err = err if isinstance(err, dict) else {}
    code, sub = err.get("code"), err.get("error_subcode")
    msg = str(err.get("error_user_msg") or err.get("message") or "")[:300]
    low = msg.lower()
    if code == 190:
        return PublishError("The Instagram access token has expired or is not valid.", "make a new long-lived token")
    if code == 10 or (isinstance(code, int) and 200 <= code <= 299) or "instagram_content_publish" in low:
        return PublishError(
            "The token is missing the instagram_content_publish permission.",
            "add instagram_content_publish (and instagram_basic) to your app's token, then make a new token",
        )
    if "personal" in low or "professional" in low or "business" in low or sub in (2207001, 2207013):
        return PublishError(
            "Publishing needs a professional (Business or Creator) Instagram account linked to a Facebook Page.",
            "switch the account to professional in the Instagram app, link it to a Page, then try again",
        )
    if code in (4, 9, 17, 32, 613) or sub == 2207042:
        return PublishError("Instagram's publishing limit was reached.", "accounts may publish 100 posts in 24 hours")
    return PublishError(f"Instagram refused the {what} (HTTP {status}): {msg or 'no details'}", SETUP_HINT)


def _auth(creds: dict, scheme: str = "Bearer") -> dict:
    return {"Authorization": f"{scheme} {creds['access_token']}"}


def _url(creds: dict, path: str, query: dict = None) -> str:
    q = f"?{urlencode(query)}" if query else ""
    return f"{GRAPH}/{creds['graph_version']}/{path}{q}"


def account_name(creds: dict, transport) -> str:
    st, _h, body = transport.request("GET", _url(creds, creds["ig_user_id"], {"fields": "username"}), _auth(creds))
    if st != 200:
        raise _graph_error(st, body, "account check")
    return str(json_of(body).get("username") or "")


def upload(mp4, caption: str, creds: dict, transport, sleep=time.sleep, clock=time.monotonic) -> dict:
    """container -> rupload -> status poll -> media_publish; returns {platform, media_id, container_id}."""
    mp4 = Path(mp4)
    data = mp4.read_bytes()
    if not data:
        raise PublishError(f"{mp4.name} is empty.", "render the clip again")
    js = {"Content-Type": "application/json"}
    body = {"media_type": "REELS", "upload_type": "resumable", "caption": caption}
    st, _h, b = transport.request(
        "POST", _url(creds, f"{creds['ig_user_id']}/media"), {**_auth(creds), **js}, json.dumps(body).encode()
    )
    res = json_of(b)
    if st != 200 or not res.get("id"):
        raise _graph_error(st, b, "Reel container")
    cid, uri = str(res["id"]), str(res.get("uri") or "")
    if not uri.startswith(RUPLOAD_PREFIX):  # the token goes only to Meta's upload host
        raise PublishError("Instagram sent no upload address.", "try again in a few minutes")
    hdrs = {
        **_auth(creds, "OAuth"),
        "offset": "0",
        "file_size": str(len(data)),
        "Content-Type": "application/octet-stream",
    }
    st, _h, b = transport.request("POST", uri, hdrs, data)
    if st != 200 or json_of(b).get("success") is False:
        raise _graph_error(st, b, "video upload")
    deadline = clock() + POLL_MAX_S
    while True:
        st, _h, b = transport.request("GET", _url(creds, cid, {"fields": "status_code,status"}), _auth(creds))
        if st != 200:
            raise _graph_error(st, b, "status check")
        code = str(json_of(b).get("status_code") or "")
        if code in ("FINISHED", "PUBLISHED"):
            break
        if code in ("ERROR", "EXPIRED"):
            detail = str(json_of(b).get("status") or code)[:200]
            raise PublishError(f"Instagram could not process the video: {detail}", "check the MP4 (9:16, <= 90 s)")
        if clock() >= deadline:
            raise PublishError("Instagram was still processing the video after 5 minutes.", "try again later")
        sleep(POLL_STEP_S)
    st, _h, b = transport.request(
        "POST",
        _url(creds, f"{creds['ig_user_id']}/media_publish"),
        {**_auth(creds), **js},
        json.dumps({"creation_id": cid}).encode(),
    )
    res = json_of(b)
    if st != 200 or not res.get("id"):
        raise _graph_error(st, b, "publish")
    return {"platform": "instagram", "media_id": str(res["id"]), "container_id": cid}
