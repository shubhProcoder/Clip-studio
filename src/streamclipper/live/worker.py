"""Background renders for live mode: one worker thread, so ingest and analysis never wait for ffmpeg."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from streamclipper.audio import _mmss
from streamclipper.commands import _link_outputs, make_stream_clip, save_clips
from streamclipper.discovery import clips_document
from streamclipper.gate import render_and_gate
from streamclipper.live.engine import shift_clip
from streamclipper.story import suggest_hook
from streamclipper.util import SkipClip, ToolError, fwd


class Worker:
    """
    Job = window MP4 + clip in stream time -> commands.make_stream_clip on the window (no download) ->
    gate.render_and_gate (MP4 + thumbnail) -> clips.json/clips.csv/index.html refreshed -> console line.
    """

    def __init__(
        self,
        out_dir: Path,
        opts: dict,
        assets: dict,
        doc_info: dict,
        render: bool = True,
        no_draft: bool = True,
        portable: bool = False,
        draft_root: str = "",
        prefix: str = "LIVE",
        hook_override: str = "",
        llm: str = "none",
        llm_model: str = "",
        make_clip=make_stream_clip,
        render_fn=render_and_gate,
        moment_lock=None,
    ):
        self.out = Path(out_dir)
        self.opts, self.assets, self.doc_info = opts, assets, doc_info
        self.render, self.no_draft, self.portable, self.draft_root = render, no_draft, portable, draft_root
        self.prefix, self.hook_override = prefix, hook_override
        self.llm, self.llm_model = llm, llm_model
        self.make_clip, self.render_fn = make_clip, render_fn
        self.mlock = moment_lock or threading.Lock()  # the engine's lock: it replaces only unstarted moments
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-render")
        self.lock = threading.Lock()
        self.clips = []  # finished clips (stream time), in the order they were made
        self.extra = []  # per clip: {mp4, thumb, latency_s, gate}
        self.failed = 0
        self.futures = []
        self.windows_deleted = 0
        self._win_refs = {}  # window file -> jobs queued on it (a replaced moment can share the file name)

    def submit(self, moment, window: Path, t0: float, t1: float, payoff_wall: float):
        key = str(Path(window).resolve())
        with self.lock:
            self._win_refs[key] = self._win_refs.get(key, 0) + 1
        f = self.pool.submit(self._job_and_cleanup, moment, Path(window), t0, t1, payoff_wall)
        self.futures.append(f)
        return f

    def _job_and_cleanup(self, moment, window: Path, t0: float, t1: float, payoff_wall: float):
        self._last_plan, self._last_ok = None, False
        try:
            return self._job(moment, window, t0, t1, payoff_wall)
        finally:
            self._release_window(window, self._last_plan, self._last_ok or moment.cancelled)

    def _release_window(self, window: Path, plan: dict, drop: bool):
        """
        After a job: delete its window files when nothing needs them any more. Drafts and clip.json renders
        use the panel files (panels/), never the windows, so a window goes once its clip is made (the MP4
        passed the gate, or no MP4 was asked for) or its moment was replaced. A failed or skipped clip keeps
        its window for a re-run. Only files of this run inside <out>/windows are deleted: the exported window
        (live_<a>_<b>.mp4) and the window copy make_stream_clip cut from it (<same stem>_win_*.mp4).
        """
        key = str(Path(window).resolve())
        with self.lock:
            left = self._win_refs.get(key, 1) - 1
            if left > 0:
                self._win_refs[key] = left
                return
            self._win_refs.pop(key, None)
        if not drop:
            return
        wdir = (self.out / "windows").resolve()
        stem = Path(window).stem
        paths = [Path(window)]
        sw = (plan or {}).get("source_window") or {}
        if sw.get("path"):
            paths.append(Path(sw["path"]))
        for p in paths:
            p = p.resolve()
            if p.parent != wdir or p.suffix != ".mp4" or not p.name.startswith(stem):
                continue
            try:
                p.unlink()
                self.windows_deleted += 1
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"Live: could not delete {p.name}: {e}")

    def shutdown(self):
        self.pool.shutdown(wait=True)

    def _llm_copy(self, clip: dict, n: int):
        """LLM title/description/hashtags for this clip only (in the worker; ingest never waits)."""
        if self.llm in ("", "none") or not clip.get("words"):
            return None
        from streamclipper.llm import analyze_transcript

        try:
            segs = analyze_transcript(
                clip["words"],
                self.llm,
                self.llm_model,
                self.doc_info.get("language") or "en",
                None,
                self.out / "llm" / f"clip_{n:02d}",
                self.doc_info.get("st", {}).get("content") or "gaming",
                self.doc_info.get("st", {}).get("game"),
            )
        except ToolError as e:
            print(f"LLM skipped for clip {n}: {e}")
            return None
        inside = [s for s in segs if clip["start"] - 5 <= s["payoff_t"] <= clip["end"] + 5]
        return min(inside, key=lambda s: abs(s["payoff_t"] - clip["peak"])) if inside else None

    def _job(self, moment, window: Path, t0: float, t1: float, payoff_wall: float):
        with self.mlock:
            if moment.cancelled:
                return None
            moment.started = True
        clip = moment.clip
        n = len(self.clips) + self.failed + 1
        name = f"{self.prefix}_{n:02d}"
        try:
            used = {c.get("hook_headline") for c in self.clips}
            if clip.get("hook_headline") in used:  # hooks stay unique over the whole live run
                clip = dict(clip, hook_headline=suggest_hook(clip.get("words") or [], clip, clip.get("category"), used))
            seg = self._llm_copy(clip, n)
            if seg:
                clip = dict(clip, llm=seg)
            local = shift_clip(clip, -t0)
            plan = self.make_clip(
                local,
                n - 1,
                name,
                window,
                "",
                t1 - t0,
                self.out,
                self.opts,
                self.assets,
                {},
                self.hook_override,
                draft_root=self.draft_root,
                no_draft=self.no_draft,
                portable=self.portable,
            )
            self._last_plan = plan
            if local.get("caption_style"):  # the style make_stream_clip picked (auto = from the category)
                clip = dict(clip, caption_style=local["caption_style"])
            ok = None
            mp4 = self.out / f"{plan['name']}.mp4"
            if self.render:
                ok = self.render_fn(plan, mp4)
            self._last_ok = ok is True or not self.render
        except SkipClip as e:
            self.failed += 1
            print(f"Live: moment at {_mmss(clip['peak'])} skipped: {e}")
            return None
        except Exception as e:  # one bad clip never stops the run
            self.failed += 1
            print(f"Live: clip {n} failed ({type(e).__name__}: {str(e)[:200]})")
            return None
        latency = round(time.time() - payoff_wall, 1)
        moment.done = True
        with self.lock:
            self.clips.append(dict(clip, rank=len(self.clips) + 1))
            self.extra.append({"mp4": mp4 if self.render else None, "latency_s": latency, "gate": ok})
            doc = self.write_doc()
        entry = doc["clips"][-1]
        where = fwd(mp4) if self.render else fwd(self.out / f"{plan['name']}.clip.json")
        gate = "" if ok is None else (" | gate PASS" if ok else " | gate FAIL")
        print(
            f"CLIP {entry['rank']:02d} | {_mmss(clip['peak'])} into stream | score {clip['score']} | "
            f"'{entry['title']}' | {where} | ready {latency:.0f} s after the moment{gate}"
        )
        return entry

    def write_doc(self) -> dict:
        info = self.doc_info
        res = {
            "clips": self.clips,
            "duration": info["duration"](),
            "transcribed": info["transcribed"],
            "language": info.get("language") or "auto",
            "elapsed": round(time.time() - info["t_start"], 1),
            "asr": info.get("asr", "none"),
            "llm": info.get("llm", "none"),
            "candidates": None,
        }
        doc = clips_document(res, info["label"], info["title"], info["st"])
        doc["mode"] = "live"
        for c, ex in zip(doc["clips"], self.extra, strict=True):
            c["latency_s"] = ex["latency_s"]
            c["gate"] = ex["gate"]
            if ex["mp4"] is not None:
                _link_outputs(c, ex["mp4"], self.out)
        save_clips(self.out / "clips.json", doc)
        return doc
