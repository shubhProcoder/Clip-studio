"""
Dashboard publishing (this PC only; the server answers 403 on the network): Publish one clip, Schedule week,
and Settings > Publishing (connect/disconnect). Uploads run on background threads, one per clip (409 while
one runs). Results are kept per clip in the job (job['publish'][key][platform]); tokens stay in
paths.CONFIG_DIR and never appear in a response (only the connected account name).
"""

import threading
import time
from datetime import datetime, timezone

from streamclipper.dashboard.jobs import _choice, parse_bool
from streamclipper.dashboard.library import _inside, clip_key, load_doc
from streamclipper.publish import PLATFORMS
from streamclipper.publish import http as pub_http
from streamclipper.publish import instagram as ig
from streamclipper.publish import store as pstore
from streamclipper.publish import youtube as yt
from streamclipper.publish.meta import clip_meta, instagram_caption, youtube_meta
from streamclipper.publish.queue import QueueStore
from streamclipper.publish.run import publish_instagram, publish_pack, publish_youtube
from streamclipper.publish.schedule import check_future, parse_start, parse_when, schedule_week
from streamclipper.util import ToolError

LAN_MSG = "Publishing works only on this PC."
DONE_STATES = ("uploaded", "scheduled", "published", "packed")
SCHEDULE_PLATFORMS = ("youtube", "pack")
MAX_TEXT = 5000


class Busy(Exception):
    """A publish run for this clip (or a schedule for this project) is already running (HTTP 409)."""


def _text(body: dict, k: str) -> str:
    v = body.get(k)
    if v is None:
        return ""
    if not isinstance(v, str) or len(v) > MAX_TEXT:
        raise ToolError(f"{k} must be text (at most {MAX_TEXT} characters).", "shorten it")
    return v


