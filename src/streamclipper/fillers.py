"""
Filler-word removal (opt-in, --remove-fillers): cut "um", "uh", filler "like," and filler "you know" out of the
clip pieces between Whisper words, so the MP4, the captions and the CapCut draft all use the same cuts.
A cut never starts or ends inside a kept word; the payoff zone, the first 2 s of the clip and a filler before
a dramatic pause are left alone.
"""

import re

FILLER_WORDS = {"um", "uh", "uhh", "umm", "erm", "er", "hmm"}
# 'like' after these is a verb or part of a phrase ('I like', 'looks like', 'feel like'), never a filler
LIKE_KEEP_AFTER = {"i", "you", "we", "they", "would", "look", "looks", "feel", "feels", "just"}
YOU_KNOW_KEEP_AFTER = {"do", "did", "don't", "dont", "if"}
EDGE = 0.02  # a cut starts at least this long after the previous word and ends this long before the next
PAD = 0.12  # ... and takes at most this much of the pause on each side (longer pauses keep their beat)
MIN_CUT = 0.06  # a shorter cut is not worth it
LIKE_PAUSE = 0.15
YOU_KNOW_PAUSE = 0.25
BEAT_PAUSE = 0.8  # a filler followed by a pause this long is a dramatic beat: kept
MAX_CUTS = 8
MIN_PIECE = 0.3  # parts shorter than this after a cut are dropped
HOOK_PROTECT_S = 2.0
PAYOFF_BEFORE, PAYOFF_AFTER = 3.0, 1.5  # the protect tuple trim_pauses uses


def _norm(w: str) -> str:
    return re.sub(r"[^\w']", "", str(w).lower())


def _gap_before(words: list, i: int) -> float:
    return float(words[i]["s"]) - float(words[i - 1]["e"]) if i > 0 else 9.0


def _gap_after(words: list, i: int) -> float:
    return float(words[i + 1]["s"]) - float(words[i]["e"]) if i + 1 < len(words) else 9.0


def _filler_runs(words: list) -> list:
    """[(first index, last index)] of filler words/phrases in a sorted word list."""
    runs, i = [], 0
    while i < len(words):
        n = _norm(words[i]["w"])
        raw = str(words[i]["w"]).strip()
        prev = _norm(words[i - 1]["w"]) if i > 0 else ""
        if n in FILLER_WORDS:
            runs.append((i, i))
        elif n == "like" and prev not in LIKE_KEEP_AFTER:
            between_pauses = _gap_before(words, i) >= LIKE_PAUSE and _gap_after(words, i) >= LIKE_PAUSE
            if raw.endswith(",") or between_pauses:
                runs.append((i, i))
        elif n == "you" and i + 1 < len(words) and _norm(words[i + 1]["w"]) == "know":
            k_raw = str(words[i + 1]["w"]).strip()
            if prev not in YOU_KNOW_KEEP_AFTER and (k_raw.endswith(",") or _gap_after(words, i + 1) >= YOU_KNOW_PAUSE):
                runs.append((i, i + 1))
                i += 2
                continue
        i += 1
    return runs


def filler_spans(words: list, protect: list = None, max_cuts: int = MAX_CUTS) -> list:
    """
    Cut spans [(a, b, text)] (same time base as words) for the fillers in words [{w, s, e}]. A span covers the
    filler plus up to PAD of the pause on each side, and never reaches past the previous word's end + EDGE or
    the next word's start - EDGE, so it never cuts into a kept word. Fillers whose span touches a protected
    zone [(a, b), ...] or that come before a pause >= BEAT_PAUSE are kept; at most max_cuts.
    """
    words = sorted((w for w in words or [] if w.get("s") is not None), key=lambda w: float(w["s"]))
    out = []
    for i, j in _filler_runs(words):
        ws, we = float(words[i]["s"]), float(words[j]["e"])
        if _gap_after(words, j) >= BEAT_PAUSE:
            continue
        a = max(float(words[i - 1]["e"]) + EDGE, ws - PAD) if i > 0 else ws - EDGE
        b = min(float(words[j + 1]["s"]) - EDGE, we + PAD) if j + 1 < len(words) else we + EDGE
        a, b = min(a, ws), max(b, we)  # Whisper words can touch: the span still covers the whole filler
        if i > 0:
            a = max(a, float(words[i - 1]["e"]))
        if j + 1 < len(words):
            b = min(b, float(words[j + 1]["s"]))
        if b - a < MIN_CUT or any(b > za and a < zb for za, zb in protect or []):
            continue
        out.append((round(a, 3), round(b, 3), " ".join(str(w["w"]).strip() for w in words[i : j + 1])))
        if len(out) >= max_cuts:
            break
    return out


