"""Game-aware metadata and contextual prompt profiles.

This registry is intentionally small and data-driven.  It can be extended
without changing discovery or scoring.  Unknown games use a generic gaming
profile; manually selected games always override title-based detection.
"""

from __future__ import annotations

import re

GENERAL_TAGS = ("Gaming", "Gameplay", "GamingClips", "StreamHighlights")

GAMES = {
    "Grand Theft Auto VI": {
        "aliases": ("grand theft auto vi", "grand theft auto 6", "gta vi", "gta 6", "gta6", "gtavi", "leonida"),
        "tags": ("GTA6", "GTA6Game", "ViceCity", "RockstarGames"),
        "taxonomy": (
            "police chases",
            "wanted-level escalation",
            "vehicle crashes",
            "stunts",
            "combat",
            "escapes",
            "NPC interactions",
            "unexpected NPC behaviour",
            "mission completion",
            "mission failure",
            "funny events",
            "surprising visual moments",
            "player reactions",
        ),
        "hooks": (
            "THE PERFECT ESCAPE",
            "WORST TIMING",
            "THAT NPC",
            "POLICE CHAOS",
            "NO WAY OUT",
            "WHAT JUST HAPPENED",
        ),
    },
    "Grand Theft Auto V": {
        "aliases": ("grand theft auto v", "grand theft auto 5", "gta v", "gta 5", "gta5", "gtav", "gta online"),
        "tags": ("GTA5", "GTAOnline", "RockstarGames", "GTA5RP"),
        "taxonomy": ("heists", "police chases", "vehicle crashes", "stunts", "wanted level", "mission outcomes"),
        "hooks": ("THE PERFECT GETAWAY", "WORST TIMING", "POLICE CHAOS", "THIS BUILD WAS INSANE", "WHAT JUST HAPPENED"),
    },
    "Grand Theft Auto IV": {
        "aliases": ("grand theft auto iv", "gta iv", "gta 4", "gta4"),
        "tags": ("GTA4", "RockstarGames"),
        "taxonomy": ("chases", "vehicle crashes", "combat", "mission outcomes"),
        "hooks": ("THE PERFECT ESCAPE", "POLICE CHAOS", "WORST TIMING"),
    },
    "Fortnite": {
        "aliases": ("fortnite",),
        "tags": ("Fortnite", "FortniteOG", "BattleRoyale"),
        "taxonomy": ("clutches", "build fights", "eliminations", "final circles", "launches", "funny moments"),
        "hooks": ("THE CLUTCH", "NO WAY THAT LANDED", "FINAL CIRCLE", "ONE BUILD TOO FAR"),
    },
    "Minecraft": {
        "aliases": ("minecraft",),
        "tags": ("Minecraft", "MinecraftGame"),
        "taxonomy": ("speedruns", "build reveals", "redstone", "boss fights", "survival fails", "funny moments"),
        "hooks": ("THE BUILD WORKED", "WORST TIMING", "NO WAY HE FOUND IT"),
    },
    "Valorant": {
        "aliases": ("valorant",),
        "tags": ("Valorant", "ValRadiant"),
        "taxonomy": ("clutches", "aces", "eco rounds", "team fights", "ability plays"),
        "hooks": ("THE ACE", "ONE VS FIVE", "THE CLUTCH", "NO WAY OUT"),
    },
    "League of Legends": {
        "aliases": ("league of legends", "league of legend", "lol"),
        "tags": ("LeagueOfLegends", "LoL", "RiotGames"),
        "taxonomy": ("team fights", "pentakills", "outplays", "comebacks", "objective steals"),
        "hooks": ("THE PENTAKILL", "THE STEAL", "ONE MORE HIT", "THE COMEBACK"),
    },
    "Counter-Strike": {
        "aliases": ("counter-strike", "counter strike", "cs2", "csgo"),
        "tags": ("CS2", "CounterStrike", "Esports"),
        "taxonomy": ("clutches", "aces", "eco wins", "bomb defuses", "sniper shots"),
        "hooks": ("THE CLUTCH", "THE ACE", "NO WAY HE MISSED", "EVERY ROUND COUNTS"),
    },
    "Elden Ring": {
        "aliases": ("elden ring",),
        "tags": ("EldenRing", "SoulsLike", "FromSoftware"),
        "taxonomy": ("boss fights", "one-shot deaths", "comebacks", "build reveals", "unexpected encounters"),
        "hooks": ("ONE SHOT", "THE COMEBACK", "WORST TIMING", "HE PULLED IT OFF"),
    },
    "Palworld": {
        "aliases": ("palworld",),
        "tags": ("Palworld", "Survival"),
        "taxonomy": ("boss fights", "base events", "capturing", "survival", "funny companion moments"),
        "hooks": ("THE CAPTURE", "THAT PAL", "WORST TIMING"),
    },
    "Call of Duty": {
        "aliases": ("call of duty", "warzone", "cod"),
        "tags": ("CallOfDuty", "Warzone", "COD"),
        "taxonomy": ("gunfights", "clutches", "high-kill moments", "final circles", "vehicle escapes"),
        "hooks": ("THE CLUTCH", "LAST CIRCLE", "NO WAY OUT"),
    },
    "Helldivers 2": {
        "aliases": ("helldivers 2", "helldivers2"),
        "tags": ("Helldivers2", "CoOp"),
        "taxonomy": ("team kills", "extractions", "mission completion", "mission failure", "friendly fire"),
        "hooks": ("FRIENDLY FIRE", "THE EXTRACTION", "NO WAY THAT WORKED"),
    },
    "Cyberpunk 2077": {
        "aliases": ("cyberpunk 2077", "cyberpunk"),
        "tags": ("Cyberpunk2077", "NightCity"),
        "taxonomy": ("combat", "choices", "story missions", "vehicle scenes", "unexpected NPC events"),
        "hooks": ("NIGHT CITY CHAOS", "WHAT JUST HAPPENED", "THE WRONG CHOICE"),
    },
    "Starfield": {
        "aliases": ("starfield",),
        "tags": ("Starfield", "Bethesda"),
        "taxonomy": ("exploration", "space combat", "missions", "base building", "unexpected encounters"),
        "hooks": ("SPACE CHAOS", "THE DISCOVERY", "WHAT JUST HAPPENED"),
    },
    "Black Myth Wukong": {
        "aliases": ("black myth wukong", "black myth", "wukong"),
        "tags": ("BlackMythWukong", "ActionRPG"),
        "taxonomy": ("boss fights", "defeats", "comebacks", "skill reveals"),
        "hooks": ("THE COMEBACK", "ONE MORE TRY", "BOSS DOWN"),
    },
    "Marvel Rivals": {
        "aliases": ("marvel rivals",),
        "tags": ("MarvelRivals", "Heroes"),
        "taxonomy": ("team fights", "hero plays", "comebacks", "final pushes"),
        "hooks": ("THE TEAM PLAY", "THE COMEBACK", "NO WAY THAT LANDED"),
    },
    "Overwatch 2": {
        "aliases": ("overwatch 2", "overwatch"),
        "tags": ("Overwatch2", "OW2", "Blizzard"),
        "taxonomy": ("team fights", "play-of-the-game", "ultimate plays", "comebacks", "fails"),
        "hooks": ("THE ULTI", "THE COMEBACK", "PURE CHAOS"),
    },
    "Apex Legends": {
        "aliases": ("apex legends", "apex"),
        "tags": ("ApexLegends", "EApex"),
        "taxonomy": ("clutches", "final circles", "knockdowns", "revives", "movement plays"),
        "hooks": ("THE CLUTCH", "LAST CIRCLE", "THE REVIVE"),
    },
    "PUBG": {
        "aliases": ("pubg", "playerunknown battlegrounds"),
        "tags": ("PUBG", "BattleRoyale"),
        "taxonomy": ("final circles", "gunfights", "vehicle escapes", "clutches"),
        "hooks": ("LAST CIRCLE", "THE CLUTCH", "NO WAY OUT"),
    },
    "Rocket League": {
        "aliases": ("rocket league",),
        "tags": ("RocketLeague", "CarSports"),
        "taxonomy": ("goals", "aerial shots", "saves", "comebacks", "own goals"),
        "hooks": ("THE SAVE", "AERIAL PERFECT", "THE COMEBACK"),
    },
}

