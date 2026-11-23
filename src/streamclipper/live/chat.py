"""
Live chat, read-only. Twitch: anonymous IRC (justinfan, TLS, no password). YouTube: tail the live_chat
file that yt-dlp writes. Kick: not supported yet. A chat failure never stops the live run.
"""

import random
import re
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

from streamclipper.chat import normalize_username, youtube_chat_line_full
from streamclipper.live.ingest import _proc_flags, twitch_channel
from streamclipper.util import ytdlp_cmd

TWITCH_HOST, TWITCH_PORT = "irc.chat.twitch.tv", 6697
_PRIVMSG = re.compile(r"^(?:@\S+ )?:\S+ PRIVMSG #\S+ :(.*)$")
_PRIVMSG_FULL = re.compile(r"^(?:@(?P<tags>\S+) )?:(?P<sender>[^\s!]+)(?:![^\s]+)? PRIVMSG #\S+ :(.*)$")


def parse_irc_line(line: str) -> tuple:
    """('ping', payload) | ('msg', text) | (None, None). Tags (@...) and the sender are ignored."""
    line = line.rstrip("\r\n")
    if line.startswith("PING"):
        return "ping", line[4:].strip() or ":tmi.twitch.tv"
    m = _PRIVMSG.match(line)
    if m:
        return "msg", m.group(1)
    return None, None


def parse_irc_line_full(line: str) -> tuple:
    """('ping', payload, '') or ('msg', text, normalized_user)."""
    line = line.rstrip("\r\n")
    if line.startswith("PING"):
        return "ping", line[4:].strip() or ":tmi.twitch.tv", ""
    m = _PRIVMSG_FULL.match(line)
    if m:
        sender = m.group("sender")
        tags = m.group("tags") or ""
        display = ""
        for tag in tags.split(";"):
            if tag.lower().startswith("display-name="):
                display = tag.split("=", 1)[1]
                break
        return "msg", m.group(3), normalize_username(display or sender)
    return None, None, ""


class LiveChat:
    """Collects (stream_time, text). clock() -> current stream time (Ring.wall_to_stream)."""

    def __init__(self, kind: str, url: str, clock, out_dir: Path):
        self.kind, self.url, self.clock = kind, url, clock
        self.out_dir = Path(out_dir)
        self.msgs = []
        self.keep_s = 600.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._proc = None
        self.status = "off"

    def start(self):
        if self.kind == "twitch":
            target = self._twitch
        elif self.kind == "youtube":
            target = self._youtube
        else:
            if self.kind == "kick":
                print("Kick chat is not supported yet; scoring uses audio and speech.")
            return
        self.status = "starting"
        self._thread = threading.Thread(target=self._guard, args=(target,), name="live-chat", daemon=True)
        self._thread.start()

    def _guard(self, target):
        try:
            target()
        except Exception as e:  # never stop the run
            self.status = "failed"
            print(f"Live chat stopped ({type(e).__name__}: {str(e)[:120]}); scoring continues with audio and speech.")

    def add(self, text: str, t: float = None, user: str = ""):
        with self._lock:
            now = self.clock() if t is None else t
            self.msgs.append((now, text, normalize_username(user)))
            cutoff = now - self.keep_s
            self.msgs = [(a, b, c) for a, b, c in self.msgs if a >= cutoff][-10000:]

    def messages(self) -> list:
        with self._lock:
            return [(t, text) for t, text, _user in self.msgs]

    def messages_with_users(self) -> list:
        with self._lock:
            return list(self.msgs)

    @property
    def count(self) -> int:
        return len(self.msgs)

    def stop(self):
        self._stop.set()
        p = self._proc
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()

    # ------------------------------------------------------------------ Twitch
    def _twitch(self):
        chan = twitch_channel(self.url)
        if not chan:
            raise ValueError("no channel name in the Twitch link")
        for attempt in range(3):
            if self._stop.is_set():
                return
            try:
                self._twitch_session(chan)
                return
            except OSError as e:
                if attempt == 2:
                    raise
                print(f"Live chat: Twitch connection dropped ({e}); reconnecting ...")
                self._stop.wait(5 * (attempt + 1))

    def _twitch_session(self, chan: str):
        ctx = ssl.create_default_context()
        with socket.create_connection((TWITCH_HOST, TWITCH_PORT), timeout=30) as raw:
            with ctx.wrap_socket(raw, server_hostname=TWITCH_HOST) as s:
                s.settimeout(2.0)
                nick = f"justinfan{random.randint(10000, 99999)}"
                s.sendall(f"NICK {nick}\r\nJOIN #{chan}\r\n".encode())
                self.status = f"twitch #{chan}"
                print(f"Live chat: reading Twitch chat #{chan} (anonymous, read-only)")
                buf = b""
                last_rx = time.time()
                while not self._stop.is_set():
                    try:
                        data = s.recv(65536)
                    except (socket.timeout, TimeoutError):
                        if time.time() - last_rx > 360:
                            raise OSError("no data from Twitch for 6 minutes") from None
                        continue
                    if not data:
                        raise OSError("Twitch closed the connection")
                    last_rx = time.time()
                    buf += data
                    *lines, buf = buf.split(b"\r\n")
                    for raw_line in lines:
                        kind, val, user = parse_irc_line_full(raw_line.decode("utf-8", errors="replace"))
                        if kind == "ping":
                            s.sendall(f"PONG {val}\r\n".encode())
                        elif kind == "msg":
                            self.add(val, user=user)

    # ------------------------------------------------------------------ YouTube
    def _youtube(self):
        chat_dir = self.out_dir / "chat"
        chat_dir.mkdir(parents=True, exist_ok=True)
        log = open(chat_dir / "yt-dlp.log", "w", encoding="utf-8")
        try:
            self._proc = subprocess.Popen(
                ytdlp_cmd()
                + [
                    "--skip-download",
                    "--write-subs",
                    "--sub-langs",
                    "live_chat",
                    "-o",
                    str(chat_dir / "%(id)s"),
                    self.url,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                **_proc_flags(),
            )
            self.status = "youtube"
            print("Live chat: reading YouTube live chat (yt-dlp)")
            pos = {}
            while not self._stop.wait(2.0):
                for f in chat_dir.glob("*.live_chat.json*"):
                    self._tail(f, pos)
                if self._proc.poll() is not None:
                    for f in chat_dir.glob("*.live_chat.json*"):
                        self._tail(f, pos)
                    if not self.msgs:
                        raise OSError("yt-dlp found no live chat for this stream")
                    return
        finally:
            log.close()

    def _tail(self, f: Path, pos: dict):
        key = f.name.removesuffix(".part")  # yt-dlp renames .part when it finishes; keep the read position
        try:
            with open(f, "rb") as fh:
                fh.seek(pos.get(key, 0))
                data = fh.read()
        except OSError:
            return
        done = data.rfind(b"\n") + 1
        if done <= 0:
            return
        pos[key] = pos.get(key, 0) + done
        for line in data[:done].decode("utf-8", errors="replace").splitlines():
            _t, texts, users = youtube_chat_line_full(line)
            for i, text in enumerate(texts):
                self.add(text, user=users[i] if i < len(users) else "")
