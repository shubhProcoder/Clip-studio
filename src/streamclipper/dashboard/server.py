"""
`streamclipper dashboard`: a local web app on http://127.0.0.1:8765 (standard library only).

Security model: bound to 127.0.0.1 and the Host header must be 127.0.0.1/localhost (DNS rebinding);
`--lan` binds 0.0.0.0 and every request needs the random access token (printed link, then a cookie);
every POST/PUT needs the per-start CSRF token and a same-origin Origin; files are served only from
<workspace>/jobs/<id>/out (and the job's log); jobs run the normal CLI with argument lists, never a shell.
"""

import errno
import functools
import hmac
import html
import json
import os
import re
import secrets
import shutil
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.request
import webbrowser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from streamclipper import paths
from streamclipper.captions.styles import AUTO_STYLE, DEFAULT_HOOK_STYLE, HOOK_STYLES, STYLES
from streamclipper.creators import CreatorStore
from streamclipper.dashboard import brandkit, editor, library, publishing
from streamclipper.dashboard.jobs import (
    JobStore,
    Runner,
    build_live_argv,
    build_vod_argv,
    check_job_id,
    new_job_id,
    parse_bool,
    presets,
    validate_options,
)
from streamclipper.dashboard.shares import DAYS, ShareStore
from streamclipper.effects import ENHANCE_MIX, ZOOM_LEVELS
from streamclipper.util import VIDEO_EXTS, ToolError

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {
    "app.js": "application/javascript; charset=utf-8",
    "icons.js": "application/javascript; charset=utf-8",
    "editor.js": "application/javascript; charset=utf-8",
    "theme.js": "application/javascript; charset=utf-8",
    "landing.js": "application/javascript; charset=utf-8",
    "vendor/gsap.min.js": "application/javascript; charset=utf-8",
    "vendor/ScrollTrigger.min.js": "application/javascript; charset=utf-8",
    "vendor/SplitText.min.js": "application/javascript; charset=utf-8",
    "vendor/lenis.min.js": "application/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "share.css": "text/css; charset=utf-8",
    "tokens.css": "text/css; charset=utf-8",
    "landing.css": "text/css; charset=utf-8",
    "landing.html": "text/html; charset=utf-8",
    "icons-sprite.svg": "image/svg+xml",
    "fonts/geist-sans-300.woff2": "font/woff2",
    "fonts/geist-sans-400.woff2": "font/woff2",
    "fonts/geist-sans-500.woff2": "font/woff2",
    "fonts/geist-sans-600.woff2": "font/woff2",
    "fonts/geist-mono-400.woff2": "font/woff2",
    "fonts/geist-mono-500.woff2": "font/woff2",
    "media/monitor.webm": "video/webm",
    "media/monitor.mp4": "video/mp4",
    "media/monitor-vertical.mp4": "video/mp4",
    "media/monitor-poster.jpg": "image/jpeg",
    "logo.png": "image/png",
    "styles_gallery.jpg": "image/jpeg",
}
FILE_TYPES = {
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}
CSP = "default-src 'self'; font-src 'self'; img-src 'self' data:; media-src 'self'; object-src 'none'; frame-ancestors 'none'"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
UPLOAD_CHUNK = 1 << 20
SEND_CHUNK = 256 * 1024
MAX_JSON = 1 << 20
DISK_RESERVE = 512 * 1024 * 1024  # an upload must leave this much free disk space
LAN_WARNING = (
    "WARNING: --lan: anyone on your network with this link can control StreamClipper. Use it only on a trusted Wi-Fi."
)
SHARE_LABEL = "Works for people on the same Wi-Fi/network"
COOKIE = "sc_access"
GALLERY_GEOMETRY = {"tile_w": 520, "tile_h": 280, "label_h": 40, "cols": 3, "gap": 6}  # gallery.py constants


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = int(status), message


def default_workspace() -> Path:
    """The workspace saved on the Settings page (paths.USER_DIR/dashboard.json), else ~/StreamClipper."""
    p = paths.USER_DIR / "dashboard.json"
    try:
        ws = json.loads(p.read_text(encoding="utf-8")).get("workspace")
        if ws:
            return Path(ws)
    except (OSError, ValueError, AttributeError):
        pass
    return Path.home() / "StreamClipper"


