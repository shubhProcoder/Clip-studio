"""Chat replay signal: parsers (YouTube live_chat, Twitch rechat, own JSONL), rate spikes, hype words."""

import json
import re
from pathlib import Path

from streamclipper.audio import _np
from streamclipper.util import ToolError, norm_token

# Emotes, laugh and hype words that chat spams on a clip-worthy moment (English first, Hindi second).
HYPE_TOKENS = {
    "kekw",
    "lul",
    "lulw",
    "omegalul",
    "pog",
    "poggers",
    "pogchamp",
    "w",
    "ww",
    "www",
    "l",
    "lmao",
    "lmfao",
    "lol",
    "lool",
    "rofl",
    "gg",
    "clip",
    "insane",
    "bruh",
    "omg",
    "wtf",
    "dead",
    "bhai",
    "op",
    "bawal",
    "gajab",
    "भाई",
    "गजब",
}
HYPE_PHRASES = ("clip it", "clip that", "no way", "let's go", "lets go")
HYPE_EMOJI = ("😂", "🤣", "💀", "😭", "🔥", "😱", "😳")


def _yt_runs_text(runs: list) -> str:
    out = []
    for r in runs or []:
        if "text" in r:
            out.append(str(r["text"]))
        elif "emoji" in r:
            em = r["emoji"] or {}
            if em.get("isCustomEmoji"):
                sc = (em.get("shortcuts") or [""])[0]
                out.append(str(sc).strip(":"))
            else:
                out.append(str(em.get("emojiId") or ""))
    return "".join(out)


def youtube_chat_line(line: str) -> tuple:
    """One line of a yt-dlp live_chat.json -> (t seconds or None, [message texts]). Bad lines -> (None, [])."""
    line = line.strip()
    if not line:
        return None, []
    try:
        obj = json.loads(line)
    except ValueError:
        return None, []
    if not isinstance(obj, dict):
        return None, []
    act = obj.get("replayChatItemAction") or {}
    try:
        t = int(act.get("videoOffsetTimeMsec", obj.get("videoOffsetTimeMsec"))) / 1000.0
    except (TypeError, ValueError):
        t = None
    texts = []
    for a in act.get("actions") or []:
        item = (a.get("addChatItemAction") or {}).get("item") or {}
        for kind in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer"):
            r = item.get(kind)
            if r:
                texts.append(_yt_runs_text((r.get("message") or {}).get("runs")))
    return t, texts


def _yt_author(renderer: dict) -> str:
    for key in ("authorExternalChannelId", "authorName", "authorId"):
        value = renderer.get(key)
        if isinstance(value, dict):
            value = value.get("simpleText") or value.get("text")
        if value:
            return str(value)
    return ""


def youtube_chat_line_full(line: str) -> tuple:
    """(timestamp, [texts], [normalized usernames or None]) for one YouTube replay line."""
    line = line.strip()
    if not line:
        return None, [], []
    try:
        obj = json.loads(line)
    except ValueError:
        return None, [], []
    if not isinstance(obj, dict):
        return None, [], []
    act = obj.get("replayChatItemAction") or {}
    try:
        t = int(act.get("videoOffsetTimeMsec", obj.get("videoOffsetTimeMsec"))) / 1000.0
    except (TypeError, ValueError):
        t = None
    texts, users = [], []
    for a in act.get("actions") or []:
        item = (a.get("addChatItemAction") or {}).get("item") or {}
        for kind in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer"):
            r = item.get(kind)
            if r:
                texts.append(_yt_runs_text((r.get("message") or {}).get("runs")))
                users.append(normalize_username(_yt_author(r)))
    return t, texts, users


def parse_youtube_live_chat(path: Path) -> list:
    """yt-dlp live_chat.json (one JSON object per line, replayChatItemAction) -> [(t seconds, text)]."""
    msgs = []
    for line in Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines():
        t, texts = youtube_chat_line(line)
        if t is not None:
            msgs.extend((t, text) for text in texts)
    return sorted(msgs, key=lambda m: m[0])


