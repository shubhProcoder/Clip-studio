"""Shared helpers: ToolError, subprocess runner, time/box parsing, cache tags, trigger words, ids."""

import hashlib
import importlib.util
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

DRAFT_FPS = 30.0


# Every draft.extra CapCut Desktop writes holds exactly these 26 bytes.
DRAFT_EXTRA_BYTES = bytes.fromhex("69060000006900000000656900000000020000007B7D99C065C3")


COMPANION_TEMPLATES = (
    "draft_agency_config.json",
    "draft_biz_config.json",
    "draft_virtual_store.json",
    "attachment_editing.json",
    "attachment_pc_common.json",
    "key_value.json",
    "performance_opt_info.json",
)


DEFAULT_HOOK_TEXT = "{yellow:WAIT FOR}\n{red:THE END!}"


VIDEO_EXTS = {".mov", ".mp4", ".mkv", ".m4v", ".avi", ".webm", ".ts", ".flv"}


TOOL_HINTS = {
    "ffmpeg": "winget install Gyan.FFmpeg  (or download from https://ffmpeg.org), then reopen the terminal",
    "ffprobe": "winget install Gyan.FFmpeg  (or download from https://ffmpeg.org), then reopen the terminal",
    "yt-dlp": "python -m pip install -U yt-dlp",
    "faster-whisper": "python -m pip install faster-whisper",
    "numpy": "python -m pip install numpy",
    "streamlink": "python -m pip install -U streamlink",
}


class ToolError(Exception):
    """Expected failure with a message for the user and a fix hint (printed without a traceback)."""

    def __init__(self, msg: str, hint: str = ""):
        super().__init__(msg)
        self.hint = hint


class SkipClip(ToolError):
    """A picked clip that cannot be made into a clean video (e.g. the stream shows a blank screen)."""


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ToolError(f"{name} was not found on PATH.", TOOL_HINTS.get(name, f"install {name}"))
    return path


def own_group_flags() -> dict:
    """Popen flags for a child in its own process group: a console Ctrl+C (Ctrl+Break) or a terminal
    SIGINT to the foreground group reaches only streamclipper, which then stops in an orderly way."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


_OWN_GROUP = []  # non-empty: every run_cmd child gets its own process group (live mode)


def isolate_children(on: bool = True) -> None:
    """Start every later run_cmd child (ffmpeg renders, probes) in its own process group (live mode)."""
    _OWN_GROUP.clear()
    if on:
        _OWN_GROUP.append(True)


def run_cmd(
    cmd: list, what: str, cwd=None, capture: bool = True, binary: bool = False, check: bool = True, hint: str = ""
) -> subprocess.CompletedProcess:
    """Run a command with UTF-8 decoding. On failure raise ToolError with the last stderr lines."""
    cmd = [str(c) for c in cmd]
    kw = {"cwd": str(cwd) if cwd else None}
    if _OWN_GROUP:
        kw.update(own_group_flags())
    if capture:
        kw["capture_output"] = True
    if not binary:
        kw.update(text=True, encoding="utf-8", errors="replace")
    try:
        res = subprocess.run(cmd, **kw)
    except FileNotFoundError:
        tool = Path(cmd[0]).stem
        raise ToolError(
            f"{tool} was not found (needed for: {what}).", TOOL_HINTS.get(tool, f"install {tool}")
        ) from None
    if check and res.returncode != 0:
        err = res.stderr if capture else ""
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        tail = "\n".join((err or "").strip().splitlines()[-15:])
        raise ToolError(
            f"{what} failed ({Path(cmd[0]).name} exit code {res.returncode})" + (f":\n{tail}" if tail else ""),
            hint,
        )
    return res


_YTDLP_COOKIES = []  # optional cookies.txt path; only ever passed to yt-dlp --cookies, never printed


def set_ytdlp_cookies(path=None) -> None:
    """Use a cookies.txt for every yt-dlp call of this process (None turns it off)."""
    _YTDLP_COOKIES.clear()
    if path:
        _YTDLP_COOKIES.append(str(path))


def ytdlp_cmd() -> list:
    extra = ["--cookies", _YTDLP_COOKIES[0]] if _YTDLP_COOKIES else []
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"] + extra
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe] + extra
    raise ToolError("yt-dlp is not installed.", TOOL_HINTS["yt-dlp"])


def parse_time(value) -> float:
    """Seconds from '75', '75.5', 'MM:SS' or 'HH:MM:SS'."""
    s = str(value).strip()
    try:
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        raise ToolError(f"Bad time value '{value}'.", "use seconds (75) or [HH:]MM:SS (1:15)") from None
    if not parts or len(parts) > 3 or any(p < 0 for p in parts):
        raise ToolError(f"Bad time value '{value}'.", "use seconds (75) or [HH:]MM:SS (1:15)")
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def parse_box(value) -> tuple:
    """'x,y,w,h' string or 4-item list -> pixel ints (x, y, w, h)."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (list, tuple)):
            nums = [int(round(float(v))) for v in value]
        else:
            nums = [int(round(float(v))) for v in str(value).replace(" ", "").split(",")]
    except (TypeError, ValueError):
        nums = []
    if len(nums) != 4 or nums[0] < 0 or nums[1] < 0 or nums[2] <= 0 or nums[3] <= 0:
        raise ToolError(
            f"Bad crop box '{value}'.", "use pixels of the source video as x,y,w,h, e.g. --cam-crop 0,0,412,260"
        )
    return tuple(nums)


