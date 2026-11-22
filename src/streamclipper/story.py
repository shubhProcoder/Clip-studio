"""Sentence boundaries, story windows, pause trimming and hook text."""

import math

from streamclipper.audio import _q
from streamclipper.util import TRIGGER_PHRASES, find_triggers

SENTENCE_END = (".", "?", "!", "।", "॥")


def sentence_boundaries(words: list, islands: list, dur: float) -> list:
    """
    Points where a clip may start or end, as pauses {gs, ge, pause, kind}: gs = speech stops,
    ge = speech starts again. Sources: word ends with . ? ! । or followed by a gap >= 0.6 s
    (kind 'sentence'), and silence gaps >= 0.35 s between speech islands (kind 'silence').
    """
    out = []
    prev = 0.0
    for isl in islands:
        gap = isl["speech_start"] - prev
        if gap >= 0.35:
            out.append({"gs": prev, "ge": isl["speech_start"], "pause": round(gap, 3), "kind": "silence"})
        prev = max(prev, isl["speech_end"])
    if dur - prev >= 0.35:
        out.append({"gs": prev, "ge": dur, "pause": round(dur - prev, 3), "kind": "silence"})
    for i, w in enumerate(words):
        nxt = words[i + 1]["s"] if i + 1 < len(words) else dur
        gap = max(0.0, nxt - w["e"])
        if gap > 5.0:  # next word is in another transcribed region; the islands describe this gap
            continue
        if str(w["w"]).rstrip("\"')]").endswith(SENTENCE_END) or gap >= 0.6:
            out.append({"gs": w["e"], "ge": max(w["e"], nxt), "pause": round(gap, 3), "kind": "sentence"})
    out.sort(key=lambda b: b["ge"])
    return out


def _inside_word(t: float, words: list):
    for w in words:
        if w["s"] < t < w["e"]:
            return w
    return None


WORD_PAD = 0.08
_GRID = 30.0


def off_word(t: float, words: list, direction: int, pad: float = WORD_PAD) -> float:
    """
    Cut point t moved out of the word it falls inside: direction -1 (a start, or an end pulled back)
    goes to the word start - pad, never into the previous word; +1 (an end, or a start pushed forward)
    goes to the word end + pad, never into the next word. t outside every word is returned as is.
    """
    w = _inside_word(t, words)
    if w is None:
        return t
    if direction < 0:
        prev = max((x["e"] for x in words if x["e"] <= w["s"]), default=None)
        return w["s"] - pad if prev is None else max(w["s"] - pad, prev)
    nxt = min((x["s"] for x in words if x["s"] >= w["e"]), default=None)
    return w["e"] + pad if nxt is None else min(w["e"] + pad, nxt)


def grid_off_word(t: float, words: list, direction: int) -> float:
    """off_word, then onto the 1/30 s grid (floor for -1, ceil for +1; the other way when that lands in a word)."""
    k = off_word(t, words, direction) * _GRID
    down, up = math.floor(k + 1e-6) / _GRID, math.ceil(k - 1e-6) / _GRID
    a, b = (down, up) if direction < 0 else (up, down)
    return round(b if _inside_word(a, words) and not _inside_word(b, words) else a, 4)


def snap_segments(segs: list, words: list, lo: float = None, hi: float = None) -> list:
    """
    Every segment start/end that lies inside a Whisper word moved to that word's outer edge (+0.08 s),
    on the 1/30 s grid, within [lo, hi]; pieces that then touch or overlap are merged.
    """
    if not words:
        return segs
    out = []
    for s in segs:
        a = grid_off_word(s["src_s"], words, -1)
        b = grid_off_word(s["src_e"], words, +1)
        if lo is not None:
            a = max(a, lo)
        if hi is not None:
            b = min(b, hi)
        if out and a <= out[-1]["src_e"] + 1e-6:
            prev = out[-1]
            prev["src_e"] = max(prev["src_e"], b)
            if s.get("kind") == "payoff":
                prev["kind"] = "payoff"
            continue
        out.append({**s, "src_s": a, "src_e": b})
    return out