def protect_zones(segs: list, peak: float = None) -> list:
    """Source-time zones where fillers stay: the payoff zone and what the first HOOK_PROTECT_S s of the clip show."""
    zones = []
    if peak is not None:
        zones.append((float(peak) - PAYOFF_BEFORE, float(peak) + PAYOFF_AFTER))
    tl = 0.0
    for s in segs:
        a, b = float(s["cut_start"]), float(s["cut_end"])
        if tl >= HOOK_PROTECT_S:
            break
        zones.append((a, min(b, a + HOOK_PROTECT_S - tl)))
        tl += b - a
    return zones


def cut_fillers(segs: list, spans: list) -> list:
    """
    Pieces (cut_start/cut_end/words in source time) with the filler spans cut out, like layout.cut_black.
    Tease pieces are not touched; parts shorter than MIN_PIECE are dropped; the part after a cut is marked
    after_filler (no jump-cut pop there). Words are kept by their midpoint.
    """
    out = []
    for pc in segs:
        if pc.get("kind") == "tease":
            out.append(pc)
            continue
        parts = [(float(pc["cut_start"]), float(pc["cut_end"]), False)]
        for a, b, _ in spans:
            nxt = []
            for s, e, cut in parts:
                if b <= s or e <= a:
                    nxt.append((s, e, cut))
                    continue
                if a > s:
                    nxt.append((s, a, cut))
                if b < e:
                    nxt.append((b, e, True))
            parts = nxt
        for s, e, cut in parts:
            if e - s < MIN_PIECE:
                continue
            ws = [w for w in pc.get("words") or [] if s <= (float(w["s"]) + float(w["e"])) / 2 < e]
            part = dict(pc, cut_start=s, cut_end=e, words=ws)
            if cut:
                part["after_filler"] = True
            out.append(part)
    return out


def snap_spans(spans: list, words: list, origin: float, fps: int = 30) -> list:
    """
    Cut spans moved inwards onto the frame grid the renderer cuts on (origin + k/fps): the start up, the end
    down, so the rounding of the cut list can never move a cut into the kept word next to it. Spans that get
    shorter than MIN_CUT are dropped.
    """
    import math

    out = []
    for a, b, text in spans:
        a2 = origin + math.ceil((a - origin) * fps - 1e-6) / fps
        b2 = origin + math.floor((b - origin) * fps + 1e-6) / fps
        if b2 - a2 < MIN_CUT:
            continue
        out.append((round(a2, 4), round(b2, 4), text))
    return out


def remove_fillers(segs: list, words: list, peak: float = None, origin: float = None) -> tuple:
    """(new pieces, removed [{w, s, e}]) for one clip; the pieces are unchanged when nothing is cut.
    origin: where the renderer's 1/30 s frame grid starts (the clip window start); the cuts are snapped onto it."""
    spans = filler_spans(words, protect_zones(segs, peak))
    if origin is not None:
        spans = snap_spans(spans, words, origin)
    if not spans:
        return segs, []
    story = [(float(s["cut_start"]), float(s["cut_end"])) for s in segs if s.get("kind") != "tease"]
    spans = [sp for sp in spans if any(sp[1] > a and sp[0] < b for a, b in story)]
    new = cut_fillers(segs, spans)
    removed = [w for w in words if any(a <= (float(w["s"]) + float(w["e"])) / 2 < b for a, b, _ in spans)]
    return new, removed