def box_to_norm(box_px: tuple, vw: int, vh: int) -> list:
    x, y, w, h = box_px
    if x >= vw or y >= vh:
        raise ToolError(
            f"Crop box {box_px} is outside the {vw}x{vh} source.", "check the box against the source resolution"
        )
    x1, y1 = min(vw, x + w), min(vh, y + h)
    return [round(x / vw, 5), round(y / vh, 5), round(x1 / vw, 5), round(y1 / vh, 5)]


def file_tag(path: Path, *parts) -> str:
    """Short hash of file size + mtime + extra parts, for cache file names."""
    try:
        st = Path(path).stat()
        base = f"{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        base = str(path)
    raw = base + "|" + "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def fwd(p) -> str:
    return str(p).replace("\\", "/")


# Trigger words, English (stream/gaming slang) first, Hindi second. Single tokens are matched as whole
# words only, so "w", "l" and "gg" never match inside another word.
ENGLISH_POWER_WORDS = (
    "bro",
    "bruh",
    "nah",
    "dude",
    "yo",
    "chat",
    "w",
    "l",
    "gg",
    "insane",
    "run",
    "cops",
    "wasted",
    "busted",
    "wait",
    "wtf",
    "omg",
    "lmao",
    "lol",
    "crazy",
    "never",
    "secret",
    "shocking",
    "dead",
    "clutch",
    "impossible",
    "million",
    "billion",
    "100%",
    "stop",
    "look",
    "watch",
    "no",
    "way",
    "hack",
    "glitch",
    "banned",
    "police",
    "gta",
    "gta6",
    "vi",
    "lucia",
    "jason",
    "heist",
    "boss",
    "kill",
    "win",
    "lost",
    "rip",
    "legend",
    "goat",
    "real",
    "fake",
    "exposed",
    "heavy",
    "attack",
    "parry",
    "dodge",
    "combo",
    "critical",
    "god",
    "solo",
    "freeze",
    "spell",
    "magic",
    "wukong",
    "monkey",
    "king",
    "graphics",
)


HINDI_POWER_WORDS = (
    "bhai",
    "kya",
    "dekho",
    "maar",
    "maro",
    "maara",
    "ladai",
    "pahad",
    "patthar",
    "roka",
    "yaar",
    "abey",
    "bhaag",
    "gaya",
    "khatam",
    "op",
    "bawal",
    "tabahi",
    "zinda",
    "maza",
    "gajab",
    "hatt",
    "bc",
    "arre",
    "भाई",
    "क्या",
    "देखो",
    "मारो",
    "मर",
    "गया",
    "ख़त्म",
    "खत्म",
    "पागल",
    "क्यों",
    "बचाओ",
    "अरे",
    "यार",
    "भागो",
    "ज़िंदा",
)


VIRAL_POWER_WORDS = set(ENGLISH_POWER_WORDS) | set(HINDI_POWER_WORDS)


# Multi-word triggers, matched on the joined word stream. Longer phrases first, so "are you kidding me"
# wins over "are you kidding".
ENGLISH_TRIGGER_PHRASES = (
    "are you kidding me",
    "are you kidding",
    "are you serious",
    "he's cheating",
    "oh my god",
    "wait for it",
    "look at this",
    "plot twist",
    "hot take",
    "what the",
    "no way",
    "let's go",
    "clip that",
    "clip it",
    "i'm dead",
    "one hp",
    "thank you",
    "love you",
)


HINDI_TRIGGER_PHRASES = (
    "mar gaya",
    "bap re",
    "baap re",
    "kya baat",
    "मर गया",
    "बाप रे",
    "क्या बात",
)


TRIGGER_PHRASES = ENGLISH_TRIGGER_PHRASES + HINDI_TRIGGER_PHRASES