def lan_ip() -> str:
    """This PC's address on the local network (a UDP connect sends no packet); 127.0.0.1 when offline."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def ollama_available() -> bool:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/version", timeout=0.4):
            return True
    except (OSError, ValueError):
        return False


class Cached:
    """
    A slow check (the process list for CapCut, the Ollama ping: 0.6-2 s each on Windows) that runs on a
    background thread. Requests read the last value and start a refresh when it is older than ttl; only the very
    first read waits for the result (at most `wait` seconds).
    """

    def __init__(self, fn, ttl: float, default=None):
        self.fn, self.ttl, self.value = fn, float(ttl), default
        self.at, self.busy = 0.0, False
        self.lock = threading.Lock()
        self.ready = threading.Event()

    def _run(self):
        try:
            v = self.fn()
        except Exception:
            v = self.value
        with self.lock:
            self.value, self.at, self.busy = v, time.time(), False
        self.ready.set()

    def get(self, wait: float = 3.0):
        with self.lock:
            if not self.busy and time.time() - self.at > self.ttl:
                self.busy = True
                threading.Thread(target=self._run, daemon=True).start()
        if not self.ready.is_set() and wait > 0:
            self.ready.wait(wait)
        return self.value


def available_llms(ollama: bool = None) -> list:
    out = ["none"]
    if ollama_available() if ollama is None else ollama:
        out.append("ollama")
    if os.environ.get("GEMINI_API_KEY"):
        out.append("gemini")
    return out


@functools.lru_cache(maxsize=1)
def gallery_geometry() -> dict:
    """Where each tile of styles_gallery.jpg is (same order as gallery.make_gallery). Computed once (the import
    of streamclipper.gallery takes up to ~1 s), warmed on a thread when the server starts."""
    from streamclipper import gallery

    g = {
        "url": "/static/styles_gallery.jpg",
        "tile_w": gallery.TILE_W,
        "tile_h": gallery.TILE_H,
        "label_h": gallery.LABEL_H,
        "cols": gallery.GRID_COLS,
        "gap": gallery.GAP,
    }
    tiles = [{"kind": "caption", "name": s} for s in STYLES] + [{"kind": "hook", "name": h} for h in HOOK_STYLES]
    rows = -(-len(tiles) // g["cols"])
    g["width"] = g["cols"] * g["tile_w"] + (g["cols"] + 1) * g["gap"]
    g["height"] = rows * (g["tile_h"] + g["label_h"]) + (rows + 1) * g["gap"]
    for i, t in enumerate(tiles):
        col, row = i % g["cols"], i // g["cols"]
        t["x"] = g["gap"] + col * (g["tile_w"] + g["gap"])
        t["y"] = g["gap"] + row * (g["tile_h"] + g["label_h"] + g["gap"])
    g["tiles"] = tiles
    return g


def safe_upload_name(name: str) -> str:
    base = str(name or "").replace("\\", "/").split("/")[-1]
    base = re.sub(r"[^\w.\- ()]+", "_", base).strip(" .")
    stem, ext = os.path.splitext(base)
    if not stem or ext.lower() not in VIDEO_EXTS:
        raise HttpError(415, "Only video files can be uploaded (" + ", ".join(sorted(VIDEO_EXTS)) + ").")
    return stem[:120] + ext.lower()


def source_kind(source: str) -> str:
    """Icon of a job card: twitch / youtube / kick for their links, link for other URLs, file for a video file."""
    if not re.match(r"^https?://", source or "", re.I):
        return "file"
    try:
        host = (urlsplit(source).hostname or "").lower()
    except ValueError:
        return "link"
    for kind, hosts in (("twitch", ("twitch.tv",)), ("youtube", ("youtube.com", "youtu.be")), ("kick", ("kick.com",))):
        if any(host == h or host.endswith("." + h) for h in hosts):
            return kind
    return "link"


def _scrub(text: str) -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    return str(text).replace(key, "***") if key else str(text)


def _tail(path: Path, lines: int = 40) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return _scrub("\n".join(text.replace("\r", "").splitlines()[-lines:]))


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let a second server take a used port

    def __init__(self, addr, ws: Path, lan: bool, draft_root: str = ""):
        self.ws = Path(ws).resolve()
        self.ws.mkdir(parents=True, exist_ok=True)
        (self.ws / "uploads").mkdir(exist_ok=True)
        self.lan = bool(lan)
        self.access = secrets.token_urlsafe(24) if lan else ""
        self.csrf = secrets.token_urlsafe(24)
        self.draft_root = str(draft_root or "")
        self.store = JobStore(self.ws)
        self.creators = CreatorStore(self.ws)
        self.runner = Runner(self.store, on_finish=self._job_finished)
        self.shares = ShareStore(self.ws)
        self.public_host = lan_ip() if lan else "127.0.0.1"
        self.doctor = {"running": False, "rows": None, "ended": None}
        self.doctor_lock = threading.Lock()
        self.reframe_lock = threading.Lock()
        self.reframes = {}  # (job, clip key) -> Auto-reframe state of the editor
        self.publisher = publishing.Publisher(self.store)  # uploads and Settings > Publishing (this PC only)
        self.card_cache = {}  # job id -> (signature, {thumb_url, clip_count}) for the Projects grid
        super().__init__(addr, Handler)
        from streamclipper import env as _env

        # slow checks run off the request threads (warmed here, read by /api/settings and /api/options)
        self.capcut_open = Cached(lambda: _env.capcut_running(), 5.0, False)
        self.ollama = Cached(lambda: ollama_available(), 30.0, False)
        self.capcut_open.get(wait=0)
        self.ollama.get(wait=0)
        threading.Thread(target=gallery_geometry, daemon=True).start()

    def _job_finished(self, job: dict):
        library.fix_variant_name(self.store, job)
        editor.fix_history_name(self.store, job)

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)  # skip getfqdn(): it can take seconds on Windows
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port

    @property
    def port(self) -> int:
        return self.server_address[1]

    def settings(self) -> dict:
        p = self.ws / "settings.json"
        st = {"preset": "", "caption_style": AUTO_STYLE, "hook_style": DEFAULT_HOOK_STYLE}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                st.update({k: v for k, v in data.items() if k in st and isinstance(v, str)})
        except (OSError, ValueError):
            pass
        return st

    def close_dashboard(self, wait: float = 30.0):
        self.runner.shutdown(wait)
        self.server_close()


def make_server(
    ws, host: str = "127.0.0.1", port: int = 8765, lan: bool = False, draft_root: str = "", bind: str = None
) -> DashboardServer:
    """The dashboard server (not started). bind overrides the listen address (tests: LAN mode on 127.0.0.1)."""
    if not lan and host not in LOOPBACK:
        raise ToolError(
            f"--host {host} would open the dashboard to other computers.",
            "add --lan to use it on your network (with an access token), or leave --host out",
        )
    addr = bind or ("0.0.0.0" if lan and host in LOOPBACK else host)
    try:
        return DashboardServer((addr, int(port)), ws, lan, draft_root)
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, errno.EACCES, 10048, 10013):
            raise ToolError(f"Port {port} is in use.", f"pass --port {int(port) + 1}") from None
        raise


class Handler(BaseHTTPRequestHandler):
    server_version = "StreamClipper"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    server: DashboardServer

    def log_message(self, fmt, *args):  # request lines can hold tokens: nothing is logged
        return

    def end_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._send_text(405, "Method not allowed")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    # ---- plumbing ---------------------------------------------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, headers: dict = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, obj):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _send_text(self, status: int, text: str):
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8", {"Cache-Control": "no-store"})

    def _send_page(self, status: int, title: str, text: str):
        page = (
            '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>StreamClipper</title></head>'
            f"<body><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p></body></html>"
        )
        self._send(status, page.encode("utf-8"), "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _drain(self):
        """Read a small unread request body before an error reply (else Windows resets the connection)."""
        if getattr(self, "_body_read", False):
            return
        self._body_read = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < n <= MAX_JSON:
            self.rfile.read(n)

    def _handle(self, method: str):
        self._body_read = method == "GET"
        try:
            try:
                self._route(method)
            except (HttpError, ToolError, KeyError, publishing.Busy):
                self._drain()
                raise
        except HttpError as e:
            self._send_json(e.status, {"error": e.message})
        except publishing.Busy as e:
            self._send_json(409, {"error": str(e)})
        except library.CapCutOpen as e:
            self._send_json(409, {"error": str(e), "hint": e.hint})
        except ToolError as e:
            self._send_json(400, {"error": _scrub(str(e)), "hint": _scrub(e.hint or "")})
        except KeyError:
            self._send_json(404, {"error": "Not found."})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # never show a traceback to the browser
            print(f"Dashboard: {type(e).__name__}: {_scrub(str(e))[:300]}", file=sys.stderr)
            try:
                self._send_json(500, {"error": "Something went wrong; see the dashboard terminal."})
            except OSError:
                pass

    @staticmethod
    def _segments(raw_path: str) -> list:
        """Path segments, each URL-decoded; any '..', '.', slash, backslash, NUL or drive letter is refused."""
        if not raw_path.startswith("/"):
            raise HttpError(404, "Not found.")
        if raw_path == "/":
            return []
        parts = raw_path[1:].split("/")
        if parts and parts[-1] == "":
            parts = parts[:-1]
        out = []
        for part in parts:
            s = unquote(part)
            if (
                not s
                or s in (".", "..")
                or any(ch in s for ch in "/\\\x00")
                or re.match(r"^[A-Za-z]:", s)
                or len(s) > 255
            ):
                raise HttpError(404, "Not found.")
            out.append(s)
        return out

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        port = self.server.port
        return host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")

    def _cookie_token(self) -> str:
        try:
            c = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        return c[COOKIE].value if COOKIE in c else ""

    def _lan_auth(self, raw_path: str, query: dict) -> bool:
        access = self.server.access
        given = (query.get("token") or [""])[0]
        if given:
            if hmac.compare_digest(given.encode(), access.encode()):
                self.send_response(302)
                self.send_header("Set-Cookie", f"{COOKIE}={access}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000")
                self.send_header("Location", raw_path or "/")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return False
        else:
            cookie = self._cookie_token()
            if cookie and hmac.compare_digest(cookie.encode(), access.encode()):
                return True
        self._send_page(401, "Access token needed", "Open the link printed in the terminal.")
        return False

    def _csrf_ok(self) -> bool:
        given = self.headers.get("X-CSRF-Token", "")
        if not given or not hmac.compare_digest(given.encode(), self.server.csrf.encode()):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin == f"http://{self.headers.get('Host', '')}"

    def _json_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, "Bad Content-Length.") from None
        if n > MAX_JSON:
            raise HttpError(413, "Request too large.")
        self._body_read = True
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HttpError(400, "Invalid JSON.") from None
        if not isinstance(data, dict):
            raise HttpError(400, "Invalid JSON.")
        return data

    # ---- routing -----------------------------------------------------------------------------------------------

    def _route(self, method: str):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        segs = self._segments(url.path)
        if segs[:1] == ["s"]:
            if method != "GET":
                raise HttpError(405, "Share links are view-only.")
            return self._share(segs[1:], query)
        if self.server.lan:
            if not self._lan_auth(url.path, query):
                return self._drain()
        elif not self._host_ok():
            self._drain()
            return self._send_page(403, "Forbidden", "Open the dashboard at http://127.0.0.1 on this PC.")
        if method != "GET" and not self._csrf_ok():
            raise HttpError(403, "Refused: missing or wrong security token. Reload the page.")
        if method == "GET":
            return self._get(segs, query)
        if method == "POST":
            return self._post(segs)
        if method == "PUT" and len(segs) == 3 and segs[:2] == ["api", "creators"]:
            body = self._json_body()
            try:
                profile = self.server.creators.update(segs[2], body)
            except KeyError:
                raise HttpError(404, "Unknown creator.") from None
            return self._send_json(200, profile)
        if method == "DELETE" and len(segs) == 3 and segs[:2] == ["api", "creators"]:
            try:
                deleted = self.server.creators.delete(segs[2])
            except KeyError:
                raise HttpError(404, "Unknown creator.") from None
            if not deleted:
                raise ToolError("The default personal workspace cannot be deleted.")
            return self._send_json(200, {"deleted": True, "creator_id": segs[2]})
        if method == "PUT" and segs == ["api", "upload"]:
            return self._upload(query)
        if method == "PUT" and segs == ["api", "brand", "upload"]:
            return self._brand_upload(query)
        raise HttpError(405, "Method not allowed.")

    def _get(self, segs: list, query: dict):
        srv = self.server
        if not segs or segs == ["index.html"]:
            return self._index()
        if segs in (["welcome"], ["landing"]):
            return self._landing()
        if segs[0] == "static" and len(segs) in (2, 3):
            return self._static("/".join(segs[1:]))
        if segs[0] == "files" and len(segs) >= 3:
            return self._files(segs[1], segs[2:], query)
        if segs[0] != "api":
            raise HttpError(404, "Not found.")
        api = segs[1:]
        if api == ["options"]:
            return self._send_json(200, self._options())
        if api == ["jobs"]:
            return self._send_json(200, {"jobs": self._jobs()})
        if len(api) == 2 and api[0] == "jobs":
            return self._send_json(200, self._job_detail(api[1]))
        if len(api) == 3 and api[0] == "jobs" and api[2] == "clips":
            statuses = {j["id"]: j["status"] for j in srv.store.list()}
            job = srv.store.get(check_job_id(api[1]))
            clips = srv.publisher.decorate(api[1], library.list_clips(srv.store, api[1], statuses))
            return self._send_json(200, {"job": self._public_job(job), "clips": clips, "lan": srv.lan})
        if len(api) == 5 and api[0] == "jobs" and api[2] == "clips" and api[4] == "publish":
            self._local_only()
            job_id, key = check_job_id(api[1]), library.check_key(api[3])
            return self._send_json(200, srv.publisher.status(job_id, key))
        if (
            len(api) == 5
            and api[0] == "jobs"
            and api[2] == "clips"
            and api[4] in ("edit", "source", "thumbs", "reframe")
        ):
            job_id, key = check_job_id(api[1]), library.check_key(api[3])
            srv.store.get(job_id)
            if api[4] == "edit":
                return self._send_json(200, editor.context(srv.store, job_id, key, srv.lan))
            if api[4] == "thumbs":
                return self._send_file(editor.thumbs(srv.store, job_id, key, srv.lan), "image/jpeg")
            if api[4] == "reframe":
                with srv.reframe_lock:
                    st = srv.reframes.get((job_id, key))
                    return self._send_json(200, dict(st) if st else {"running": False, "segments": None})
            plan = editor._load_plan(srv.store.job_dir(job_id) / "out", key)
            try:
                video = editor.source_info(plan, editor.lan_roots(srv.store) if srv.lan else None)["video"]
            except ToolError as e:
                raise HttpError(404, str(e)) from None
            if Path(video).suffix.lower() not in VIDEO_EXTS:
                raise HttpError(404, "Not found.")
            return self._send_file(Path(video), "video/mp4")
        if api == ["settings"]:
            return self._send_json(200, self._settings())
        if api == ["creators"]:
            return self._send_json(200, {"creators": srv.creators.list()})
        if len(api) == 2 and api[0] == "creators":
            profile = srv.creators.get(api[1])
            if profile is None:
                raise HttpError(404, "Unknown creator.")
            return self._send_json(200, profile)
        if api == ["brand"]:
            return self._send_json(200, brandkit.read(srv.ws, srv.lan))
        if api == ["publish"]:
            self._local_only()
            return self._send_json(200, srv.publisher.settings())
        if api == ["publishing", "accounts"]:
            self._local_only()
            return self._send_json(200, srv.publisher.queue_accounts())
        if api == ["publishing", "queue"]:
            self._local_only()
            creator = query.get("creator", [""])[0]
            creator_id = creator or None
            return self._send_json(200, srv.publisher.queue_overview(creator_id))
        if api == ["doctor"]:
            with srv.doctor_lock:
                return self._send_json(200, dict(srv.doctor))
        if api == ["shares"]:
            return self._send_json(200, {"shares": srv.shares.list(), "lan": srv.lan})
        raise HttpError(404, "Not found.")

    def _post(self, segs: list):
        srv = self.server
        body = self._json_body()
        if segs[:1] != ["api"]:
            raise HttpError(404, "Not found.")
        api = segs[1:]
        if api == ["jobs"]:
            return self._send_json(201, self._new_job(body))
        if len(api) == 3 and api[0] == "jobs":
            job_id = check_job_id(api[1])
            job = srv.store.get(job_id)
            action = api[2]
            if action == "cancel":
                return self._send_json(200, {"status": srv.runner.cancel(job_id)})
            if action == "stop":
                if job.get("kind") != "live":
                    raise ToolError("Only live jobs can be stopped.", "use Cancel for other jobs")
                return self._send_json(200, {"status": srv.runner.stop(job_id)})
            if action == "resume":
                srv.runner.resume(job_id)
                return self._send_json(200, {"status": "queued"})
            raise HttpError(404, "Not found.")
        if len(api) == 4 and api[0] == "jobs" and api[2:] == ["publish", "schedule"]:
            self._local_only()
            job_id = check_job_id(api[1])
            srv.store.get(job_id)
            return self._send_json(*srv.publisher.schedule(job_id, body))
        if len(api) == 5 and api[0] == "jobs" and api[2] == "clips":
            return self._clip_action(check_job_id(api[1]), library.check_key(api[3]), api[4], body)
        if len(api) == 3 and api[0] == "publish" and api[1] in ("youtube", "instagram"):
            self._local_only()
            return self._send_json(*srv.publisher.action(api[1], api[2], body))
        if api == ["publishing", "queue"]:
            self._local_only()
            job_id = check_job_id(str(body.get("job_id") or ""))
            key = library.check_key(str(body.get("key") or ""))
            platforms = body.get("platforms")
            if not isinstance(platforms, list):
                platform = str(body.get("platform") or "").strip().lower()
                platforms = [platform] if platform else []
            if not platforms:
                raise ToolError("Pick at least one publishing platform.")
            scheduled_at = None
            if body.get("scheduled_at"):
                from streamclipper.publish.schedule import check_future, parse_when

                scheduled_at = check_future(parse_when(str(body["scheduled_at"]))).timestamp()
            made = srv.publisher.queue_enqueue(
                job_id,
                key,
                platforms,
                creator_id=str(body.get("creator_id") or "default"),
                scheduled_at=scheduled_at,
                interval_hours=float(body.get("interval_hours") or 3.0),
                daily_caps=body.get("daily_caps") if isinstance(body.get("daily_caps"), dict) else {},
            )
            return self._send_json(201, {"queued": made})
        if len(api) == 4 and api[:2] == ["publishing", "queue"] and api[3] in ("retry", "cancel"):
            self._local_only()
            queue_id = str(api[2])
            result = srv.publisher.queue_retry(queue_id) if api[3] == "retry" else srv.publisher.queue_cancel(queue_id)
            return self._send_json(200, result)
        if api == ["settings"]:
            return self._send_json(200, self._save_settings(body))
        if api == ["creators"]:
            try:
                profile = srv.creators.create(body)
            except ValueError as e:
                raise ToolError(str(e), "give the creator a name") from None
            return self._send_json(201, profile)
        if api == ["brand"]:
            if srv.lan:
                raise HttpError(403, "The brand kit can be changed only on this PC.")
            return self._send_json(200, brandkit.save(srv.ws, body))
        if api == ["doctor"]:
            return self._send_json(202, self._start_doctor())
        if api == ["shares"]:
            return self._send_json(201, self._new_share(body))
        if len(api) == 3 and api[0] == "shares" and api[2] == "revoke":
            if not re.match(r"^[0-9a-f]{1,32}$", api[1]) or not srv.shares.revoke(api[1]):
                raise HttpError(404, "Unknown share link.")
            return self._send_json(200, {"revoked": True})
        raise HttpError(404, "Not found.")

    # ---- static and files --------------------------------------------------------------------------------------

    def _index(self):
        page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        page = page.replace("{{CSRF}}", self.server.csrf).replace("{{MODE}}", "lan" if self.server.lan else "local")
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _landing(self):
        media_dir = STATIC_DIR / "media"
        avail = [
            name
            for name in ("monitor-poster.jpg", "monitor.webm", "monitor.mp4", "monitor-vertical.mp4")
            if (media_dir / name).is_file()
        ]
        page = (STATIC_DIR / "landing.html").read_text(encoding="utf-8")
        page = page.replace("{{MEDIA}}", ",".join(avail) if avail else "none")
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _static(self, name: str):
        ctype = STATIC_TYPES.get(name)
        p = STATIC_DIR / name
        if not ctype or not p.is_file():
            raise HttpError(404, "Not found.")
        if name.startswith("media/"):
            return self._send_file(p, ctype)
        cache = "public, max-age=31536000, immutable" if name.startswith(("fonts/", "vendor/")) else "no-cache"
        self._send(200, p.read_bytes(), ctype, {"Cache-Control": cache})

    def _files(self, job_id: str, rel: list, query: dict):
        job_id = check_job_id(job_id)
        jdir = self.server.store.job_dir(job_id).resolve()
        if job_id not in self.server.store.jobs:
            raise HttpError(404, "Not found.")
        if rel == ["log.txt"]:
            text = _tail(jdir / "log.txt", 100000)
            return self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8", {"Cache-Control": "no-store"})
        if rel[0] != "out" or len(rel) < 2:
            raise HttpError(404, "Not found.")
        ext = os.path.splitext(rel[-1])[1].lower()
        if ext not in FILE_TYPES:
            raise HttpError(404, "Not found.")
        base = jdir / "out"
        p = base.joinpath(*rel[1:]).resolve()
        if not p.is_relative_to(base.resolve()) or not p.is_file():
            raise HttpError(404, "Not found.")
        self._send_file(p, FILE_TYPES[ext], bool(query.get("download")))

    def _send_file(self, path: Path, ctype: str, download: bool = False):
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = (self.headers.get("Range") or "").strip()
        m = re.match(r"^bytes=(\d*)-(\d*)$", rng)
        if rng and m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = end - start + 1 if size else 0
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", path.name)
            self.send_header(
                "Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(path.name)}"
            )
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                data = f.read(min(SEND_CHUNK, left))
                if not data:
                    break
                self.wfile.write(data)
                left -= len(data)

    def _upload(self, query: dict):
        name = safe_upload_name((query.get("name") or [""])[0])
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            raise HttpError(411, "Content-Length is required.")
        try:
            length = int(raw_len)
        except ValueError:
            raise HttpError(400, "Bad Content-Length.") from None
        if length <= 0:
            raise HttpError(400, "The file is empty.")
        up = self.server.ws / "uploads"
        up.mkdir(exist_ok=True)
        if length > shutil.disk_usage(up).free - DISK_RESERVE:
            raise HttpError(507, "Not enough free disk space for this upload.")
        stem, ext = os.path.splitext(name)
        dest, k = up / name, 2
        while dest.exists() or dest.with_name(dest.name + ".part").exists():
            dest = up / f"{stem}_{k}{ext}"
            k += 1
        part = dest.with_name(dest.name + ".part")
        left = length
        self._body_read = True
        try:
            with open(part, "wb") as f:
                while left > 0:
                    data = self.rfile.read(min(UPLOAD_CHUNK, left))
                    if not data:
                        break
                    _write_chunk(f, data)
                    left -= len(data)
        except OSError:
            part.unlink(missing_ok=True)
            raise
        if left:
            part.unlink(missing_ok=True)
            raise HttpError(400, "The upload stopped before the end; try again.")
        os.replace(part, dest)
        self._send_json(201, {"upload": dest.name, "size": length})

    def _brand_upload(self, query: dict):
        """A logo/card image or a font for the brand kit, into <workspace>/brand/ (this PC only, max 20 MB)."""
        if self.server.lan:
            self._drain()
            raise HttpError(403, "The brand kit can be changed only on this PC.")
        name = brandkit.safe_name((query.get("name") or [""])[0])
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            raise HttpError(411, "Content-Length is required.") from None
        if not 0 < length <= brandkit.MAX_UPLOAD:
            self._drain()
            raise HttpError(413, "Brand files must be smaller than 20 MB.")
        self._body_read = True
        data = self.rfile.read(length)
        if len(data) != length:
            raise HttpError(400, "The upload stopped before the end; try again.")
        self._send_json(201, brandkit.store_file(self.server.ws, name, data))

    # ---- API helpers -------------------------------------------------------------------------------------------

    def _options(self) -> dict:
        from streamclipper.captions.styles import STYLES as _S
        from streamclipper.games import game_names

        return {
            "presets": presets(),
            "games": ["", *game_names()],
            "creators": self.server.creators.list(),
            "caption_styles": [AUTO_STYLE, *STYLES],
            "caption_looks": {k: v.look for k, v in _S.items()},
            "hook_styles": list(HOOK_STYLES),
            "hook_looks": dict(HOOK_STYLES),
            "ratios": ["9:16", "1:1"],
            "llms": available_llms(bool(self.server.ollama.get())),
            "share_days": list(DAYS),
            "gallery": gallery_geometry(),
            "defaults": self.server.settings(),
            "mode": "lan" if self.server.lan else "local",
            "effects": {"zoom": list(ZOOM_LEVELS), "enhance_speech": list(ENHANCE_MIX)},
            "brand_kit": brandkit.kit_path(self.server.ws).is_file(),
        }

    def _public_job(self, job: dict) -> dict:
        keys = (
            "id",
            "kind",
            "label",
            "status",
            "stage",
            "progress",
            "created",
            "started",
            "ended",
            "returncode",
            "parent",
            "options",
            "creator_id",
        )
        out = {k: job.get(k) for k in keys}
        q = self.server.runner.queued_ids()
        out["queue_pos"] = q.index(job["id"]) + 1 if job["id"] in q else None
        out["source_kind"] = source_kind(job.get("label") or "")
        out.update(self._card_data(job))
        return out

    def _card_data(self, job: dict) -> dict:
        """Thumbnail and clip count of a job card: clips.json + file checks only (no ffprobe), cached by mtime."""
        out = self.server.store.job_dir(job["id"]) / "out"
        doc_p = out / "clips.json"
        try:
            sig = (out.stat().st_mtime_ns, doc_p.stat().st_mtime_ns, tuple(job.get("deleted") or []))
        except OSError:
            return {"thumb_url": "", "clip_count": 0}
        hit = self.server.card_cache.get(job["id"])
        if hit and hit[0] == sig:
            return dict(hit[1])
        deleted = set(sig[2])
        clips = [c for c in library.load_doc(out).get("clips") or [] if isinstance(c, dict)]
        clips = sorted((c for c in clips if library.clip_key(c) not in deleted), key=lambda c: c.get("rank") or 0)
        thumb = ""
        for c in clips:
            thumb = library._file_url(job["id"], out, c.get("thumb"))
            if thumb:
                break
        data = {"thumb_url": thumb, "clip_count": len(clips)}
        self.server.card_cache[job["id"]] = (sig, data)
        return dict(data)

    def _jobs(self) -> list:
        return [self._public_job(j) for j in self.server.store.list()]

    def _job_detail(self, job_id: str) -> dict:
        job_id = check_job_id(job_id)
        job = self.server.store.get(job_id)
        out = self._public_job(job)
        out["log_tail"] = _tail(self.server.store.job_dir(job_id) / "log.txt", 40)
        return out

    def _resolve_source(self, body: dict) -> str:
        up = self.server.ws / "uploads"
        if body.get("upload"):
            p = up / safe_upload_name(body["upload"])
            if not p.is_file():
                raise ToolError("The uploaded file was not found.", "upload the video again")
            return str(p.resolve())
        src = str(body.get("source") or "").strip()
        if not src:
            raise ToolError("Paste a stream link, upload a video or enter a file path.", "fill in the source")
        if re.match(r"^https?://", src, re.I):
            if len(src) > 2000 or re.search(r"\s", src):
                raise ToolError("That link does not look right.", "paste the stream link without spaces")
            return src
        p = Path(src.strip('"')).expanduser()
        if not p.is_absolute():
            raise ToolError("Enter the full path of the video file.", r"for example D:\Videos\stream.mp4")
        p = p.resolve()
        if self.server.lan and not p.is_relative_to(up.resolve()):
            raise ToolError(
                "On the network only uploaded videos can be used.", "upload the video, or use a stream link"
            )
        if p.suffix.lower() not in VIDEO_EXTS:
            raise ToolError("That file is not a video.", "use an .mp4, .mkv, .mov, .webm, .ts or .flv file")
        if not p.is_file():
            raise ToolError("Video file not found.", "check the path")
        return str(p)

    def _new_job(self, body: dict) -> dict:
        srv = self.server
        opts = validate_options(body)
        source = self._resolve_source(body)
        job_id = new_job_id()
        while job_id in srv.store.jobs:
            job_id = new_job_id()
        jdir = srv.store.job_dir(job_id)
        if opts["mode"] == "live":
            argv = build_live_argv(source, opts, jdir)
        else:
            kit = brandkit.kit_path(srv.ws)
            if opts["brand_kit"] and not kit.is_file():
                raise ToolError("No brand kit yet.", "set it up in Settings > Brand kit, or turn Brand kit off")
            argv = build_vod_argv(source, opts, jdir, kit)
        label = source if re.match(r"^https?://", source, re.I) else Path(source).name
        job = srv.store.create(opts["mode"], label[:200], argv, opts, job_id=job_id)
        srv.runner.submit(job_id)
        return self._public_job(srv.store.get(job["id"]))

    def _clip_action(self, job_id: str, key: str, action: str, body: dict):
        srv = self.server
        job = srv.store.get(job_id)
        if action == "mark":
            return self._send_json(200, {"marks": library.mark(srv.store, job_id, key, body.get("mark"))})
        if action == "rerender":
            v = library.rerender(
                srv.store,
                srv.runner,
                job_id,
                key,
                str(body.get("caption_style") or ""),
                str(body.get("hook_style") or ""),
            )
            return self._send_json(202, {"variant": v})
        if action == "capcut":
            name = library.open_in_capcut(srv.store, job_id, key, srv.draft_root)
            return self._send_json(200, {"draft": name, "message": f"Imported as '{name}'. Open CapCut to edit it."})
        if action == "edit":
            res = editor.apply(
                srv.store,
                srv.runner,
                job_id,
                key,
                str(body.get("action") or ""),
                body.get("edit"),
                srv.draft_root,
                srv.lan,
            )
            return self._send_json(200, res)
        if action == "reframe":
            return self._send_json(202, self._start_reframe(job_id, key, body.get("edit")))
        if action == "publish":
            self._local_only()
            return self._send_json(202, srv.publisher.start(job_id, key, body))
        if action == "delete":
            if job.get("status") in ("running", "queued") or srv.runner.running_id() == job_id:
                raise ToolError("This job is still running.", "wait until it has finished, then delete clips")
            moved = library.delete_clip(srv.store, srv.ws, job_id, key, parse_bool(body.get("confirm"), "confirm"))
            return self._send_json(200, {"moved": moved, "trash": "trash/" + job_id})
        raise HttpError(404, "Not found.")

    def _local_only(self) -> None:
        if self.server.lan:
            raise HttpError(403, publishing.LAN_MSG)

    def _settings(self) -> dict:
        from streamclipper.env import find_draft_root

        srv = self.server
        return {
            "workspace": str(srv.ws),
            "workspace_default": str(default_workspace()),
            "defaults": srv.settings(),
            "draft_root": srv.draft_root or str(find_draft_root()),
            "draft_root_custom": bool(srv.draft_root),
            "gemini": "set" if os.environ.get("GEMINI_API_KEY") else "not set",
            "mode": "lan" if srv.lan else "local",
            "capcut": "open" if srv.capcut_open.get() else "closed",
        }

    def _save_settings(self, body: dict) -> dict:
        srv = self.server
        msg = []
        if isinstance(body.get("defaults"), dict):
            d = body["defaults"]
            st = srv.settings()
            if d.get("preset", "") not in ("", *presets()):
                raise ToolError("Unknown preset.", "pick one from the list")
            if d.get("caption_style", AUTO_STYLE) not in (AUTO_STYLE, *STYLES):
                raise ToolError("Unknown caption style.", "pick one from the list")
            if d.get("hook_style", DEFAULT_HOOK_STYLE) not in HOOK_STYLES:
                raise ToolError("Unknown hook style.", "pick one from the list")
            for k in st:
                if k in d:
                    st[k] = str(d[k])
            from streamclipper.state import write_json_atomic

            write_json_atomic(srv.ws / "settings.json", st)
            msg.append("Defaults saved.")
        if body.get("workspace"):
            ws = Path(str(body["workspace"]).strip().strip('"')).expanduser()
            if not ws.is_absolute():
                raise ToolError("Enter the full path of the workspace folder.", r"for example D:\StreamClipper")
            from streamclipper.state import write_json_atomic

            write_json_atomic(paths.USER_DIR / "dashboard.json", {"workspace": str(ws)})
            msg.append("Workspace saved. Restart the dashboard to use it.")
        return {"message": " ".join(msg) or "Nothing to save.", **self._settings()}

    def _start_doctor(self) -> dict:
        srv = self.server
        with srv.doctor_lock:
            if srv.doctor["running"]:
                return dict(srv.doctor)
            srv.doctor["running"] = True

        def run():
            from streamclipper.env import detect_env, doctor_checks, ensure_bundled_assets, find_capcut_local

            try:
                rows = doctor_checks(detect_env(save=False), ensure_bundled_assets(find_capcut_local(), install=False))
                rows = [[_scrub(str(x)) for x in r] for r in rows]
            except Exception as e:
                rows = [["doctor", "WARN", _scrub(f"{type(e).__name__}: {e}")[:300], ""]]
            with srv.doctor_lock:
                srv.doctor.update(running=False, rows=rows, ended=time.time())

        threading.Thread(target=run, daemon=True).start()
        with srv.doctor_lock:
            return dict(srv.doctor)

    def _start_reframe(self, job_id: str, key: str, edit) -> dict:
        """Auto-reframe of one clip's edit on a background thread (face tracking takes seconds); one run per clip."""
        srv = self.server
        k = (job_id, key)
        state = {"running": True, "error": "", "hint": "", "segments": None, "faces": [], "note": ""}
        with srv.reframe_lock:  # check and claim in one step, so two quick POSTs start one tracking thread
            cur = srv.reframes.get(k)
            if cur and cur["running"]:
                raise HttpError(409, "Auto-reframe is already running for this clip. Wait for it to finish.")
            prev = cur
            srv.reframes[k] = state
        try:
            ctx = editor.context(srv.store, job_id, key, srv.lan)
            form = editor.validate(ctx, edit)
            _jdir, out, edir = editor._dirs(srv.store, job_id, key)
            video = editor.source_info(editor._load_plan(out, key), editor.lan_roots(srv.store) if srv.lan else None)
        except BaseException:
            with srv.reframe_lock:  # a bad request leaves the previous result as it was
                if prev is None:
                    srv.reframes.pop(k, None)
                else:
                    srv.reframes[k] = prev
            raise

        def run():
            try:
                res = editor.auto_reframe(ctx, form, video["video"], edir)
                upd = {"segments": res["segments"], "faces": res["faces"], "note": res["note"]}
            except ToolError as e:
                upd = {"error": _scrub(str(e)), "hint": _scrub(e.hint or "")}
            except Exception as e:  # never kill the dashboard; the page shows the message
                upd = {"error": _scrub(f"Auto-reframe failed: {type(e).__name__}: {e}")[:300], "hint": ""}
            with srv.reframe_lock:
                state.update(upd, running=False)

        threading.Thread(target=run, daemon=True).start()
        with srv.reframe_lock:
            return dict(state)

    def _share_keys(self, share: dict) -> list:
        clips = library.list_clips(self.server.store, share["job"])
        keys = [c["key"] for c in clips if c["mp4_url"]]
        if share["kind"] == "clip":
            return [k for k in keys if k == share.get("clip")]
        return keys

    def _new_share(self, body: dict) -> dict:
        srv = self.server
        if not srv.lan:
            raise ToolError("Start the dashboard with --lan to share on your network.", "streamclipper dashboard --lan")
        kind = body.get("kind")
        job_id = check_job_id(str(body.get("job") or ""))
        srv.store.get(job_id)
        clip = None
        if kind == "clip":
            clip = library.check_key(str(body.get("clip") or ""))
            if clip not in self._share_keys({"kind": "job", "job": job_id}):
                raise ToolError("Only clips with an MP4 can be shared.", "render the clip first")
        share_id, token, days = srv.shares.create(kind, job_id, clip, body.get("days", 7))
        return {
            "id": share_id,
            "url": f"http://{srv.public_host}:{srv.port}/s/{token}",
            "days": days,
            "label": SHARE_LABEL,
        }

    # ---- share pages (no access token; the share token is the key) ----------------------------------------------

    def _share(self, rest: list, query: dict):
        srv = self.server
        share = srv.shares.resolve(rest[0]) if rest and srv.lan else None
        if share is None or share["job"] not in srv.store.jobs:
            return self._send_page(404, "Link not valid", "This share link is wrong, has expired or was revoked.")
        token, rest = rest[0], rest[1:]
        if rest == ["share.css"]:
            return self._static("share.css")
        if rest == ["logo.png"]:
            return self._static("logo.png")
        keys = self._share_keys(share)
        out = srv.store.job_dir(share["job"]) / "out"
        if len(rest) == 2 and rest[0] == "f":
            name = rest[1]
            if name.endswith("_thumb.jpg") and name[: -len("_thumb.jpg")] in keys:
                p = out / name
                if p.is_file():
                    return self._send_file(p, "image/jpeg")
            if name.endswith(".mp4") and name[:-4] in keys:
                p = out / name
                if p.is_file():
                    return self._send_file(p, "video/mp4", bool(query.get("download")))
            return self._send_page(404, "Not found", "This file is not part of the shared link.")
        if rest:
            return self._send_page(404, "Not found", "This file is not part of the shared link.")
        clips = [c for c in library.list_clips(srv.store, share["job"]) if c["key"] in keys]
        self._send_share_page(token, share, clips)

    def _send_share_page(self, token: str, share: dict, clips: list):
        e = html.escape
        base = f"/s/{quote(token)}"
        cards = []
        for c in clips:
            k = quote(c["key"])
            poster = f' poster="{base}/f/{k}_thumb.jpg"' if c["thumb_url"] else ""
            tags = " ".join(c["hashtags"])
            cards.append(
                '<div class="card">'
                f'<video controls preload="metadata"{poster} src="{base}/f/{k}.mp4"></video>'
                f'<div class="body"><div class="title">{e(c["title"] or c["hook_headline"] or c["key"])}</div>'
                f'<div class="meta">Score {e(str(c["score"]))} &middot; {e(c["category"])}</div>'
                + (f'<p class="desc">{e(c["description"])}</p>' if c["description"] else "")
                + (f'<p class="tags">{e(tags)}</p>' if tags else "")
                + f'<a class="btn" href="{base}/f/{k}.mp4?download=1">Download MP4</a></div></div>'
            )
        exp = time.strftime("%d %b %Y %H:%M", time.localtime(float(share["expires"])))
        page = (
            '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Shared clips</title><link rel="stylesheet" href="{base}/share.css"></head><body>'
            f'<header><img src="{base}/logo.png" alt="" width="32" height="32"><span>StreamClipper</span></header>'
            f"<main><h1>{'Shared clip' if share['kind'] == 'clip' else 'Shared clips'}</h1>"
            f'<p class="note">{e(SHARE_LABEL)}. View and download only. This link works until {e(exp)}.</p>'
            f'<div class="grid">{"".join(cards) or "<p>No clips.</p>"}</div></main></body></html>'
        )
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", {"Cache-Control": "no-store"})