def parse_youtube_live_chat_users(path: Path) -> list:
    """Identity-aware YouTube replay rows -> [(t, text, normalized_user)]."""
    msgs = []
    for line in Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines():
        t, texts, users = youtube_chat_line_full(line)
        if t is not None:
            for i, text in enumerate(texts):
                msgs.append((t, text, users[i] if i < len(users) else ""))
    return sorted(msgs, key=lambda m: m[0])


def _twitch_items(obj):
    """Walk any Twitch chat JSON layout and yield (offset, text)."""
    if isinstance(obj, list):
        for x in obj:
            yield from _twitch_items(x)
        return
    if not isinstance(obj, dict):
        return
    node = obj.get("node") if isinstance(obj.get("node"), dict) else obj
    off = node.get("content_offset_seconds", node.get("contentOffsetSeconds"))
    msg = node.get("message")
    if off is not None and isinstance(msg, dict):
        text = msg.get("body")
        if text is None:
            text = "".join(str(f.get("text", "")) for f in msg.get("fragments") or [] if isinstance(f, dict))
        try:
            yield float(off), str(text)
        except (TypeError, ValueError):
            pass
        return
    for v in obj.values():
        if isinstance(v, (list, dict)):
            yield from _twitch_items(v)


def parse_twitch_rechat(path: Path) -> list:
    """Twitch VOD chat JSON (yt-dlp rechat, v5 'comments', GQL 'edges/node' or TwitchDownloader) -> [(t, text)]."""
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace").strip()
    try:
        objs = [json.loads(raw)]
    except ValueError:  # one JSON document per line
        objs = []
        for line in raw.splitlines():
            try:
                objs.append(json.loads(line))
            except ValueError:
                continue
    return sorted((m for o in objs for m in _twitch_items(o)), key=lambda m: m[0])


def _twitch_items_with_users(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _twitch_items_with_users(x)
        return
    if not isinstance(obj, dict):
        return
    node = obj.get("node") if isinstance(obj.get("node"), dict) else obj
    off = node.get("content_offset_seconds", node.get("contentOffsetSeconds"))
    msg = node.get("message")
    author = (
        node.get("author")
        or node.get("display_name")
        or node.get("displayName")
        or node.get("user")
        or node.get("username")
        or (msg.get("user") if isinstance(msg, dict) else "")
    )
    if off is not None and isinstance(msg, dict):
        text = msg.get("body")
        if text is None:
            text = "".join(str(f.get("text", "")) for f in msg.get("fragments") or [] if isinstance(f, dict))
        try:
            yield float(off), str(text), normalize_username(author)
        except (TypeError, ValueError):
            pass
        return
    for v in obj.values():
        if isinstance(v, (list, dict)):
            yield from _twitch_items_with_users(v)


def parse_twitch_rechat_users(path: Path) -> list:
    """Identity-aware Twitch replay rows; missing usernames remain anonymous."""
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace").strip()
    try:
        objs = [json.loads(raw)]
    except ValueError:
        objs = []
        for line in raw.splitlines():
            try:
                objs.append(json.loads(line))
            except ValueError:
                continue
    return sorted((m for o in objs for m in _twitch_items_with_users(o)), key=lambda m: m[0])


def parse_jsonl(path: Path) -> list:
    """Own format: one {"t": seconds, "text": "..."} per line -> [(t, text)]."""
    msgs = []
    for line in Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            msgs.append((float(obj["t"]), str(obj.get("text", ""))))
        except (ValueError, KeyError, TypeError):
            continue
    return sorted(msgs, key=lambda m: m[0])


def parse_jsonl_users(path: Path) -> list:
    """Own JSONL format with optional user/username/author/id fields."""
    msgs = []
    for line in Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            user = obj.get("user") or obj.get("username") or obj.get("author") or obj.get("name") or obj.get("id")
            msgs.append((float(obj["t"]), str(obj.get("text", "")), normalize_username(user)))
        except (ValueError, KeyError, TypeError):
            continue
    return sorted(msgs, key=lambda m: m[0])


def load_chat(path: Path) -> list:
    """Detect the format of a chat file and parse it. Raises ToolError when nothing can be read."""
    path = Path(path)
    if not path.is_file():
        raise ToolError(f"Chat file not found: {path}", "check the --chat path")
    head = path.read_text(encoding="utf-8-sig", errors="replace")[:4000]
    if "replayChatItemAction" in head:
        msgs = parse_youtube_live_chat(path)
    elif "content_offset_seconds" in head or "contentOffsetSeconds" in head:
        msgs = parse_twitch_rechat(path)
    else:
        msgs = parse_jsonl(path)
    if not msgs:
        raise ToolError(
            f"No chat messages found in {path.name}.",
            'use a yt-dlp live_chat.json, a Twitch rechat JSON, or JSON lines like {"t": 12.5, "text": "W"}',
        )
    return msgs


def load_chat_users(path: Path) -> list:
    """Identity-aware chat loader; returns legacy 2-item rows for formats with no names."""
    path = Path(path)
    if not path.is_file():
        raise ToolError(f"Chat file not found: {path}", "check the --chat path")
    head = path.read_text(encoding="utf-8-sig", errors="replace")[:4000]
    if "replayChatItemAction" in head:
        msgs = parse_youtube_live_chat_users(path)
    elif "content_offset_seconds" in head or "contentOffsetSeconds" in head:
        msgs = parse_twitch_rechat_users(path)
    else:
        msgs = parse_jsonl_users(path)
    if not msgs:
        raise ToolError(
            f"No chat messages found in {path.name}.",
            'use a yt-dlp live_chat.json, a Twitch rechat JSON, or JSON lines like {"t": 12.5, "text": "W"}',
        )
    return msgs


def hype_count(text: str) -> int:
    """Hype keywords/emotes/emoji in one message (each kind counted once)."""
    low = str(text).lower()
    toks = {norm_token(t) for t in low.split()}
    n = len(toks & HYPE_TOKENS)
    n += sum(1 for ph in HYPE_PHRASES if ph in low)
    n += sum(1 for em in HYPE_EMOJI if em in low)
    return n


def normalize_username(value) -> str:
    """Stable lowercase username/ID for unique-viewer counts; missing names return ''."""
    text = str(value or "").strip().lower()
    text = text.lstrip("@")
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^a-z0-9_.:-]", "", text)
    return text[:80]


