"""`streamclipper live`: ingest -> ring buffer -> rolling analysis -> background renders."""

import json
import signal
import statistics
import time
from pathlib import Path

from streamclipper.audio import _mmss
from streamclipper.discovery import _resolve_asr
from streamclipper.draft.writer import guard_capcut, resolve_draft_root
from streamclipper.env import ensure_bundled_assets, find_capcut_local
from streamclipper.live.chat import LiveChat
from streamclipper.live.engine import CLIP_WORDS, Engine, LiveASR
from streamclipper.live.ingest import Ingest, channel_name, resolve_source
from streamclipper.live.ring import Ring
from streamclipper.live.worker import Worker
from streamclipper.options import OPT_CHOICES, resolve_options
from streamclipper.state import write_json_atomic
from streamclipper.util import ToolError, fwd, isolate_children, require_tool

WINDOW_PAD_S = 1.5
POLL_S = 1.0


class LiveRun:
    """Wires ring, ingest, engine, chat and worker together; run() blocks until the stream (or --stop-after) ends."""

    def __init__(
        self,
        ring,
        ingest,
        engine,
        worker,
        chat=None,
        stop_after_s: float = 0.0,
        out_dir: Path = None,
        max_disk_gb: float = 0.0,
    ):
        self.ring, self.ingest, self.engine, self.worker, self.chat = ring, ingest, engine, worker, chat
        self.stop_after_s = stop_after_s
        self.out = Path(out_dir) if out_dir else ring.dir.parent
        self.max_disk_gb = float(max_disk_gb or 0.0)
        self.next_disk_warn = 0.0
        self.done_segs = 0
        self.stopping = False
        self.t_start = time.time()
        self.next_status = 60.0

    def check_disk(self) -> float:
        """GB used by the output folder; a warning (at most every 10 min) when it is over --max-disk-gb."""
        used = sum(p.stat().st_size for p in self.out.rglob("*") if p.is_file()) / 1e9
        if self.max_disk_gb and used > self.max_disk_gb and time.time() >= self.next_disk_warn:
            self.next_disk_warn = time.time() + 600.0
            print(
                f"Live: WARNING: {fwd(self.out)} uses {used:.1f} GB (over --max-disk-gb {self.max_disk_gb:g}). "
                "The run goes on; move finished clips elsewhere or stop with Ctrl+C before the disk fills up."
            )
        return used

    def _chat_msgs(self):
        return self.chat.messages_with_users() if self.chat is not None else None

    def process_new(self) -> int:
        """Analyse every segment not seen yet; then emit ready moments and prune the ring."""
        self.ring.poll()
        new = self.ring.segments[self.done_segs :]
        for seg in new:
            self.engine.feed(seg)
            self.done_segs += 1
        if new:
            self.emit(self.engine.analyze(self._chat_msgs()))
            self.ring.prune()
            if self.ring.end >= self.next_status:
                self.next_status = self.ring.end + 60.0
                chat = f" | chat {self.chat.count} msgs" if self.chat is not None else ""
                print(
                    f"Live: buffered {_mmss(self.ring.end)} | {len(self.engine.words)} words in memory{chat} | "
                    f"{len(self.worker.clips)} clips ready, {len(self.worker.futures) - len(self.worker.clips)} queued"
                )
                if self.max_disk_gb:
                    self.check_disk()
        return len(new)

    def emit(self, ready: list):
        for _action, clip, replaced in ready:
            a = max(0.0, clip["start"] - WINDOW_PAD_S)
            b = min(self.ring.end, clip["end"] + WINDOW_PAD_S)
            win = self.out / "windows" / f"live_{int(a * 1000):09d}_{int(b * 1000):09d}.mp4"
            try:
                path, t0, t1 = self.ring.export_window(a, b, win)
            except ToolError as e:
                print(f"Live: moment at {_mmss(clip['peak'])} skipped: {e}")
                continue
            moment = self.engine.moment_for(clip)
            seg = self.ring.segment_at(clip["peak"])
            note = f" (replaces the queued moment at {_mmss(replaced.clip['peak'])})" if replaced else ""
            print(f"Live: moment at {_mmss(clip['peak'])} (score {clip['score']}) -> render queue{note}")
            self.worker.submit(moment, path, t0, t1, seg.wall if seg else time.time())

    def run(self) -> dict:
        self.ingest.start()
        if self.chat is not None:
            self.chat.start()
        try:
            self._loop()
        except KeyboardInterrupt:
            print("Ctrl+C: stopping the ingest; finishing the buffered segments and the queued renders ...")
            self.stopping = True
            self.ingest.stop()
            self._loop()
        return self.finish()

    def _loop(self):
        while True:
            n = self.process_new()
            if self.ingest.finished.is_set():
                if n or self.process_new():
                    continue
                self.emit(self.engine.analyze(self._chat_msgs(), final=True))
                return
            if not self.stopping and self.stop_after_s and time.time() - self.t_start >= self.stop_after_s:
                print(f"Live: --stop-after reached ({self.stop_after_s / 60:g} min); stopping the ingest ...")
                self.stopping = True
                self.ingest.stop()
            if not n:
                time.sleep(POLL_S)

    def finish(self) -> dict:
        self.ingest.join(30)
        if self.chat is not None:
            self.chat.stop()
        self.worker.shutdown()
        lat = [x["latency_s"] for x in self.worker.extra]
        summary = {
            "buffered_s": round(self.ring.end, 1),
            "wall_s": round(time.time() - self.t_start, 1),
            "segments": len(self.ring.segments),
            "segments_deleted": self.ring.deleted,
            "windows_deleted": getattr(self.worker, "windows_deleted", 0),
            "disk_gb": round(self.check_disk(), 2),
            "reconnects": self.ingest.reconnects,
            "end_reason": self.ingest.end_reason,
            "chat_messages": self.chat.count if self.chat is not None else 0,
            "clips": len(self.worker.clips),
            "failed_or_skipped": self.worker.failed,
            "latency_median_s": round(statistics.median(lat), 1) if lat else None,
            "latency_max_s": max(lat) if lat else None,
            "gate_pass": sum(1 for x in self.worker.extra if x["gate"]),
        }
        write_json_atomic(self.out / "live_summary.json", summary)
        print("LIVE SUMMARY")
        print(
            f"  stream buffered {_mmss(summary['buffered_s'])} in {_mmss(summary['wall_s'])} ({summary['end_reason']})"
        )
        print(f"  clips: {summary['clips']} ({summary['failed_or_skipped']} skipped or failed)")
        if lat:
            print(f"  ready after the moment: median {summary['latency_median_s']:.0f} s, max {max(lat):.0f} s")
        print(f"  reconnects: {summary['reconnects']} | chat messages: {summary['chat_messages']}")
        disk, gone = summary["disk_gb"], summary["windows_deleted"]
        print(f"  disk: {disk:.2f} GB in the output folder ({gone} window files deleted)")
        print(f"  report: {fwd(self.out / 'index.html')}" if summary["clips"] else "  no moment passed the bar")
        return summary


