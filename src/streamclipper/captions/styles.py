"""Caption style registry, hook banner styles, palette, fonts and caption text helpers (case, emoji, profanity)."""

import re
from dataclasses import dataclass

NEON_PALETTE = {
    "white": ([1.0, 1.0, 1.0], "#FFFFFF"),
    "yellow": ([1.0, 0.902, 0.0], "#FFE600"),  # active word
    "green": ([0.0, 1.0, 0.35], "#00FF59"),
    "red": ([1.0, 0.231, 0.188], "#FF3B30"),  # hype / trigger words
    "cyan": ([0.0, 0.94, 1.0], "#00F0FF"),
    "orange": ([1.0, 0.52, 0.0], "#FF8500"),
    "pink": ([1.0, 0.18, 0.78], "#FF2EC7"),
    "purple": ([0.68, 0.28, 1.0], "#AD47FF"),
    "black": ([0.02, 0.02, 0.03], "#050508"),
}

# Colours a user may pick with --highlight-color (black is only used inside boxes and bars).
HIGHLIGHT_COLOURS = tuple(k for k in NEON_PALETTE if k != "black")


FONT_FILES = {
    "barlow": "BarlowCondensed-Black.ttf",
    "anton": "Anton-Regular.ttf",
    "baloo": "Baloo2-ExtraBold.ttf",
    "montserrat": "Montserrat-Bold.ttf",
    "bebas": "BebasNeue-Regular.ttf",
    "poppins": "Poppins-ExtraBold.ttf",
    "poppins-black": "Poppins-Black.ttf",
    "bangers": "Bangers-Regular.ttf",
    "luckiest": "LuckiestGuy-Regular.ttf",
    "emoji": "NotoEmoji-VariableFont_wght.ttf",  # monochrome emoji for the MP4 captions only
}

# Fonts a caption style or --caption-font may use (the emoji font is picked automatically).
CAPTION_FONTS = tuple(k for k in FONT_FILES if k != "emoji")


@dataclass(frozen=True)
class CaptionStyle:
    """One caption look. Colours are palette names; sizes are ASS pixels on the 1080x1920 canvas."""

    name: str
    look: str
    words: int  # words per caption (1-4)
    font: str  # FONT_FILES key
    size: int = 84
    primary: str = "white"
    active: str = "yellow"
    hype: str = "red"
    outline: int = 6
    shadow: int = 3
    box: bool = False  # active word on a box in its active colour
    anim: str = "pop"  # pop|none|shake|bounce|burst|glitch|neon|gradient|typewriter|stack
    emoji: bool = False
    case: str = "upper"  # upper|title|as-is (Latin letters only)
    glow: bool = False  # CapCut TextBloom (only when the glow effect is in the user's CapCut cache)
    anim_up: int = 0  # px the animation can move the text above its anchor

    @property
    def capcut_anim(self) -> str:
        """Keyframe animation for the first word of each caption in CapCut drafts."""
        return {
            "pop": "pop-bounce",
            "gradient": "pop-bounce",
            "glitch": "pop-bounce",
            "stack": "slide-up-pop",
            "shake": "shake",
            "bounce": "bounce",
            "burst": "burst",
        }.get(self.anim, "none")


STYLES = {
    s.name: s
    for s in (
        CaptionStyle("bold-pop", "1-2 big words, yellow active word, pop-in", 2, "barlow", glow=True),
        CaptionStyle(
            "beast-bold",
            "1-2 huge words, thick black stroke, green active word",
            2,
            "luckiest",
            88,
            active="green",
            outline=10,
        ),
        CaptionStyle(
            "karaoke-highlight",
            "3-word line, the spoken word lights up",
            3,
            "poppins",
            72,
            outline=5,
            anim="none",
            glow=True,
        ),
        CaptionStyle(
            "word-by-word", "one word at a time, tall condensed type", 1, "bebas", 112, active="white", anim="none"
        ),
        CaptionStyle(
            "two-line-stack", "4 words on 2 lines, the second line slides up", 4, "barlow", anim="stack", glow=True
        ),
        CaptionStyle(
            "highlight-box",
            "2-3 words, the spoken word sits on a yellow box",
            3,
            "poppins-black",
            72,
            outline=5,
            box=True,
            anim="none",
        ),
        CaptionStyle("neon-glow", "2 words with a soft cyan glow", 2, "barlow", active="cyan", anim="neon", glow=True),
        CaptionStyle("impact-shake", "1 punchy word that shakes on impact", 1, "anton", 100, anim="shake"),
        CaptionStyle(
            "emoji-pop", "bold-pop plus a matching emoji (fire, skull, laughing...)", 2, "barlow", emoji=True, glow=True
        ),
        CaptionStyle("typewriter", "up to 4 words typed in one by one", 4, "poppins", 68, outline=5, anim="typewriter"),
        CaptionStyle("bounce", "1 word that bounces up into place", 1, "barlow", 96, anim="bounce", anim_up=8),
        CaptionStyle(
            "gradient-stroke", "2 words with a pink and orange double outline", 2, "anton", 88, anim="gradient"
        ),
        CaptionStyle(
            "comic-burst",
            "comic-book yellow words, red spoken word, tilted, black stroke",
            2,
            "bangers",
            96,
            primary="yellow",
            active="red",
            outline=8,
            anim="burst",
            anim_up=32,
        ),
        CaptionStyle(
            "minimal-clean",
            "4 words, smaller clean type, thin outline, no motion",
            4,
            "poppins",
            64,
            outline=3,
            shadow=2,
            anim="none",
            case="title",
        ),
        CaptionStyle(
            "gaming-glitch", "2 words with a cyan/magenta glitch flicker", 2, "anton", 88, active="cyan", anim="glitch"
        ),
    )
}