def _chat_rows(msgs):
    """Accept legacy (t,text) or identity-aware (t,text,user) messages."""
    rows = []
    for row in msgs or []:
        if len(row) >= 3:
            t, text, user = row[:3]
        elif len(row) >= 2:
            t, text = row[:2]
            user = None
        else:
            continue
        try:
            rows.append((float(t), str(text), normalize_username(user)))
        except (TypeError, ValueError):
            continue
    return rows


def chat_signal(msgs: list, dur: float) -> dict:
    """
    1 s bins: rate (messages/s), hype (messages with a hype word), the 5 s smoothed rate, its rolling
    120 s median as the baseline (never below 0.2 msg/s) and the ratio smoothed rate / baseline.
    """
    np = _np()
    n = max(1, int(np.ceil(dur)))
    rate, hype, unique, repeated = np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n)
    identified = np.zeros(n, dtype=bool)
    for t, text, user in _chat_rows(msgs):
        i = int(t)
        if 0 <= i < n:
            rate[i] += 1
            if hype_count(text):
                hype[i] += 1
            if user:
                identified[i] = True
    users_by_second = {}
    if identified.any():
        for t, _text, user in _chat_rows(msgs):
            i = int(t)
            if 0 <= i < n and user:
                users_by_second.setdefault(i, set()).add(user)
        for i, users in users_by_second.items():
            unique[i] = len(users)
            repeated[i] = max(0.0, 1.0 - len(users) / max(1.0, rate[i]))
    k = np.ones(5) / 5.0
    smooth = np.convolve(rate, k, mode="same")
    hype_s = np.convolve(hype, k, mode="same")
    unique_smooth = np.convolve(unique, k, mode="same")
    spam = np.clip(repeated / 0.5, 0.0, 1.0)
    w = min(120, n)
    padded = np.pad(smooth, (w // 2, w - 1 - w // 2), mode="edge")
    base = np.maximum(np.median(np.lib.stride_tricks.sliding_window_view(padded, w), axis=1), 0.2)
    unique_padded = np.pad(unique_smooth, (w // 2, w - 1 - w // 2), mode="edge")
    unique_base = np.maximum(
        np.median(np.lib.stride_tricks.sliding_window_view(unique_padded, w), axis=1),
        0.2,
    )
    unique_ratio = unique_smooth / unique_base if identified.any() else np.ones(n)
    acceleration = np.maximum(0.0, unique_ratio - 1.0)
    return {
        "rate": rate,
        "hype": hype,
        "base": base,
        "smooth": smooth,
        "hype_s": hype_s,
        "ratio": smooth / base,
        "unique": unique,
        "unique_smooth": unique_smooth,
        "unique_base": unique_base,
        "unique_ratio": unique_ratio,
        "acceleration": acceleration,
        "repeated": repeated,
        "spam": spam,
        "identified": bool(identified.any()),
    }


def spikes(sig: dict, min_ratio: float = 3.0, min_rate: float = 0.5, min_sep: float = 20.0) -> list:
    """Chat spikes: maxima of the smoothed ratio >= min_ratio with >= min_rate msg/s, min_sep s apart."""
    np = _np()
    ratio = sig["unique_ratio"] if sig.get("identified") else sig["ratio"]
    smooth = sig["unique_smooth"] if sig.get("identified") else sig["smooth"]
    out = []
    for i in np.argsort(-ratio, kind="stable").tolist():
        if ratio[i] < min_ratio:
            break
        if smooth[i] < min_rate or any(abs(i - s["t"]) < min_sep for s in out):
            continue
        spam = float(sig["spam"][i]) if "spam" in sig else 0.0
        if spam >= 0.8:
            continue
        hype = float(sig["hype_s"][i] / max(sig["smooth"][i], 1e-6))
        out.append(
            {
                "t": float(i),
                "ratio": round(float(ratio[i]), 2),
                "hype": round(min(1.0, hype), 2),
                "unique": round(float(sig["unique_smooth"][i]), 2) if sig.get("identified") else None,
                "spam": round(spam, 2),
            }
        )
    return sorted(out, key=lambda s: s["t"])


def window_score(sig: dict, p: float, before: float = 5.0, after: float = 15.0) -> dict:
    """
    Chat reaction to a payoff at p (chat lags speech, so the window is [p-5, p+15]):
    score 0..1 = rate jump (ratio 2 -> 0, ratio 6 -> 1) x (0.7 + 0.3 x hype share of the messages).
    """
    ratio, smooth, hype_s = sig["ratio"], sig["smooth"], sig["hype_s"]
    a, b = max(0, int(p - before)), min(len(ratio), int(p + after) + 1)
    if b <= a:
        return {"score": 0.0, "ratio": 1.0}
    if sig.get("identified"):
        combined = (0.6 * sig["unique_ratio"][a:b] + 0.4 * ratio[a:b])
        combined = combined * (1.0 - 0.7 * sig["spam"][a:b])
        i = a + int(combined.argmax())
        ur = float(sig["unique_ratio"][i])
        mr = float(ratio[i])
        u_strength = max(0.0, min(1.0, (ur - 2.0) / 4.0))
        m_strength = max(0.0, min(1.0, (mr - 2.0) / 4.0))
        strength = 0.6 * u_strength + 0.4 * m_strength
        hype = min(1.0, float(hype_s[i] / max(smooth[i], 1e-6))) if smooth[i] > 0 else 0.0
        spam = float(max(sig["spam"][a:b])) if "spam" in sig else 0.0
        score = strength * (0.7 + 0.3 * hype) * (1.0 - 0.7 * spam)
        return {
            "score": round(max(0.0, min(1.0, score)), 3),
            "ratio": round(mr, 2),
            "unique_ratio": round(ur, 2),
            "spam": round(spam, 2),
        }
    seg = ratio[a:b]
    i = a + int(seg.argmax())
    r = float(ratio[i])
    hype = min(1.0, float(hype_s[i] / max(smooth[i], 1e-6))) if smooth[i] > 0 else 0.0
    score = max(0.0, min(1.0, (r - 2.0) / 4.0)) * (0.7 + 0.3 * hype)
    return {"score": round(score, 3), "ratio": round(r, 2)}