SNAP_START_S = 4.0  # an LLM start this close to a transcript line start snaps onto it
SNAP_TAIL_S = 0.35  # a sentence-ending clip end gets a short tail so the last word is not clipped


def _snap(start_t: float, end_t: float, words: list, min_len: float = 15.0, max_len: float = 45.0) -> tuple:
    """
    The model's (start_t, end_t) moved onto the transcript, so its story survives the audio rules:
    START snaps to the nearest line start (a sentence end or a pause >= 0.6 s) within +-SNAP_START_S,
    else the whole-word edge. END is the first sentence-ending punctuation inside [start + min_len,
    start + max_len] plus SNAP_TAIL_S; when the model's window holds no sentence end, end_t is kept and
    moved onto whole-word edges. Returns (start, end).
    """
    start_t, end_t = float(start_t), float(end_t)
    if not words:
        return start_t, end_t
    starts = []
    for i, w in enumerate(words):
        if i == 0:
            starts.append(float(w["s"]))
            continue
        prev = words[i - 1]
        if str(prev["w"]).rstrip("\"')]").endswith(SENTENCE_END) or w["s"] - prev["e"] >= 0.6:
            starts.append(float(w["s"]))
    near = [s for s in starts if abs(s - start_t) <= SNAP_START_S]
    start = max(0.0, min(near, key=lambda s: abs(s - start_t))) if near else off_word(start_t, words, -1)
    lo, hi = start + min_len, max(start + min_len, start + max_len)
    ends = [
        float(w["e"]) + SNAP_TAIL_S
        for w in words
        if str(w["w"]).rstrip("\"')]").endswith(SENTENCE_END) and lo <= float(w["e"]) + SNAP_TAIL_S <= hi
    ]
    if ends:
        end = min(ends)
    else:
        end = off_word(min(max(end_t, lo), hi), words, +1)
    return round(start, 3), round(max(end, start + min_len), 3)


