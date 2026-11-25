"""
Posting times: a best-time suggestion (a common heuristic, not measured on your audience) and schedule_week,
which spreads clips over the next days at fixed local times. YouTube publishes a scheduled upload itself
(status.publishAt); for the TikTok pack the planned time is written into best_time.txt.
"""

import re
from datetime import date, datetime, time, timedelta, timezone

from streamclipper.publish.store import PublishError

HEURISTIC = "a common heuristic, not measured on your audience"
WEEKDAY_SLOTS = ("18:00", "19:30", "21:00")  # weekday evenings 18:00-21:00
WEEKEND_SLOTS = ("11:00", "12:00", "13:00")  # weekend late mornings 11:00-13:00
MAX_PER_DAY = 6
MIN_LEAD = timedelta(minutes=15)  # YouTube needs publishAt in the future
DAYS = 7


def best_slots(d: date) -> list:
    return list(WEEKEND_SLOTS if d.weekday() >= 5 else WEEKDAY_SLOTS)


def parse_times(text) -> list:
    """'18:00, 20:30' -> [time(18, 0), time(20, 30)] (sorted, no duplicates)."""
    out = set()
    for part in re.split(r"[\s,;]+", str(text or "").strip()):
        if not part:
            continue
        m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", part)
        if not m:
            raise PublishError(f"Unknown time: {part}", "use 24-hour times like 18:00, 20:30")
        out.add(time(int(m.group(1)), int(m.group(2))))
    return sorted(out)


def parse_start(text, today: date = None) -> date:
    today = today or date.today()
    s = str(text or "").strip().lower()
    if s in ("", "tomorrow"):
        return today + timedelta(days=1)
    if s == "today":
        return today
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise PublishError(f"Unknown start date: {text}", "use YYYY-MM-DD, today or tomorrow") from None


def parse_when(text, tz=None) -> datetime:
    """An ISO date-time ('2026-11-21T18:00', '... +05:30', '...Z') as an aware datetime (no zone = local)."""
    s = str(text or "").strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        raise PublishError(f"Unknown date and time: {text}", "use YYYY-MM-DDTHH:MM, e.g. 2026-11-21T18:00") from None
    return _aware(dt, tz)


def _aware(dt: datetime, tz=None) -> datetime:
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=tz) if tz is not None else dt.astimezone()  # naive = this PC's local time (DST-aware)


def rfc3339(dt: datetime) -> str:
    """UTC RFC 3339 for YouTube's publishAt, e.g. 2026-11-21T12:30:00Z."""
    return _aware(dt).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_future(dt: datetime, now: datetime = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if dt - now < MIN_LEAD:
        raise PublishError("The publish time must be at least 15 minutes from now.", "pick a later date or time")
    return dt


def best_time_text(now: datetime = None, planned: datetime = None) -> str:
    """Text for best_time.txt: the heuristic, the next good slot, and the planned time when one was set."""
    now = _aware(now or datetime.now())
    nxt = None
    for k in range(DAYS + 1):
        d = now.date() + timedelta(days=k)
        for s in best_slots(d):
            at = _aware(datetime.combine(d, time.fromisoformat(s)), now.tzinfo)
            if at - now >= MIN_LEAD:
                nxt = at
                break
        if nxt:
            break
    lines = [
        f"Suggested posting times ({HEURISTIC}):",
        "  weekdays 18:00-21:00, weekends 11:00-13:00, your local time.",
        f"Next good slot: {nxt.strftime('%a %d %b %H:%M')}" if nxt else "",
    ]
    if planned:
        lines.append(f"Planned in your schedule: {_aware(planned).strftime('%a %d %b %Y %H:%M')}")
    lines.append("Check your own analytics after a few posts and move the times to when your viewers are online.")
    return "\n".join(x for x in lines if x) + "\n"


def schedule_week(clips: list, start: date, per_day: int = 1, times=None, tz=None, now: datetime = None) -> dict:
    """
    Spread clips (best first) over 7 days from start: per_day slots a day at the given times (default the
    heuristic slots of each day). Slots less than 15 minutes ahead are skipped.
    -> {"slots": [{"clip": clip, "at": aware datetime}], "left": [clips that did not fit]}
    """
    try:
        per_day = int(per_day)
    except (TypeError, ValueError):
        raise PublishError("Clips per day must be a number.", f"use 1-{MAX_PER_DAY}") from None
    fixed = parse_times(times) if isinstance(times, str) else sorted(times or [])
    limit = len(fixed) if fixed else len(WEEKDAY_SLOTS)
    if not 1 <= per_day <= min(MAX_PER_DAY, limit):
        raise PublishError(
            f"{per_day} clips a day needs {per_day} posting times.",
            f"use 1-{min(MAX_PER_DAY, limit)} clips a day, or give more times (e.g. 12:00, 18:00, 21:00)",
        )
    now = _aware(now or datetime.now(timezone.utc))
    queue = list(clips)
    slots = []
    for k in range(DAYS):
        d = start + timedelta(days=k)
        day_times = fixed or [time.fromisoformat(s) for s in best_slots(d)]
        for t in day_times[:per_day]:
            if not queue:
                break
            at = _aware(datetime.combine(d, t), tz)
            if at - now < MIN_LEAD:
                continue
            slots.append({"clip": queue.pop(0), "at": at})
    return {"slots": slots, "left": queue}