GENERIC_PROFILE = {
    "name": "Generic Gaming",
    "aliases": ("gaming", "game"),
    "tags": GENERAL_TAGS,
    "taxonomy": ("exciting gameplay", "clutches", "fails", "funny moments", "surprising events", "player reactions"),
    "hooks": ("PURE CHAOS", "WORST TIMING", "WHAT JUST HAPPENED", "THE PERFECT MOMENT"),
}

_WORD = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    return _WORD.sub(" ", str(text or "").lower()).strip()


def game_names() -> list[str]:
    return list(GAMES)


def resolve_game(name: str | None) -> dict:
    """Resolve a proper name or alias.  Returns the generic profile if unknown."""
    raw = normalize(name)
    if not raw:
        return dict(GENERIC_PROFILE)
    for profile_name, profile in GAMES.items():
        aliases = {normalize(profile_name)}
        aliases.update(normalize(alias) for alias in profile["aliases"])
        if raw in aliases:
            return {"name": profile_name, **profile}
    generic = {k: v for k, v in GENERIC_PROFILE.items() if k != "name"}
    return {
        "name": str(name).strip()[:60] or GENERIC_PROFILE["name"],
        **generic,
    }


def detect_game(*texts: str, manual: str | None = None) -> dict:
    """Manual selection has priority; otherwise detect aliases in title/metadata text."""
    if normalize(manual):
        return resolve_game(manual)
    hay = normalize(" ".join(str(text or "") for text in texts))
    if not hay:
        return dict(GENERIC_PROFILE)
    matches = []
    for name, profile in GAMES.items():
        for alias in (name, *profile["aliases"]):
            ali = normalize(alias)
            if ali and re.search(r"(?:^| )" + re.escape(ali) + r"(?: |$)", hay):
                matches.append((len(ali), name))
    if matches:
        name = max(matches)[1]
        return {"name": name, **GAMES[name]}
    return dict(GENERIC_PROFILE)