def story_window(
    p: float, bounds: list, words: list, islands: list, min_dur: float, max_dur: float, target_dur: float, dur: float
) -> dict:
    """
    Clip around payoff p. START: a clean boundary (sentence end or a pause >= 0.6 s, so never
    mid-sentence) in [p-25, p-10] (>= 3 s before the payoff lead-in) with the longest pause before it
    (the situation begins), tie -> closest to p-(target_dur-4). END: the reaction runs out - a silence
    >= 0.6 s that starts before p+2 and lasts past it, else the first silence >= 0.6 s or sentence end
    (pause >= 0.35 s) in [p+2, p+12], else the longest pause in that range, else p+4 snapped to the end
    of the word/island; never within 2 s after the payoff peak. Never inside a word. min/max duration
    move START only (END stays on the reaction).
    """

    def start_at(b):
        return max(0.0, b["ge"] - min(0.2, b["pause"] / 2))

    def clean(b):
        return b["kind"] == "sentence" or b["pause"] >= 0.6

    cand = [b for b in bounds if p - 25 <= b["ge"] <= p - 10]
    cand = [b for b in cand if clean(b)] or cand
    aim = p - (target_dur - 4)
    if cand:
        best = max(cand, key=lambda b: (round(min(b["pause"], 2.0), 1), -abs(b["ge"] - aim)))
        start = start_at(best)
    else:
        isl = [x for x in islands if x["speech_start"] < p - 4]
        start = (min(isl, key=lambda x: abs(x["speech_start"] - (p - 15)))["speech_start"] - 0.2) if isl else p - 15
    start = max(0.0, start)

    hi = p + 12.0
    quiet = [b for b in bounds if p <= b["gs"] < p + 2 <= b["ge"] and b["pause"] >= 0.6]
    ends = [b for b in bounds if p + 2 <= b["gs"] <= hi and (b["pause"] >= 0.6 or (clean(b) and b["pause"] >= 0.35))]
    if quiet:
        b = quiet[0]
        end = min(b["ge"] - 0.05, max(p + 2.2, b["gs"] + 0.4))
    elif ends:
        b = min(ends, key=lambda b: b["gs"])
        end = b["gs"] + min(0.15, b["pause"] / 2)
    else:
        c3 = [b for b in bounds if p + 2 <= b["gs"] <= hi]
        if c3:
            b = max(c3, key=lambda b: (round(b["pause"], 2), -b["gs"]))
            end = b["gs"] + min(0.15, b["pause"] / 2)
        else:
            end = p + 4.0
            w = _inside_word(end, words)
            isl = next((x for x in islands if x["speech_start"] <= end < x["speech_end"]), None)
            end = (w["e"] if w else (isl["speech_end"] if isl else end)) + 0.15
    end = max(end, p + 2.0)
    end = off_word(end, words, +1)
    end = min(dur, end)

    starts = sorted({round(start_at(b), 3) for b in bounds if b["ge"] < p - 4 and clean(b)}) or sorted(
        {round(start_at(b), 3) for b in bounds if b["ge"] < p - 3}
    )
    if end - start < min_dur:
        ok = [s for s in starts if end - s >= min_dur]
        start = ok[-1] if ok else max(0.0, end - min_dur)
    elif end - start > max_dur:
        ok = [s for s in starts if end - s <= max_dur]
        start = ok[0] if ok else end - max_dur
    if _inside_word(start, words):
        back = off_word(start, words, -1)
        start = back if end - back <= max_dur else off_word(start, words, +1)
    start = max(0.0, start)

    pay0, pay1 = max(start + 0.5, p - 1.0), min(end, p + 1.0)
    t_t = start + 0.6 * (pay0 - start)
    later = [b["ge"] for b in bounds if start + 0.4 * (pay0 - start) <= b["ge"] <= p - 3.0]
    if later:
        t_t = max(later)
    t_t = min(max(start, t_t), pay0)
    return {
        "start": start,
        "end": end,
        "peak": p,
        "arc": {"setup": [start, t_t], "tension": [t_t, pay0], "payoff": [pay0, pay1], "reaction": [pay1, end]},
    }


def trim_pauses(win: dict, islands: list, min_pause: float = 0.55, protect: tuple = None) -> list:
    """
    Ordered segments [{src_s, src_e, kind, fade_ms}] covering win start..end. Silences longer than
    min_pause between speech are cut down to 0.15 s on each side of the speech; nothing is cut inside
    the protected payoff zone. Boundaries sit on the 1/30 s grid; pieces < 0.25 s are merged.
    """
    s0, e0 = win["start"], win["end"]
    spans = [
        (max(s0, x["speech_start"]), min(e0, x["speech_end"]))
        for x in islands
        if x["speech_end"] > s0 and x["speech_start"] < e0
    ]
    cuts = []
    for (_, a1), (b0, _) in zip(spans, spans[1:], strict=False):
        if b0 - a1 <= min_pause:
            continue
        c0, c1 = a1 + 0.15, b0 - 0.15
        parts = [(c0, c1)]
        if protect:
            parts = [(c0, min(c1, protect[0])), (max(c0, protect[1]), c1)]
        cuts += [(_q(x), _q(y)) for x, y in parts if y - x >= 0.1]
    edges = [_q(s0)] + [v for c in cuts for v in c] + [_q(e0)]
    segs = [[edges[i], edges[i + 1]] for i in range(0, len(edges) - 1, 2)]
    segs = [s for s in segs if s[1] > s[0]] or [[_q(s0), _q(e0)]]
    while len(segs) > 1:
        k = next((i for i, s in enumerate(segs) if s[1] - s[0] < 0.25), None)
        if k is None:
            break
        j = k if k + 1 < len(segs) else k - 1
        segs[j] = [segs[j][0], segs[j + 1][1]]
        del segs[j + 1]
    arc, p = win.get("arc"), win.get("peak")

    def kind(a, b):
        if p is not None and a <= p < b:
            return "payoff"
        if not arc:
            return "story"
        m = (a + b) / 2
        for k_name in ("setup", "tension", "payoff", "reaction"):
            if arc[k_name][0] <= m < arc[k_name][1]:
                return k_name
        return "reaction" if p is not None and m >= p else "setup"

    return [{"src_s": a, "src_e": b, "kind": kind(a, b), "fade_ms": 12} for a, b in segs]