# Clip categories (first matching rule wins; "reaction" when nothing matches). Each rule lists trigger
# labels (normalised tokens or phrases) and an optional audio condition: "laughter" (laughter bursts in
# the clip) or "loud" (voice >= 12 dB above the stream's usual level).
CATEGORIES = ("funny", "rage", "clutch", "reaction", "fail", "hot take", "reveal", "wholesome", "chaos")
CATEGORY_RULES = (
    ("funny", ("i'm dead", "lmao", "lol", "haha", "hahaha"), "laughter"),
    ("fail", ("wasted", "busted", "l", "rip", "mar gaya", "मर गया", "khatam", "खत्म"), None),
    ("clutch", ("let's go", "gg", "w", "clutch", "one hp"), None),
    ("rage", ("are you kidding me", "are you kidding", "are you serious", "what the", "wtf", "he's cheating"), "loud"),
    ("chaos", ("cops", "police", "run", "busted", "bhaag", "भागो"), None),
    ("reveal", ("wait", "look at this", "plot twist", "wait for it", "look", "dekho", "देखो"), None),
    ("hot take", ("honestly", "hot take"), None),
    ("wholesome", ("thank you", "love you"), None),
)


_TOKEN_STRIP = re.compile(r"[^\w\u0900-\u097F%$]")


def norm_token(word: str) -> str:
    """Lower-case and strip punctuation, keeping letters, digits, Devanagari marks, % and $."""
    return _TOKEN_STRIP.sub("", str(word).lower())


_PHRASE_TOKENS = [(ph, tuple(norm_token(x) for x in ph.split())) for ph in TRIGGER_PHRASES]


VIRAL_POWER_WORDS = {norm_token(w) for w in VIRAL_POWER_WORDS}


def find_triggers(tokens: list, digits: bool = False) -> list:
    """[(first_index, last_index, label)] for trigger phrases and power words in a list of words."""
    norm = [norm_token(t) for t in tokens]
    hits, used = [], set()
    for label, ph in _PHRASE_TOKENS:
        n = len(ph)
        for i in range(len(norm) - n + 1):
            if tuple(norm[i : i + n]) == ph and not used.intersection(range(i, i + n)):
                hits.append((i, i + n - 1, label))
                used.update(range(i, i + n))
    for i, t in enumerate(norm):
        if i in used or not t:
            continue
        if t in VIRAL_POWER_WORDS or (digits and any(ch.isdigit() for ch in t)):
            hits.append((i, i, t))
    return sorted(hits)


# Talk content (--content talk: podcasts, interviews, just-chatting). Openers start a strong statement
# (the point follows them); reactions answer something that was just said. Single words count less.
TALK_OPENERS = (
    "nobody talks about",
    "nobody's talking about",
    "here's the thing",
    "here is the thing",
    "the truth is",
    "the problem is",
    "the crazy thing is",
    "the craziest thing",
    "i'll be honest",
    "to be honest",
    "let me tell you",
    "i'm telling you",
    "hot take",
    "unpopular opinion",
    "the secret is",
    "the reason is",
    "the worst part",
    "the best part",
)
TALK_REACTIONS = (
    "are you serious",
    "are you kidding me",
    "are you kidding",
    "that's crazy",
    "that's insane",
    "that's wild",
    "oh my god",
    "no way",
    "what the",
    "i can't believe",
)
TALK_WORDS = ("never", "always", "honestly", "literally", "insane", "crazy", "million", "billion", "worst", "best")
_TALK_PHRASES = [(ph, tuple(norm_token(x) for x in ph.split())) for ph in TALK_OPENERS + TALK_REACTIONS]
_MONEY = re.compile(r"^\$?\d[\d,.]*(k|m|%)?$")


def find_talk_triggers(tokens: list) -> list:
    """
    [(first_index, last_index, label, kind)] for talk triggers: kind 'opener' (a statement follows),
    'reaction', 'word' (TALK_WORDS) or 'number' (amounts like 500, $20, 10k, 90%).
    """
    norm = [norm_token(t) for t in tokens]
    hits, used = [], set()
    openers = set(TALK_OPENERS)
    for label, ph in _TALK_PHRASES:
        n = len(ph)
        for i in range(len(norm) - n + 1):
            if tuple(norm[i : i + n]) == ph and not used.intersection(range(i, i + n)):
                hits.append((i, i + n - 1, label, "opener" if label in openers else "reaction"))
                used.update(range(i, i + n))
    words = {norm_token(w) for w in TALK_WORDS}
    for i, t in enumerate(norm):
        if i in used or not t:
            continue
        if t in words:
            hits.append((i, i, t, "word"))
        elif _MONEY.match(t) and (t.startswith("$") or len(t.strip("$,.")) >= 2):
            hits.append((i, i, t, "number"))
    return sorted(hits)


def new_id() -> str:
    return str(uuid.uuid4()).upper()


def sec_to_us(sec: float, fps: float = 60.0) -> int:
    frames = round(float(sec) * fps)
    return int(round(frames * 1_000_000 / fps))


def utf16_len(s: str) -> int:
    """
    CapCut Desktop (C++/Qt on Windows) indexes `styles[].range` by UTF-16 code units (`wchar_t`),
    NOT Python code points! Using `utf16_len` guarantees surrogate pairs never shift style ranges.
    """
    return len(s.encode("utf-16-le")) // 2