class Publisher:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.running = {}  # (job id, clip key) -> platform being published
        self.schedules = set()  # job ids with a running Schedule week
        self.connecting = {"running": False, "error": ""}
        self.queue = QueueStore(store.ws)
        threading.Thread(target=self._queue_loop, daemon=True, name="publish-queue").start()

    # ---- settings ---------------------------------------------------------------------------------------

    def settings(self) -> dict:
        st = pstore.load_settings()
        y = yt.connected()
        with self.lock:
            conn = dict(self.connecting)
        return {
            "youtube": {
                "client_file": st.get("youtube_client", ""),
                "connected": y["connected"],
                "account": y["account"],
                "connecting": conn["running"],
                "error": conn["error"],
            },
            "instagram": {
                "credentials_file": st.get("instagram_credentials", ""),
                "connected": bool(st.get("instagram_credentials")),
                "account": st.get("instagram_account", ""),
            },
            "config_dir": str(pstore.config_dir()),
        }

    def action(self, platform: str, what: str, body: dict) -> tuple:
        """(HTTP status, response) of Settings > Publishing buttons."""
        if (platform, what) == ("youtube", "connect"):
            path = _text(body, "client_file").strip().strip('"')
            client = yt.load_client(path)
            with self.lock:
                if self.connecting["running"]:
                    raise Busy("YouTube sign-in is already open in the browser. Finish it or wait 5 minutes.")
                self.connecting.update(running=True, error="")
            pstore.save_settings(youtube_client=path)

            def run():
                err = ""
                try:
                    yt.authorize(client, pub_http.make_transport())
                except ToolError as e:
                    err = pstore.redact(str(e))
                except Exception as e:  # never kill the dashboard
                    err = pstore.redact(f"Sign-in failed: {type(e).__name__}: {e}")[:300]
                with self.lock:
                    self.connecting.update(running=False, error=err)

            threading.Thread(target=run, daemon=True).start()
            return 202, self.settings()
        if (platform, what) == ("youtube", "disconnect"):
            yt.disconnect()
            return 200, self.settings()
        if (platform, what) == ("instagram", "connect"):
            path = _text(body, "credentials_file").strip().strip('"')
            creds = ig.load_credentials(path)
            name = ig.account_name(creds, pub_http.make_transport())
            pstore.save_settings(instagram_credentials=path, instagram_account=name or "connected")
            return 200, self.settings()
        if (platform, what) == ("instagram", "disconnect"):
            pstore.save_settings(instagram_credentials="", instagram_account="")
            return 200, self.settings()
        raise KeyError(what)

    # ---- clips ------------------------------------------------------------------------------------------

    def _clips(self, job_id: str) -> list:
        """[(key, clip, mp4)] of the job's rendered clips that are not deleted, best rank first."""
        job = self.store.get(job_id)
        out = self.store.job_dir(job_id) / "out"
        deleted = set(job.get("deleted") or [])
        res = []
        for c in sorted(load_doc(out).get("clips") or [], key=lambda c: c.get("rank") or 0):
            key = clip_key(c)
            mp4 = (out / str(c.get("mp4") or "")).resolve() if c.get("mp4") else None
            if key in deleted or not mp4 or not mp4.is_file() or not _inside(mp4, out):
                continue
            res.append((key, c, mp4))
        return res

    def _clip(self, job_id: str, key: str):
        for k, c, mp4 in self._clips(job_id):
            if k == key:
                return c, mp4
        raise ToolError("This clip has no MP4 to publish.", "render the clip first")

    def _save(self, job_id: str, key: str, platform: str, entry: dict) -> None:
        entry = pstore.redact({**entry, "time": time.time()})
        self.store.mutate(
            job_id, lambda j: j.setdefault("publish", {}).setdefault(key, {}).__setitem__(platform, entry)
        )

    def status(self, job_id: str, key: str) -> dict:
        with self.lock:
            running = self.running.get((job_id, key))
        return {"running": running or "", "publish": (self.store.get(job_id).get("publish") or {}).get(key) or {}}

    def decorate(self, job_id: str, clips: list) -> list:
        """Mark clips with a running upload (their stored entries come from library.list_clips)."""
        with self.lock:
            running = {k: p for (j, k), p in self.running.items() if j == job_id}
        for c in clips:
            if c["key"] in running:
                c["publish"] = {**(c.get("publish") or {}), running[c["key"]]: {"state": "uploading"}}
        return clips

    def queue_overview(self, creator_id: str = None) -> dict:
        return self.queue.overview(creator_id)

    def queue_accounts(self) -> dict:
        accounts = pstore.load_accounts()
        st = pstore.load_settings()
        if st.get("youtube_client"):
            if not any(a["id"] == "youtube" for a in accounts):
                accounts.append(
                    {
                        "id": "youtube",
                        "platform": "youtube",
                        "name": "YouTube",
                        "connected": yt.connected()["connected"],
                    }
                )
        if st.get("instagram_credentials"):
            if not any(a["id"] == "instagram" for a in accounts):
                accounts.append(
                    {
                        "id": "instagram",
                        "platform": "instagram",
                        "name": st.get("instagram_account") or "Instagram",
                        "connected": True,
                    }
                )
        return {"accounts": accounts}

    def queue_enqueue(
        self,
        job_id: str,
        key: str,
        platforms: list,
        creator_id: str = "default",
        scheduled_at: float = None,
        interval_hours: float = 3.0,
        daily_caps: dict = None,
    ) -> list:
        job = self.store.get(job_id)
        creator_id = str(job.get("creator_id") or creator_id or "default")
        made = []
        caps = daily_caps or {}
        for platform in platforms:
            platform = str(platform).strip().lower()
            if platform not in ("youtube", "instagram", "tiktok", "pack"):
                continue
            account_id = "pack" if platform in ("tiktok", "pack") else platform
            account_name = account_id.title()
            item = self.queue.enqueue(
                job_id,
                key,
                "pack" if platform == "pack" else platform,
                account_id=account_id,
                creator_id=creator_id,
                scheduled_at=scheduled_at,
                interval_hours=interval_hours,
                daily_cap=int((caps or {}).get(platform, 4)),
                account_name=account_name,
            )
            if item:
                made.append(item)
        return made

    def queue_retry(self, queue_id: str) -> dict:
        return self.queue.retry(queue_id)

    def queue_cancel(self, queue_id: str) -> dict:
        return self.queue.cancel(queue_id)

    def _run_queue_item(self, item: dict) -> None:
        job_id, key, platform = item["job_id"], item["clip_key"], item["platform"]
        try:
            publish_at = None
            if item.get("scheduled_at"):
                publish_at = datetime.fromtimestamp(float(item["scheduled_at"]), timezone.utc)
            fn = self._prepare(job_id, key, platform, {}, publish_at)
            res = fn()
            if res.get("platform") == "pack":
                self.queue.mark_packed(item["id"], str(res.get("folder") or ""))
                return
            post_id = str(res.get("video_id") or res.get("media_id") or "")
            url = str(res.get("url") or "")
            self.queue.mark_posted(item["id"], post_id, url)
        except ToolError as e:
            self.queue.mark_failed(item["id"], str(e))
        except Exception as e:
            self.queue.mark_failed(item["id"], f"{type(e).__name__}: {e}"[:500])

    def _queue_loop(self) -> None:
        while True:
            time.sleep(15)
            for item in self.queue.due():
                claimed = self.queue.claim(item["id"])
                if claimed:
                    try:
                        self._run_queue_item(claimed)
                    except Exception:
                        self.queue.mark_failed(item["id"], "Publishing queue worker failed.")

    def _prepare(self, job_id: str, key: str, platform: str, body: dict, publish_at=None):
        """Check everything before a thread starts (bad input = HTTP 400); returns the publish function."""
        clip, mp4 = self._clip(job_id, key)
        base = clip_meta(clip, _text(body, "title"), _text(body, "description"), _text(body, "tags"))
        out = self.store.job_dir(job_id) / "out"
        if platform == "youtube":
            privacy = _choice(body.get("privacy"), list(yt.PRIVACY), "privacy", "private")
            when = publish_at
            if when is None and _text(body, "publish_at").strip():
                when = check_future(parse_when(body["publish_at"]))
            yt.video_body(youtube_meta(base), privacy, when)  # validates the time and the privacy
            if not yt.connected()["connected"]:
                raise ToolError("YouTube is not connected.", "connect it in Settings > Publishing")
            return lambda: publish_youtube(mp4, base, privacy, when, transport=pub_http.make_transport())
        if platform == "instagram":
            creds_file = pstore.load_settings().get("instagram_credentials", "")
            if not creds_file:
                raise ToolError("Instagram is not connected.", "connect it in Settings > Publishing")
            caption = _text(body, "caption").strip() or instagram_caption(base)
            if len(caption) > 2200:
                raise ToolError("Instagram captions can have at most 2,200 characters.", "shorten the caption")
            return lambda: publish_instagram(mp4, caption, creds_file, transport=pub_http.make_transport())
        return lambda: publish_pack(mp4, base, out, key, publish_at)

    @staticmethod
    def _entry(res: dict) -> dict:
        p = res.get("platform")
        if p == "youtube":
            state = "scheduled" if res.get("publish_at") else "uploaded"
            keep = ("video_id", "url", "privacy", "publish_at")
        elif p == "instagram":
            state, keep = "published", ("media_id",)
        else:
            state, keep = "packed", ("folder",)
        return {"state": state, **{k: res.get(k) for k in keep}}

    def _run_one(self, job_id: str, key: str, platform: str, fn) -> None:
        try:
            entry = self._entry(fn())
        except ToolError as e:
            entry = {"state": "failed", "message": str(e), "hint": e.hint or ""}
        except Exception as e:  # a bug must not kill the dashboard; the card shows the message
            entry = {"state": "failed", "message": f"{type(e).__name__}: {e}"[:300], "hint": ""}
        self._save(job_id, key, platform, entry)

    def start(self, job_id: str, key: str, body: dict) -> dict:
        platform = _choice(body.get("platform"), list(PLATFORMS), "platform")
        if not platform:
            raise ToolError("Pick where to publish.", "YouTube, Instagram or TikTok pack")
        fn = self._prepare(job_id, key, platform, body)
        with self.lock:  # check and claim in one step: two quick clicks start one upload
            if (job_id, key) in self.running:
                raise Busy("This clip is already being published. Wait for it to finish.")
            self.running[(job_id, key)] = platform

        def run():
            try:
                self._run_one(job_id, key, platform, fn)
            finally:
                with self.lock:
                    self.running.pop((job_id, key), None)

        threading.Thread(target=run, daemon=True).start()
        return {"started": True, "platform": platform, **self.status(job_id, key)}

    # ---- schedule week ----------------------------------------------------------------------------------

    def schedule(self, job_id: str, body: dict) -> tuple:
        platform = _choice(body.get("platform"), list(SCHEDULE_PLATFORMS), "platform", "youtube")
        start = parse_start(_text(body, "start"))
        times = _text(body, "times").strip() or None
        per_day = body.get("per_day", 1)
        confirm = parse_bool(body.get("confirm"), "confirm", False)
        job = self.store.get(job_id)
        done = job.get("publish") or {}
        marks = job.get("marks") or {}
        todo = [
            (k, c, m)
            for k, c, m in self._clips(job_id)
            if marks.get(k) != "rejected" and (done.get(k) or {}).get(platform, {}).get("state") not in DONE_STATES
        ]
        plan = schedule_week(todo, start, per_day, times)
        slots = [
            {"key": k, "title": c.get("title") or c.get("hook_headline") or k,
             "at": s["at"].isoformat(timespec="minutes"),
             "label": s["at"].strftime("%a %d %b %H:%M")}
            for s in plan["slots"]
            for k, c, _m in [s["clip"]]
        ]  # fmt: skip
        res = {"platform": platform, "slots": slots, "left": len(plan["left"]), "started": False}
        if not confirm or not slots:
            return 200, res
        fns = [(s["clip"][0], self._prepare(job_id, s["clip"][0], platform, {}, s["at"])) for s in plan["slots"]]
        with self.lock:
            if job_id in self.schedules or any((job_id, k) in self.running for k, _f in fns):
                raise Busy("A schedule or an upload of these clips is still running. Wait for it to finish.")
            self.schedules.add(job_id)
            for k, _f in fns:
                self.running[(job_id, k)] = platform

        def run():
            try:
                for k, fn in fns:
                    try:
                        self._run_one(job_id, k, platform, fn)
                    finally:
                        with self.lock:
                            self.running.pop((job_id, k), None)
            finally:
                with self.lock:
                    self.schedules.discard(job_id)
                    for k, _f in fns:
                        self.running.pop((job_id, k), None)

        threading.Thread(target=run, daemon=True).start()
        return 202, {**res, "started": True}