def _interrupt(_sig, _frame):
    raise KeyboardInterrupt


def stop_on_console_signals() -> None:
    """
    Live mode: every later ffmpeg/ffprobe child (renders, window exports, probes) starts in its own process
    group, so a console Ctrl+C reaches only streamclipper: the ingest stops and the clip being rendered and
    the queued ones are finished. Ctrl+Break (Windows) takes the same stop path as Ctrl+C.
    """
    isolate_children()
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _interrupt)


def cmd_live(args):
    kind = resolve_source(args.url)
    require_tool("ffmpeg")
    if args.buffer_min < 3:
        raise ToolError(f"--buffer-min {args.buffer_min:g} is too small.", "use at least 3 (default 15)")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    opts = resolve_options(args, {})
    if opts["hook_mode"] == "uncut-16x9-stack" and opts["layout"] == "split":
        opts["layout"] = "uncut-stack"
    portable = bool(args.portable_drafts)
    if not args.no_draft:
        if args.draft_root:
            Path(args.draft_root).mkdir(parents=True, exist_ok=True)
        guard_capcut(resolve_draft_root(args.draft_root), args.close_capcut)
    assets = ensure_bundled_assets(find_capcut_local(), install=not args.no_draft)
    asr = _resolve_asr({"asr": args.asr})
    keep_s = args.buffer_min * 60.0
    st = {
        "min_score": float(args.min_score),
        "content": opts["content"],
        "caption_style": opts["caption_style"],
        "game": str(getattr(args, "game", "") or "").strip()[:60],
    }
    ring = Ring(out / "buffer", keep_s)
    ingest = Ingest(args.url, kind, ring, out / "logs")
    transcriber = None
    if asr != "none":
        transcriber = LiveASR(args.whisper_model, args.device, args.language)
        transcriber.load()
    engine = Engine(
        st,
        transcriber,
        max_per_hour=args.max_clips_per_hour,
        keep_s=keep_s,
        tmp_dir=out / "tmp",
        clip_words=CLIP_WORDS + tuple(args.clip_word or ()),
    )
    chat = None
    if args.chat != "off" and kind in ("twitch", "youtube", "kick"):
        chat = LiveChat(kind, args.url, lambda: ring.wall_to_stream(time.time()), out)
    t_start = time.time()
    doc_info = {
        "label": args.url,
        "title": channel_name(args.url),
        "st": dict(engine.st, chat=chat is not None),
        "transcribed": transcriber is not None,
        "language": args.language if args.language not in ("", "auto") else "auto",
        "asr": "live" if transcriber else "none",
        "llm": args.llm,
        "duration": lambda: ring.end,
        "t_start": t_start,
    }
    worker = Worker(
        out,
        opts,
        assets,
        doc_info,
        render=args.render,
        no_draft=args.no_draft,
        portable=portable,
        draft_root=args.draft_root,
        prefix=args.prefix,
        hook_override=(args.hook_text or "").replace("\\n", "\n"),
        llm=args.llm,
        llm_model=args.llm_model,
        moment_lock=engine.lock,
    )
    stop_txt = f", stops after {args.stop_after:g} min" if args.stop_after else ", runs until the stream ends"
    print(
        f"Live: {kind} source {channel_name(args.url)} | buffer {args.buffer_min:g} min | min score "
        f"{args.min_score:g} | max {args.max_clips_per_hour} clips/hour{stop_txt} (Ctrl+C stops cleanly)"
    )
    run = LiveRun(ring, ingest, engine, worker, chat, args.stop_after * 60.0, out, args.max_disk_gb)
    run.t_start = t_start
    stop_on_console_signals()
    summary = run.run()
    (out / "live_settings.json").write_text(
        json.dumps({"kind": kind, "buffer_min": args.buffer_min, "min_score": args.min_score}, indent=2),
        encoding="utf-8",
    )
    if not args.render and summary["clips"]:
        print("Tip: add --render to also export ready-to-post MP4s.")
    return summary