def _hook_case(text: str) -> str:
    """Upper-case Latin letters only; Devanagari stays as it is."""
    return "".join(ch.upper() if ch.isascii() else ch for ch in text)


FILLERS = {"um", "uh", "uhh", "umm", "erm", "hmm", "mm", "ah", "eh"}
# a hook must not end on these (the phrase is cut off) nor start on the first group (it starts mid-phrase)
BAD_START = {"and", "or", "but", "so", "of", "to", "for", "with", "at", "in", "on", "by", "from", "than", "then"}
BAD_END = BAD_START | {
    "the", "a", "an", "my", "your", "his", "her", "their", "our", "its", "is", "was", "are", "were", "be",
    "i", "i'm", "im", "you", "you're", "he", "she", "we", "they", "it's", "that's", "this", "if", "when",
    "like", "just", "gonna", "wanna", "can", "can't", "don't", "do", "did", "got", "get", "have", "has",
    "never", "not", "really", "very", "even", "also", "still", "about", "because", "cause", "cuz",
}  # fmt: skip
# a clause that starts on these needs its main clause ('if we know it never ...'): never a hook start
DANGLING_START = BAD_START | {"if", "because", "cause", "cuz", "when", "while", "unless", "although", "though", "until"}
# words that carry no meaning of their own (verbs like got/did/have do count); see content_words
FUNCTION_WORDS = BAD_START | {
    "the", "a", "an", "this", "that", "these", "those", "my", "your", "his", "her", "their", "our", "its",
    "i", "me", "you", "he", "him", "she", "we", "us", "they", "them", "it", "i'm", "im", "you're", "he's",
    "she's", "we're", "they're", "it's", "that's", "there's", "what's", "is", "am", "are", "was", "were", "be",
    "been", "being", "will", "would", "could", "should", "can", "can't", "won't", "isn't", "wasn't", "aren't",
    "don't", "doesn't", "didn't", "if", "when", "because", "cause", "cuz", "as", "up", "out", "about", "into",
    "off", "over", "not", "no", "yes", "just", "like", "very", "really", "too", "also", "even", "still",
    "there", "here", "what", "who", "why", "how", "where", "which", "all", "some", "any", "gonna", "wanna",
}  # fmt: skip
INTERJECTIONS = {
    "oh", "ah", "aw", "hey", "yo", "bro", "bruh", "dude", "man", "wow", "yeah", "yep", "nah", "ok", "okay",
    "huh", "lol", "omg", "ayo", "whoa", "damn", "dang", "my", "god", "sheesh", "boy",
}  # fmt: skip

