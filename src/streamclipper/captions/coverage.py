"""Caption coverage: which spoken words a rendered clip shows on screen, and the longest uncaptioned speech."""

import re

from streamclipper.captions.capcut import devanagari_to_hinglish

MIN_PCT = 97.0  # FAIL below this share of spoken words shown
MAX_GAP = 0.6  # FAIL when speech goes this long (s) without any caption
SPEECH_JOIN = 0.35  # words closer than this belong to one stretch of speech

_NON_WORD = re.compile(r"[^\w\u0900-\u097F]")
_MASKED = re.compile(r"^(\w)\*+$")


def _norm(tok: str) -> str:
    return _NON_WORD.sub("", str(tok).replace("_", "")).casefold()


def _has_word(tok: str) -> bool:
    return bool(_norm(tok))


def spoken_words_tl(plan: dict) -> list:
    """
    plan["words"] (window seconds) -> [[word, tl_start, tl_end], ...] on the clip timeline, through
    plan["segments"]. A word belongs to the segment that holds its midpoint. Tease segments are skipped (the
    tease shows only the hook headline by design), and so are tokens with no letter or digit ("...", emoji).
    """
    words = plan.get("words") or []
    out = []
    for s in plan.get("segments") or []:
        if s.get("kind") == "tease":
            continue
        a, b, t0 = float(s["src_s"]), float(s["src_e"]), float(s["tl_start"])
        for w in words:
            ws, we = float(w["s"]), float(w["e"])
            mid = (ws + we) / 2
            if a <= mid < b and _has_word(w.get("w", "")):
                out.append([str(w["w"]), round(t0 + max(ws, a) - a, 3), round(t0 + min(we, b) - a, 3)])
    out.sort(key=lambda x: x[1])
    return out


def _matches(word: str, tokens: list) -> bool:
    n = _norm(word)
    alt = _norm(devanagari_to_hinglish(word)) if any("\u0900" <= ch <= "\u097f" for ch in word) else ""
    for raw in tokens:
        t = _norm(raw)
        if t and (t == n or (alt and t == alt)):
            return True
        m = _MASKED.match(re.sub(r"[^\w*]", "", str(raw)))
        if m and n and n[0] == m.group(1).casefold() and len(n) == len(m.group(0)):
            return True  # profanity filter: "f***" stands for a 4-letter word starting with f
    return False


def _merge(iv: list, join: float) -> list:
    out = []
    for s, e in sorted(iv):
        if out and s - out[-1][1] < join:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def coverage(words_tl: list, events: list) -> dict:
    """
    words_tl: [[word, s, e]] spoken words on the timeline; events: [[s, e, text]] word-caption events (hook
    banners excluded). A word counts as shown when an event covering its midpoint contains it (case-folded,
    punctuation ignored; "f***" matches a masked word; Devanagari also matches its roman form).
    Speech = word intervals merged across gaps < 0.35 s; max_gap = the longest stretch of speech with no
    caption event on screen. -> {total, shown, pct, max_gap, gap_at, missing: [[word, t], ...]}
    """
    evs = [(float(s), float(e), str(t or "").split()) for s, e, t in events]
    shown, missing = 0, []
    for w, s, e in words_tl:
        mid = (float(s) + float(e)) / 2
        if any(es <= mid < ee and _matches(w, toks) for es, ee, toks in evs):
            shown += 1
        else:
            missing.append([w, round(mid, 2)])
    total = len(words_tl)
    cover = _merge([[s, e] for s, e, _ in evs if e > s], 0.0)
    max_gap, gap_at = 0.0, None
    for a, b in _merge([[float(s), float(e)] for _, s, e in words_tl], SPEECH_JOIN):
        t = a
        for cs, ce in cover:
            if ce <= t or cs >= b:
                continue
            if cs > t and cs - t > max_gap:
                max_gap, gap_at = cs - t, t
            t = max(t, ce)
            if t >= b:
                break
        if b - t > max_gap:
            max_gap, gap_at = b - t, t
    return {
        "total": total,
        "shown": shown,
        "pct": round(100.0 * shown / total, 1) if total else None,
        "max_gap": round(max_gap, 2),
        "gap_at": round(gap_at, 2) if gap_at is not None else None,
        "missing": missing,
    }


def coverage_row(cap: dict) -> tuple:
    """(status, detail) of the export gate's caption_coverage row from the sidecar's caption facts."""
    words_tl, texts, spans = cap.get("words_tl"), cap.get("texts"), cap.get("spans")
    if words_tl is None or texts is None or spans is None:
        return "N/A", "sidecar written before the caption coverage check (re-render to check)"
    if not words_tl:
        return "N/A", "no spoken words in this clip"
    events = [[sp[0], sp[1], t] for sp, t in zip(spans, texts, strict=False) if t is not None]
    r = coverage(words_tl, events)
    detail = f"{r['pct']}% of {r['total']} spoken words shown, longest gap {r['max_gap']:.2f} s"
    bad = r["pct"] < MIN_PCT or r["max_gap"] > MAX_GAP
    if bad:
        if r["max_gap"] > MAX_GAP:
            detail += f" at {r['gap_at']:.2f} s"
        if r["missing"]:
            detail += "; missing: " + ", ".join(f"{w}@{t:.2f}" for w, t in r["missing"][:5])
    return ("FAIL" if bad else "PASS"), detail