def tags_for(profile: dict | str | None, limit: int = 8) -> list[str]:
    resolved = resolve_game(profile) if isinstance(profile, str) else (profile or dict(GENERIC_PROFILE))
    tags = [str(t).strip().lstrip("#") for t in (resolved.get("tags") or GENERAL_TAGS)]
    tags += [str(t).strip().lstrip("#") for t in GENERAL_TAGS[:2]]
    out, seen = [], set()
    for tag in tags:
        if not tag:
            continue
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            out.append("#" + re.sub(r"[^\w]+", "", tag))
    return out[: max(1, min(limit, 30))]


def merge_tags(canonical, extra=(), limit: int = 10) -> list[str]:
    """
    Canonical tags first, then the rest: '#'-prefixed, letters/digits/underscore only, deduplicated
    case-insensitively ("#gta6" and "#GTA6" are the same tag, and the canonical spelling wins).
    """
    out, seen = [], set()
    for group in (canonical, extra):
        for tag in group or ():
            body = re.sub(r"[^\w]+", "", str(tag or "").lstrip("#"))
            if not body or body.lower() in seen:
                continue
            seen.add(body.lower())
            out.append("#" + body)
            if len(out) >= limit:
                return out
    return out


def prompt_context(profile: dict | str | None, title: str = "") -> str:
    """
    The game block of the LLM prompt: which moments mean something in this game, how to name one when nobody
    speaks over it, and what to put in the title and hashtags (structure from PeakCut's GAME_RULES).
    title is the stream's own title, already reduced to one line by the caller.
    """
    resolved = profile if isinstance(profile, dict) else resolve_game(profile)
    taxonomy = resolved.get("taxonomy") or ()
    name = resolved.get("name", "the game")
    tags = ", ".join(tags_for(resolved, 4))
    head = f"This stream is {name}" + (f' ("{title}")' if title else "")
    return (
        f"{head}. The moments that only mean something with this game in mind: {', '.join(taxonomy)}.\n"
        "- Pick the play, the death, the scare, the glitch, the discovery, the clutch, or the streamer "
        "reacting to one.\n"
        "- The hook is what was said at that moment or, when nobody speaks (aiming, holding an angle), the "
        'name of what happened in two or three words: "1 VS 3", "THE SHOT", "HE GOT ME".\n'
        f"- The title names the game when the clip is about it; the hashtags include {tags}.\n"
        "- Judge what happened, why it matters, whether the setup and payoff are clear, whether the reaction "
        "is captured, and whether someone who has not watched the stream can understand the moment."
    )


def contextual_hook(profile: dict | str | None, category: str = "", seed: float = 0.0, exclude=()) -> str:
    resolved = profile if isinstance(profile, dict) else resolve_game(profile)
    hooks = tuple(resolved.get("hooks") or GENERIC_PROFILE["hooks"])
    if not hooks:
        hooks = GENERIC_PROFILE["hooks"]
    pool = list(hooks)
    category = str(category or "").lower()
    category_prefixes = {
        "funny": ("THAT NPC", "PURE CHAOS", "WORST TIMING"),
        "rage": ("WORST TIMING", "NO WAY OUT", "WHAT JUST HAPPENED"),
        "clutch": ("THE CLUTCH", "NO WAY OUT", "THE PERFECT ESCAPE"),
        "fail": ("WORST TIMING", "THAT DID NOT GO TO PLAN"),
        "reaction": ("WHAT JUST HAPPENED", "NO WAY"),
    }
    pool = [h for h in category_prefixes.get(category, ()) if h in pool] + pool
    exclude = {str(h).upper() for h in (exclude or ())}
    index = int(abs(seed) * 10) % max(1, len(pool))
    pool = pool[index:] + pool[:index]
    for hook in pool:
        if hook.upper() not in exclude:
            words = hook.split()
            if 2 <= len(words) <= 5:
                return hook.upper()
    return "WHAT JUST HAPPENED"
