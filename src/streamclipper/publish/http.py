"""
HTTP for publishing behind one small interface, so tests can swap the network for a recorder.

Transport.request(method, url, headers, body) -> (status, headers with lower-case names, body bytes).
UrllibTransport is the real one (HTTPS only, redirects are not followed, HTTP errors are returned, not raised).
MockTransport answers from a handler or a list and records every request with secrets redacted.
"""

import hashlib
import json
import urllib.error
import urllib.request
from typing import Protocol
from urllib.parse import parse_qsl

from streamclipper.publish.store import PublishError, redact


class Transport(Protocol):
    def request(self, method: str, url: str, headers: dict = None, body: bytes = None) -> tuple: ...


class TransportError(PublishError):
    """The request did not reach the server or got no answer (network down, timeout)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # 308 means "resume incomplete" for YouTube uploads
        return None


def _lower(headers) -> dict:
    return {str(k).lower(): str(v) for k, v in (headers.items() if headers else [])}


class UrllibTransport:
    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method: str, url: str, headers: dict = None, body: bytes = None) -> tuple:
        if not str(url).startswith("https://"):
            raise TransportError("Refused a request that is not HTTPS.", "this is a bug; please report it")
        req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return r.status, _lower(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, _lower(e.headers), e.read()
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            raise TransportError(f"Network error: {reason}", "check the internet connection and try again") from None


def make_transport() -> Transport:
    """The real transport (tests replace this function)."""
    return UrllibTransport()


def _body_record(headers: dict, body):
    if body is None or body == b"":
        return None
    ctype = (headers or {}).get("Content-Type", "") or (headers or {}).get("content-type", "")
    if isinstance(body, str):
        body = body.encode("utf-8")
    if "json" in ctype:
        try:
            return redact(json.loads(body.decode("utf-8")))
        except (UnicodeDecodeError, ValueError):
            pass
    if "x-www-form-urlencoded" in ctype:
        return redact(dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True)))
    return {"bytes": len(body), "sha1": hashlib.sha1(body).hexdigest()}


def _response(res) -> tuple:
    if isinstance(res, Exception):
        raise res
    status, headers, body = (list(res) + [None, None])[:3]
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode("utf-8")
    elif isinstance(body, str):
        body = body.encode("utf-8")
    return int(status), _lower(headers), body or b""


class MockTransport:
    """
    Never touches the network. handler(method, url, headers, body) -> (status, headers, body) or a list of
    such answers used in order. calls = [{method, url, headers, body}] with secrets redacted; binary bodies
    are recorded as {bytes, sha1}.
    """

    def __init__(self, handler=None, responses=None):
        self.handler = handler
        self.responses = list(responses or [])
        self.calls = []

    def request(self, method: str, url: str, headers: dict = None, body: bytes = None) -> tuple:
        self.calls.append(
            {
                "method": method,
                "url": redact(url),
                "headers": redact({str(k): str(v) for k, v in (headers or {}).items()}),
                "body": _body_record(headers, body),
            }
        )
        if self.handler is not None:
            return _response(self.handler(method, url, dict(headers or {}), body))
        if not self.responses:
            raise AssertionError(f"MockTransport: no answer left for {method} {url}")
        return _response(self.responses.pop(0))


def json_of(body: bytes) -> dict:
    try:
        data = json.loads((body or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