# Short plain-English hooks by category, for clips without a usable transcript line.
CATEGORY_HOOKS = {
    "funny": ("HE DID NOT SEE THAT COMING", "I CAN'T STOP LAUGHING", "THIS WENT SO WRONG"),
    "rage": ("HE LOST IT", "THIS GAME BROKE HIM", "HE WAS NOT READY"),
    "clutch": ("HE ACTUALLY PULLED IT OFF", "NO WAY HE DID THAT", "THE CLUTCH OF THE YEAR"),
    "reaction": ("HE DID NOT SEE THAT COMING", "WATCH HIS FACE", "HIS REACTION IS EVERYTHING"),
    "fail": ("THAT DID NOT GO TO PLAN", "WORST TIMING EVER", "HE WAS SO CLOSE"),
    "hot take": ("HE REALLY SAID THAT", "THIS TAKE IS WILD"),
    "reveal": ("WAIT FOR THE REVEAL", "NOBODY EXPECTED THIS"),
    "wholesome": ("THIS MADE HIS DAY", "WHOLESOME MOMENT"),
    "chaos": ("PURE CHAOS", "IT ALL WENT WRONG AT ONCE"),
}
DEFAULT_HOOKS = ("HE DID NOT SEE THAT COMING", "WAIT FOR IT...", "WATCH WHAT HAPPENS")


def _bare(w: str) -> str:
    return str(w).lower().strip(".,!?;:\"'()[]-…")


def _dedupe_repeats(toks: list) -> list:
    """'watch watch this' -> 'watch this'; a 2-word repeat ('no no') stays when nothing else is left."""
    out = []
    for t in toks:
        if out and _bare(out[-1]) == _bare(t):
            continue
        out.append(t)
    return out if len(out) >= 2 or len(toks) < 2 else toks[:2]


def content_words(toks: list) -> int:
    """Distinct words that carry meaning: not function words, interjections ('oh', 'bro', 'yo') or fillers."""
    return len({b for t in toks if (b := _bare(t)) and b not in FUNCTION_WORDS | INTERJECTIONS | FILLERS})


def hook_complete(toks: list, edge_start: bool = True, edge_end: bool = True) -> bool:
    """
    A hook line that reads as one whole thought. Never: a sentence end inside it ('TREES? GO', 'OH MY... HEY'),
    a dangling start ('IF WE KNOW IT NEVER') or end word. Then either >= 3 content words, or a complete short
    line (a phrase edge at both ends, so not 'THE CHAT YO BRO' cut from 'in the chat yo bro.') that is an
    exclamation or question with real words ('BRO WHAT IS THAT?!'), a trigger phrase ('NO WAY') or a whole
    sentence with a content word ('I GOT IT BRO.').
    """
    bare = [_bare(t) for t in toks]
    if not bare or not all(bare) or bare[0] in DANGLING_START or bare[-1] in BAD_END:
        return False
    if len(set(bare)) < 2:  # one word chanted ('GO, GO', 'WAIT WAIT')
        return False
    if any(str(t).rstrip("\"')").endswith(SENTENCE_END + ("…",)) for t in toks[:-1]):
        return False
    content = content_words(toks)
    if content >= 3 and (edge_start or edge_end):  # with no edge at all it is cut from a longer line
        return True
    if not (edge_start and edge_end):
        return False
    last = str(toks[-1]).rstrip("\"')")
    if last.endswith(("!", "?")) and any(b not in INTERJECTIONS for b in bare):
        return True
    if any(h[2] in TRIGGER_PHRASES for h in find_triggers(list(toks))):
        return True
    return content >= 2 or (content >= 1 and last.endswith(SENTENCE_END))


def category_hook(category: str = None, seed: float = 0.0, exclude=()) -> str:
    """A generic hook (last resort): the category's list first, then every other one; never one in exclude."""
    opts = CATEGORY_HOOKS.get(str(category or ""), DEFAULT_HOOKS)
    k = int(abs(seed) * 10) % len(opts)
    pool = list(opts[k:] + opts[:k]) + [h for hs in (DEFAULT_HOOKS, *CATEGORY_HOOKS.values()) for h in hs]
    return next((h for h in pool if h not in exclude), "")


