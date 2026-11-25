"""One publish action per platform (used by the CLI and the dashboard) and the `publish` command."""

import json
import time
from datetime import datetime
from pathlib import Path

from streamclipper.publish import http
from streamclipper.publish import instagram as ig
from streamclipper.publish import pack as pk
from streamclipper.publish import youtube as yt
from streamclipper.publish.meta import all_clips, caption_meta, clip_meta, instagram_caption, load_clip, youtube_meta
from streamclipper.publish.schedule import parse_start, parse_when, schedule_week
from streamclipper.publish.store import PublishError, redact
from streamclipper.state import write_json_atomic
from streamclipper.util import fwd

SCHEDULE_FILE = "publish_schedule.json"
SCHEDULE_PLATFORMS = ("youtube", "pack")


def publish_youtube(
    mp4, base: dict, privacy="private", publish_at=None, client_file="", transport=None, dry_run=False,
    authorize_fn=None, sleep=time.sleep, now=None,
) -> dict:  # fmt: skip
    body = yt.video_body(youtube_meta(base), privacy, publish_at, now)
    if dry_run:
        return {
            "dry_run": True,
            "platform": "youtube",
            "mp4": str(mp4),
            "bytes": Path(mp4).stat().st_size,
            "request": body,
        }
    transport = transport or http.make_transport()
    token = yt.get_token(transport, client_file, authorize_fn)
    return yt.upload(mp4, body, transport, token, sleep)


def publish_instagram(mp4, caption: str, creds_file="", transport=None, dry_run=False, sleep=time.sleep) -> dict:
    request = {"media_type": "REELS", "upload_type": "resumable", "caption": caption}
    if dry_run:
        return {
            "dry_run": True,
            "platform": "instagram",
            "mp4": str(mp4),
            "bytes": Path(mp4).stat().st_size,
            "request": request,
        }
    creds = ig.load_credentials(creds_file)
    return ig.upload(mp4, caption, creds, transport or http.make_transport(), sleep)


def publish_pack(mp4, base: dict, out_dir, name="", planned=None) -> dict:
    folder = pk.make_pack(mp4, base, out_dir, name, planned)
    return {"platform": "pack", "folder": str(folder)}


def describe(res: dict) -> str:
    if res.get("platform") == "youtube":
        if res.get("publish_at"):
            return f"Scheduled on YouTube (private until {res['publish_at']}): {res.get('url')}"
        return f"Uploaded to YouTube ({res.get('privacy') or 'private'}): {res.get('url')}"
    if res.get("platform") == "instagram":
        return f"Published to Instagram Reels (media id {res.get('media_id')})"
    return f"Publish pack ready: {fwd(res.get('folder', ''))}"


# ---- CLI --------------------------------------------------------------------------------------------------


def _source(args):
    """(clip dict, mp4 Path) from --mp4 or --clips/--rank."""
    if getattr(args, "mp4", ""):
        mp4 = Path(args.mp4).resolve()
        if not mp4.is_file():
            raise PublishError(f"MP4 not found: {mp4}", "pass a rendered clip with --mp4")
        return {"title": mp4.stem.replace("_", " ")}, mp4
    if getattr(args, "clips", ""):
        return load_clip(args.clips, args.rank or 1)
    raise PublishError("Pass --mp4 <clip.mp4> or --clips clips.json [--rank N].", "streamclipper publish --help")


def _base(args, clip: dict) -> dict:
    return clip_meta(clip, getattr(args, "title", ""), getattr(args, "description", ""), getattr(args, "tags", ""))


def _print_dry(res: dict) -> None:
    print(json.dumps(redact(res), indent=2, ensure_ascii=False))
    print("Dry run: nothing was sent and no sign-in was started.")


def _schedule(args) -> None:
    if args.platform not in SCHEDULE_PLATFORMS:
        raise PublishError(
            "Instagram's API has no scheduled posts.", "schedule YouTube, or make publish packs and post at the times"
        )
    clips = all_clips(args.clips)
    if not clips:
        raise PublishError("No rendered clip in clips.json.", "render the clips first")
    plan = schedule_week(clips, parse_start(args.start), args.per_day, args.times or None)
    base_dir = Path(args.out).resolve() if args.out else Path(args.clips).resolve().parent
    out = base_dir / SCHEDULE_FILE
    rows = []
    for s in plan["slots"]:
        clip, mp4 = s["clip"]
        rows.append(
            {
                "rank": clip.get("rank"),
                "mp4": fwd(mp4),
                "platform": args.platform,
                "at": s["at"].isoformat(timespec="minutes"),
                "state": "planned",
            }
        )
    doc = {"created": datetime.now().astimezone().isoformat(timespec="seconds"), "dry_run": bool(args.dry_run)}
    write_json_atomic(out, {**doc, "slots": rows, "left": [c.get("rank") for c, _m in plan["left"]]})
    for r in rows:
        print(f"  #{r['rank']:<3} {r['at']}  {args.platform}")
    if plan["left"]:
        print(f"{len(plan['left'])} clip(s) did not fit into the week (add --per-day or more --times).")
    if args.dry_run:
        print(f"Dry run: plan saved to {fwd(out)}; nothing was sent and no sign-in was started.")
        return
    transport = http.make_transport() if args.platform == "youtube" else None
    for row, s in zip(rows, plan["slots"], strict=True):
        clip, mp4 = s["clip"]
        base = _base(args, clip)
        try:
            if args.platform == "youtube":
                res = publish_youtube(mp4, base, args.privacy, s["at"], args.youtube_credentials, transport)
            else:
                res = publish_pack(mp4, base, base_dir, mp4.stem, s["at"])
            row.update(state="scheduled" if args.platform == "youtube" else "packed", result=res)
            print(describe(res))
        except PublishError as e:
            row.update(state="failed", error=str(e))
            print(f"FAILED #{row['rank']}: {e}")
        write_json_atomic(out, {**doc, "slots": rows, "left": [c.get("rank") for c, _m in plan["left"]]})
    print(f"Schedule saved: {fwd(out)}")


def cmd(args) -> None:
    what = args.platform_cmd
    if what == "schedule":
        return _schedule(args)
    if what == "pack":
        if getattr(args, "mp4", "") or getattr(args, "rank", None):
            items = [_source(args)]
        else:
            items = all_clips(args.clips) if args.clips else []
            if not items:
                raise PublishError("No rendered clip found.", "pass --mp4 <clip.mp4> or a clips.json with MP4s")
        for clip, mp4 in items:
            base = caption_meta(args.caption) if args.caption else _base(args, clip)
            out = Path(args.out).resolve() if args.out else mp4.parent
            print(describe(publish_pack(mp4, base, out, mp4.stem)))
        return
    clip, mp4 = _source(args)
    base = _base(args, clip)
    if what == "youtube":
        when = parse_when(args.publish_at) if args.publish_at else None
        res = publish_youtube(mp4, base, args.privacy, when, args.youtube_credentials, dry_run=args.dry_run)
    else:
        caption = args.caption or instagram_caption(base)
        res = publish_instagram(mp4, caption, args.instagram_credentials, dry_run=args.dry_run)
    if res.get("dry_run"):
        return _print_dry(res)
    print(describe(res))
