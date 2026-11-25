"""Post text per platform from a clips.json entry: title, description, hashtags, with each platform's limits."""

import re
from pathlib import Path

from streamclipper.publish.store import PublishError

YT_TITLE_MAX = 100  # characters
YT_DESCRIPTION_MAX = 5000  # bytes (UTF-8)
YT_TAGS_MAX = 500  # characters over all tags
IG_CAPTION_MAX = 2200
IG_HASHTAGS_MAX = 30
TIKTOK_CAPTION_MAX = 2200
SHORTS_TAG = "#Shorts"

_TAG_RE = re.compile(r"<[^>]*>")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(text) -> str:
    """Plain text: no HTML tags, no < or > (YouTube refuses them), no control characters, tidy spaces."""
    s = _TAG_RE.sub("", str(text or ""))
    s = _CTRL_RE.sub("", s.replace("<", "").replace(">", ""))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def hashtags(raw) -> list:
    """'#tag' list from a list or a space/comma separated string; letters, digits and _ only; no duplicates."""
    if isinstance(raw, str):
        raw = re.split(r"[\s,]+", raw)
    out, seen = [], set()
    for t in raw or []:
        body = re.sub(r"\W+", "", str(t).lstrip("#"))
        if body and body.lower() not in seen:
            seen.add(body.lower())
            out.append("#" + body)
    return out


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 3].rstrip()
    sp = cut.rfind(" ")
    if sp > limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,.;:-") + "..."


def _cut_bytes(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    if limit < 4:
        return ""
    return raw[: limit - 3].decode("utf-8", errors="ignore").rstrip() + "..."


def clip_meta(clip: dict, title=None, description=None, tags=None) -> dict:
    """{title, description, hashtags} from a clips.json entry; non-empty arguments override it."""
    clip = clip or {}
    t = clean_text(title if title else clip.get("title") or clip.get("hook_headline") or "")
    d = clean_text(description if description else clip.get("description") or "")
    h = hashtags(tags if tags else clip.get("hashtags") or [])
    return {"title": t or "Stream clip", "description": d, "hashtags": h}


def youtube_meta(base: dict) -> dict:
    """snippet fields: title <= 100 chars, description with #Shorts <= 5000 bytes, tags <= 500 chars."""
    tags_h = list(base.get("hashtags") or [])
    if not any(t.lower() == SHORTS_TAG.lower() for t in tags_h):
        tags_h.insert(0, SHORTS_TAG)
    line = " ".join(tags_h)
    desc = clean_text(base.get("description") or "")
    room = YT_DESCRIPTION_MAX - len(line.encode("utf-8")) - 2
    desc = _cut_bytes(desc, max(0, room))
    description = f"{desc}\n\n{line}" if desc else line
    tags, total = [], 0
    for t in tags_h:
        word = t.lstrip("#")
        cost = len(word) + (1 if tags else 0)
        if total + cost > YT_TAGS_MAX:
            break
        tags.append(word)
        total += cost
    return {
        "title": _cut(clean_text(base.get("title") or "") or "Stream clip", YT_TITLE_MAX),
        "description": description,
        "tags": tags,
    }


def _caption(base: dict, limit: int, max_tags: int) -> str:
    tags = list(base.get("hashtags") or [])[:max_tags]
    line = " ".join(tags)
    head = "\n\n".join(x for x in (base.get("title"), base.get("description")) if x)
    room = limit - len(line) - (2 if line else 0)
    head = _cut(head, max(0, room)) if room > 0 else ""
    return "\n\n".join(x for x in (head, line) if x)


def instagram_caption(base: dict) -> str:
    """Title, description and at most 30 hashtags in at most 2,200 characters."""
    return _caption(base, IG_CAPTION_MAX, IG_HASHTAGS_MAX)


def tiktok_caption(base: dict) -> str:
    return _caption(base, TIKTOK_CAPTION_MAX, IG_HASHTAGS_MAX)


def caption_meta(caption: str) -> dict:
    """A user-written caption as base meta (hashtags at the end stay where they are)."""
    return {"title": "", "description": clean_text(caption), "hashtags": []}


def _load_doc(clips_json):
    import json

    p = Path(clips_json).resolve()
    if not p.is_file():
        raise PublishError(f"clips.json not found: {p}", "pass the clips.json written by clip-stream or render")
    try:
        doc = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise PublishError(f"{p.name} is not valid JSON.", "pass the clips.json written by clip-stream") from None
    clips = doc.get("clips") if isinstance(doc, dict) else None
    return p, sorted([c for c in clips or [] if isinstance(c, dict)], key=lambda c: c.get("rank") or 0)


def load_clip(clips_json, rank: int = 1):
    """(clip dict, MP4 path) of the clip with this rank in clips.json; the MP4 must exist."""
    p, clips = _load_doc(clips_json)
    for c in clips:
        if int(c.get("rank") or 0) == int(rank):
            if not c.get("mp4"):
                raise PublishError(f"Clip {rank} has no MP4.", "render it first (clip-stream --render, or render)")
            mp4 = (p.parent / str(c["mp4"])).resolve()
            if not mp4.is_file():
                raise PublishError(f"MP4 of clip {rank} not found: {mp4.name}", "render the clip again")
            return c, mp4
    raise PublishError(f"No clip with rank {rank} in {p.name}.", "use a rank from the clips table")


def all_clips(clips_json) -> list:
    """[(clip, mp4)] of every clip in clips.json that has a rendered MP4, best rank first."""
    p, clips = _load_doc(clips_json)
    res = []
    for c in clips:
        mp4 = (p.parent / str(c["mp4"])).resolve() if c.get("mp4") else None
        if mp4 and mp4.is_file():
            res.append((c, mp4))
    return res