def suggest_hook(words: list, win: dict, category: str = None, exclude=()) -> str:
    """
    Hook banner text: the strongest short phrase (2-6 words, <= 30 chars) near the payoff (p-3 .. p+5,
    inside the clip). A phrase scores for trigger words/phrases, for starting and ending at a phrase
    edge (punctuation or a pause >= 0.25 s), and for being close to the payoff. Fillers (um, uh) are
    dropped, repeated words collapsed, no dangling 'and/the/to/my' at either end, and the phrase must be
    complete (hook_complete: no sentence end inside, ~3 content words or a whole short exclamation). When no phrase
    qualifies: the payoff sentence when it is 2-6 words, else a category-based plain English hook.
    exclude: hooks already used in this run (upper case); they are never returned again. A weaker line of
    the streamer's own beats a generic template; templates are the last resort.
    """
    exclude = set(exclude or ())
    p, start, end = win["peak"], win["start"], win["end"]
    zone = [w for w in words if max(start, p - 3.0) <= w["s"] < min(end, p + 5.0) and _bare(w["w"]) not in FILLERS]
    zone = [w for w in zone if _bare(w["w"])]

    def edge_before(i):
        if i == 0:
            return True
        prev = zone[i - 1]
        return str(prev["w"]).rstrip("\"')").endswith(SENTENCE_END + (",",)) or zone[i]["s"] - prev["e"] >= 0.25

    def edge_after(j):
        if j == len(zone) - 1:
            return True
        return (
            str(zone[j]["w"]).rstrip("\"')").endswith(SENTENCE_END + (",",)) or zone[j + 1]["s"] - zone[j]["e"] >= 0.25
        )

    best = None
    for i in range(len(zone)):
        if _bare(zone[i]["w"]) in BAD_START:
            continue
        for n in range(2, 7):
            span = zone[i : i + n]
            if len(span) < n:
                break
            toks = _dedupe_repeats([w["w"] for w in span])
            text = " ".join(toks).strip().strip(",;:-").rstrip(".,;:")
            if len(text) > 30:
                break
            if len(toks) < 2 or _bare(toks[-1]) in BAD_END or _hook_case(text) in exclude:
                continue
            hits = find_triggers([w["w"] for w in span])
            phrase = any(h[2] in TRIGGER_PHRASES for h in hits)
            j = i + n - 1
            sentence_like = edge_before(i) and edge_after(j)
            if not hits and not sentence_like:
                continue
            if not hook_complete(toks, edge_before(i), edge_after(j)):
                continue  # a fragment ('THE CHAT YO BRO'): a category template reads better
            dist = min(abs(w["s"] - p) for w in span)
            score = (
                2.0 * edge_before(i)
                + 2.0 * edge_after(j)
                + 2.0 * phrase
                + 1.0 * min(2, len(hits))
                + (0.5 if 3 <= len(toks) <= 5 else 0.0)
                + 0.3 * min(4, content_words(toks))
                - 0.4 * dist
            )
            if best is None or score > best[0]:
                best = (score, text)
    if best and (best[0] >= 2.0 or exclude):
        return _hook_case(best[1])
    hook = category_hook(category, p, exclude)
    if hook:
        return _hook_case(hook)
    kws = [_bare(w["w"]) for w in sorted(zone, key=lambda w: -len(_bare(w["w"]))) if len(_bare(w["w"])) > 3]
    base = _hook_case(f"WAIT FOR THE {kws[0]}" if kws else f"MOMENT AT {int(p) // 60}:{int(p) % 60:02d}")
    hook, k = base, 2
    while hook in exclude:
        hook, k = f"{base} ({k})", k + 1
    return hook


def unique_hooks(clips: list) -> None:
    """
    Make every clip's hook_headline unique within one run (rank order): a repeat gets the streamer's next
    best line (suggest_hook with the used hooks excluded), a generic template only as the last resort.
    """
    used = set()
    for c in clips:
        hook = str(c.get("hook_headline") or "")
        if not hook or hook in used:
            hook = suggest_hook(c.get("words") or [], c, c.get("category"), used)
            c["hook_headline"] = hook
        used.add(hook)
