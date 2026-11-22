"""
Post copy without an LLM: a title, a one-sentence description and hashtags in plain English, built from
the clip's hook headline, category and transcript keywords. Used whenever the LLM gave nothing (--llm none,
LLM down) or gave a title another clip already has.
"""

import re
from collections import Counter

from streamclipper.captions.styles import PROFANITY, mask_profanity

TITLE_MAX = 60
SMALL_WORDS = {"a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "by", "vs", "as"}
# Words that say nothing about the moment (stream filler, pronouns, common verbs); never keywords.
STOPWORDS = set(
    """
    about above after again against all also always among another any anybody anyone anything around away back
    because been before being below between both bring came come comes coming could didn doesn doing done down
    during each either else even ever every everybody everyone everything from gets getting give given goes going
    gone gonna good gotta guys have having here hers herself himself into itself just keep know knew last least
    less like literally look looks made make makes making many maybe mean might mine more most much must myself
    need never next nothing okay only other others ours over really right said same saying says should since
    some somebody someone something stuff such sure take than that thats their theirs them themselves then there
    these they thing things think this those though thought through today together told took toward under until
    upon very wait wanna want wanted were what whatever when where whether which while will with without would
    yeah yes your yours yourself bro bruh chat dude guys yall y'all hello thank thanks please sorry alright
    actually basically honestly literally stop hold watch huh whoa what's that's it's he's she's they're we're
    you're i'm i've i'll i'd don't can't won't isn't ain't wasn't didn't let's there's here's who's yo
    almost till until call called calling turn turned turns check checking chad hell heck swear life
    beat beating start started stay play playing played tell telling talk talking happen happened
    leave left feel feels felt help seen show showing move moving walk walking left right first okay
    """.split()
)
CATEGORY_LEAD = {
    "funny": "A funny moment",
    "rage": "A rage moment",
    "clutch": "A clutch moment",
    "reaction": "A big reaction",
    "fail": "A painful fail",
    "hot take": "A hot take",
    "reveal": "A surprise reveal",
    "wholesome": "A wholesome moment",
    "chaos": "Pure chaos",
}
CATEGORY_TAGS = {
    "funny": ["#funny", "#funnymoments"],
    "rage": ["#rage", "#ragequit"],
    "clutch": ["#clutch", "#gamingclips"],
    "reaction": ["#reaction", "#streamer"],
    "fail": ["#fail", "#gamingfails"],
    "hot take": ["#hottake", "#podcast"],
    "reveal": ["#plottwist", "#streamer"],
    "wholesome": ["#wholesome", "#streamer"],
    "chaos": ["#chaos", "#gamingclips"],
}
BASE_TAGS = ["#shorts", "#gaming"]
GTA_RE = re.compile(r"\bgta\s*(?:6|vi|v|5)?\b|\bgrand\s+theft\s+auto\b", re.I)


def _bare(word: str) -> str:
    return re.sub(r"[^\w']", "", str(word).lower()).strip("'")


def keywords(words: list, n: int = 3) -> list:
    """Up to n content words of the transcript, most frequent first (ties: first said)."""
    seen = Counter()
    first = {}
    for i, w in enumerate(words or []):
        t = _bare(w["w"] if isinstance(w, dict) else w)
        if len(t) < 4 or not t.isascii() or not t.replace("'", "").isalpha():
            continue
        if t in STOPWORDS or t.replace("'", "") in PROFANITY:
            continue
        seen[t] += 1
        first.setdefault(t, i)
    return sorted(seen, key=lambda t: (-seen[t], first[t]))[:n]


def title_case(text: str) -> str:
    """'HE DID NOT SEE THAT COMING' -> 'He Did Not See That Coming' (small words lower case inside)."""
    out = []
    for i, tok in enumerate(str(text).split()):
        low = tok.lower()
        core = low.strip(".,!?;:\"'()…-")
        if core == "i" or core.startswith("i'"):
            out.append("I" + low[1:] if low.startswith("i") else low)
        elif i and core in SMALL_WORDS:
            out.append(low)
        else:
            j = next((k for k, ch in enumerate(low) if ch.isalnum()), 0)
            out.append(low[:j] + low[j:].capitalize())
    return " ".join(out)


def _clean_hook(hook: str) -> str:
    text = re.sub(r"\{\w+:([^}]*)\}", r"\1", str(hook or ""))  # {yellow:WAIT} colour markup
    text = " ".join(text.replace("\\n", " ").split())
    return " ".join(mask_profanity(w) for w in text.split(" ")).strip(" .,;:-")


def _cap(text: str, limit: int = TITLE_MAX) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit + 1].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") or text[:limit]


def make_title(hook: str, kws: list, taken: set, at: float = None) -> str:
    """
    Title Case hook (<= 60 chars), unique among taken (lower case). When the hook is taken: the hook plus
    the main keyword (kws should hold only words said at least twice), else plus the stream time.
    """
    base = _cap(title_case(_clean_hook(hook)) or "Stream Highlight")
    options = [base]
    if kws:
        options.append(_cap(f"{base}: {title_case(kws[0])}"))
    if len(kws) > 1:
        options.append(_cap(f"{base}: {title_case(kws[0])} and {title_case(kws[1])}"))
    if at is not None:
        t = int(max(0.0, at))
        stamp = f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"
        options.append(f"{_cap(base, TITLE_MAX - len(stamp) - 4)} at {stamp}")
    for opt in options:
        if opt.lower() not in taken:
            return opt
    n = 2
    while f"{base} {n}".lower() in taken:
        n += 1
    return f"{_cap(base, TITLE_MAX - 3)} {n}"


def make_description(category: str, kws: list) -> str:
    lead = CATEGORY_LEAD.get(str(category or ""), "A highlight")
    if not kws:
        return f"{lead} from the stream."
    joined = kws[0] if len(kws) == 1 else ", ".join(kws[:-1]) + " and " + kws[-1]
    return f"{lead} from the stream about {joined}."


def make_hashtags(
    category: str,
    kws: list,
    text: str = "",
    source_title: str = "",
    talk: bool = False,
    game=None,
) -> list:
    tags = list(CATEGORY_TAGS.get(str(category or ""), ["#streamer"]))
    if talk:  # podcasts and interviews: no gaming tags
        tags = [t for t in tags if "gaming" not in t] + ["#podcast"]
    tags += ["#" + k.replace("'", "") for k in kws[:2]]
    if game:
        from streamclipper.games import tags_for

        tags = list(tags_for(game, limit=6)) + tags
    elif GTA_RE.search(f"{text} {source_title}"):
        tags.append("#GTA6")
    tags += ["#shorts"] if talk else BASE_TAGS
    out, seen = [], set()
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:8]


def copy_for(clip: dict, taken: set, source_title: str = "", game=None) -> dict:
    """{title, description, hashtags} for one clip dict (hook_headline, category, words or text, start)."""
    words = clip.get("words") or str(clip.get("text") or "").split()
    text = " ".join(w["w"] if isinstance(w, dict) else str(w) for w in words)
    kws = keywords(words)
    counts = Counter(_bare(w["w"] if isinstance(w, dict) else w) for w in words)
    topic = [k for k in kws if counts[k] >= 2]  # a title keyword must be a word the clip keeps saying
    title = make_title(clip.get("hook_headline") or "", topic, taken, clip.get("start"))
    return {
        "title": title,
        "description": make_description(clip.get("category"), kws),
        "hashtags": make_hashtags(
            clip.get("category"),
            kws,
            text,
            source_title,
            (clip.get("signals") or {}).get("content") == "talk",
            game,
        ),
    }