STYLE_ALIASES = {
    "hormozi-pop": "bold-pop",
    "karaoke-word": "karaoke-highlight",
    "glow-neon": "neon-glow",
    "shake-impact": "impact-shake",
    "bounce-word": "bounce",
}

DEFAULT_STYLE = "bold-pop"

# --caption-style auto (the default): each clip gets the style of its category. Podcasts and interviews
# (--content talk) get karaoke-highlight. Any other category gets DEFAULT_STYLE. An explicit style name
# (CLI flag, spec or preset) is used for every clip instead.
AUTO_STYLE = "auto"
CATEGORY_STYLES = {
    "funny": "comic-burst",
    "rage": "impact-shake",
    "chaos": "impact-shake",
    "clutch": "bold-pop",
    "reaction": "beast-bold",
    "hot take": "karaoke-highlight",
    "wholesome": "minimal-clean",
    "reveal": "highlight-box",
}
TALK_STYLE = "karaoke-highlight"

HOOK_STYLES = {
    "bold-box": "black text on a yellow box, hype words red",
    "outline-glow": "white text with a glowing yellow outline",
    "top-bar": "white text on a full-width red bar",
}

DEFAULT_HOOK_STYLE = "bold-box"

CAPTION_CASES = ("upper", "title", "as-is")
CAPTION_POSITIONS = ("auto", "top", "center", "lower")


def style_name(name: str) -> str:
    """Canonical style name for a name or alias; '' when unknown."""
    key = str(name or "").strip().lower()
    key = STYLE_ALIASES.get(key, key)
    return key if key in STYLES else ""


def get_style(name: str) -> CaptionStyle:
    return STYLES[style_name(name) or DEFAULT_STYLE]


def auto_style(category: str, content: str = "gaming") -> str:
    """The style --caption-style auto picks for one clip (see CATEGORY_STYLES)."""
    if content == "talk":
        return TALK_STYLE
    return CATEGORY_STYLES.get(str(category or "").strip().lower(), DEFAULT_STYLE)


def clip_style(style: str, category: str, content: str = "gaming") -> str:
    """The caption style one clip is rendered with: 'auto' (or empty) -> auto_style, else the style itself."""
    if not style or str(style).strip().lower() == AUTO_STYLE:
        return auto_style(category, content)
    return style_name(style) or DEFAULT_STYLE


# Keyword -> emoji (whole words, case-insensitive). Phrases attach the emoji after their last word.
EMOJI_MAP = {
    "fire": "🔥",
    "dead": "💀",
    "lol": "😂",
    "lmao": "😂",
    "no way": "😱",
    "let's go": "🚀",
    "lets go": "🚀",
    "w": "🏆",
    "l": "💀",
    "gg": "🎮",
    "cops": "🚓",
    "cop": "🚓",
    "police": "🚓",
    "money": "💰",
    "cash": "💰",
    "insane": "🤯",
}

EMOJI_RE = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff]\ufe0f?")


def _norm_word(w: str) -> str:
    return re.sub(r"[^\w']", "", str(w).lower())


def emoji_positions(words: list) -> dict:
    """{word index: emoji} for the keyword map; a phrase match wins over a single word."""
    norm = [_norm_word(w) for w in words]
    out, i = {}, 0
    phrases = sorted((k.split() for k in EMOJI_MAP), key=len, reverse=True)
    while i < len(norm):
        for ph in phrases:
            n = len(ph)
            if norm[i : i + n] == ph:
                out[i + n - 1] = EMOJI_MAP[" ".join(ph)]
                i += n - 1
                break
        i += 1
    return out


# Common English profanity (masked as f***, first letter kept). Default off.
PROFANITY = {
    "fuck",
    "fucking",
    "fucked",
    "fucker",
    "fuckin",
    "motherfucker",
    "shit",
    "shitty",
    "bullshit",
    "bitch",
    "bitches",
    "bastard",
    "asshole",
    "ass",
    "dick",
    "dickhead",
    "pussy",
    "cunt",
    "damn",
    "goddamn",
    "piss",
    "pissed",
    "whore",
    "slut",
    "crap",
}


def mask_profanity(word: str) -> str:
    """'fucking' -> 'f******'; punctuation around the word is kept; other words are unchanged."""
    m = re.match(r"^(\W*)([A-Za-z']+)(\W*)$", str(word))
    if not m or m.group(2).lower().replace("'", "") not in PROFANITY:
        return word
    core = m.group(2)
    return m.group(1) + core[0] + "*" * (len(core) - 1) + m.group(3)


def apply_case(text: str, case: str) -> str:
    """Caption case on Latin letters only; Devanagari and other scripts stay as they are."""
    if case == "upper":
        return "".join(ch.upper() if ch.isascii() else ch for ch in text)
    if case == "title":
        out, start = [], True
        for ch in text:
            if ch.isascii() and ch.isalpha():
                out.append(ch.upper() if start else ch.lower())
                start = False
            else:
                out.append(ch)
                start = ch.isspace() or (start and not ch.isalnum())
        return "".join(out)
    return text