def add_live_parser(sub, add_layout_flags, add_draft_flags, llm_providers, default_min_score):
    lv = sub.add_parser("live", help="Clip a live Twitch, YouTube or Kick stream while it is still running")
    lv.add_argument("--url", required=True, help="Live stream link (Twitch, YouTube Live, Kick), .m3u8, or a file")
    lv.add_argument("--out", required=True, help="Output folder (clips, clips.json/csv, index.html, buffer)")
    lv.add_argument("--buffer-min", type=float, default=15.0, help="Minutes of stream kept on disk (default 15)")
    lv.add_argument(
        "--min-score",
        type=float,
        default=default_min_score,
        help=f"Score threshold of the quality bar (default {default_min_score:g}, same as the VOD mode)",
    )
    lv.add_argument("--max-clips-per-hour", type=int, default=12, help="At most this many clips per stream hour")
    lv.add_argument(
        "--max-disk-gb",
        type=float,
        default=20.0,
        help="Warn when the output folder grows past this many GB (default 20; 0 = no check; a warning only)",
    )
    lv.add_argument("--stop-after", type=float, default=0.0, help="Stop after N minutes (0 = when the stream ends)")
    lv.add_argument("--prefix", default="LIVE", help="Output name prefix (<prefix>_NN.mp4)")
    lv.add_argument("--ratio", choices=["9:16", "1:1"], default=None, help="Target aspect ratio (default 9:16)")
    lv.add_argument(
        "--hook-mode", choices=list(OPT_CHOICES["hook_mode"]), default=None, help="Hook style (default tease-climax)"
    )
    lv.add_argument("--hook-text", default="", help="Fixed hook banner text (default: from each moment)")
    lv.add_argument(
        "--asr", choices=["auto", "full", "none"], default="auto", help="Speech recognition (auto = when installed)"
    )
    lv.add_argument("--whisper-model", default="auto", help="auto = large-v3-turbo on CUDA, base on the CPU")
    lv.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto", help="Whisper device")
    lv.add_argument("--language", default="auto", help="Whisper language code (en, hi) or auto")
    lv.add_argument(
        "--llm", choices=list(llm_providers), default="none", help="Optional LLM for titles (runs in the render worker)"
    )
    lv.add_argument("--llm-model", default="", help="LLM model (default qwen2.5:7b for ollama)")
    lv.add_argument("--game", default="", help="Manual game override for contextual hooks and tags")
    lv.add_argument(
        "--clip-word",
        action="append",
        default=None,
        help='Extra spoken command that makes a clip on the spot, e.g. "bhai clip karo" (repeatable; '
        '"clip that" is always on)',
    )
    lv.add_argument("--chat", choices=["auto", "off"], default="auto", help="Read live chat (Twitch, YouTube)")
    add_layout_flags(lv)
    add_draft_flags(lv)
    return lv