def _write_chunk(f, data: bytes) -> None:
    f.write(data)


def _interrupt(_sig, _frame):
    raise KeyboardInterrupt


def run_dashboard(args) -> None:
    ws = Path(args.workspace).expanduser().resolve() if args.workspace else default_workspace().resolve()
    srv = make_server(ws, args.host, args.port, args.lan, args.draft_root)
    port = srv.port
    if args.lan:
        url = f"http://{srv.public_host}:{port}/?token={srv.access}"
        local_url = f"http://127.0.0.1:{port}/?token={srv.access}"
        print(f"StreamClipper dashboard (network mode): {url}")
        print(f"On this PC: {local_url}")
        print(LAN_WARNING)
    else:
        url = local_url = f"http://127.0.0.1:{port}/"
        print(f"StreamClipper dashboard: {url}")
    print(f"Workspace: {ws}")
    print("Press Ctrl+C to stop the dashboard.")
    sys.stdout.flush()
    if hasattr(signal, "SIGBREAK"):  # Ctrl+Break takes the same clean stop path as Ctrl+C
        signal.signal(signal.SIGBREAK, _interrupt)
    if not args.no_browser:
        webbrowser.open(local_url)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("Stopping the dashboard; a running job is stopped cleanly and can be resumed ...")
    finally:
        srv.close_dashboard()
