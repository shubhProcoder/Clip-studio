"""Command-line parser and entry point."""

import argparse
import json
import sys

from streamclipper.captions.styles import (
    CAPTION_CASES,
    CAPTION_FONTS,
    CAPTION_POSITIONS,
    HIGHLIGHT_COLOURS,
    HOOK_STYLES,
)
from streamclipper.commands import (
    cmd_analyze,
    cmd_build,
    cmd_calibrate_hud,
    cmd_clip_stream,
    cmd_discover_clips,
    cmd_doctor,
    cmd_download_vod,
    cmd_publish,
    cmd_render,
    cmd_styles,
)
from streamclipper.discovery import ASR_MODES
from streamclipper.draft.audit import cmd_audit
from streamclipper.draft.importer import cmd_import_draft
from streamclipper.effects import ENHANCE_MIX, ZOOM_LEVELS
from streamclipper.hud.analyze import HUD_MODES
from streamclipper.live.command import add_live_parser, cmd_live
from streamclipper.llm import PROVIDERS
from streamclipper.options import OPT_CHOICES
from streamclipper.publish.youtube import PRIVACY
from streamclipper.quality import STATIC_BUCKET_S, STATIC_MIN_S, STATIC_THRESHOLD
from streamclipper.scoring import CONTENTS, DEFAULT_BAR
from streamclipper.util import ToolError


def _add_draft_flags(p, render: bool = True):
    p.add_argument(
        "--draft-root", default="", help="CapCut draft folder (default: auto-detected; env CAPCUT_DRAFT_ROOT)"
    )
    p.add_argument(
        "--overwrite", action="store_true", help="Replace a draft with the same name (default: add _2, _3, ...)"
    )
    p.add_argument(
        "--close-capcut", action="store_true", help="Force-close CapCut if it is open (unsaved CapCut edits are lost)"
    )
    p.add_argument("--no-draft", action="store_true", help="Do not write a CapCut draft")
    if render:
        p.add_argument(
            "--portable-drafts",
            action="store_true",
            help="Copy each draft's media into <draft>/media so the draft can be zipped and moved to another PC "
            "(then use import-draft there; automatic when CapCut is not installed)",
        )
        p.add_argument(
            "--render",
            action="store_true",
            help="Also export a ready-to-post MP4 (1080 wide, 30 fps, -11 LUFS) and run the export gate",
        )


def _add_asr_flags(p):
    p.add_argument(
        "--asr",
        choices=list(ASR_MODES),
        default="auto",
        help="Transcription: full stream in 10 min chunks (auto = full when faster-whisper is installed), "
        "only the candidate regions, or none",
    )
    p.add_argument(
        "--whisper-model",
        default="auto",
        help="faster-whisper model; auto = large-v3-turbo on a CUDA GPU, small on CPU (base for streams > 30 min)",
    )
    p.add_argument(
        "--device", choices=["auto", "cuda", "cpu"], default="auto", help="Whisper device (auto = CUDA if it works)"
    )
    p.add_argument("--fresh", action="store_true", help="Ignore saved progress (state.json) and start over")


def _add_hud_flags(p):
    p.add_argument(
        "--hud",
        choices=list(HUD_MODES),
        default="auto",
        help="Game HUD events (WASTED, BUSTED, mission passed, wanted stars) as a clip signal: auto = pick the "
        "game from the title or the screen (needs the vision extra), off, or a profile (gta6 is experimental)",
    )
    p.add_argument(
        "--hud-profile", default=None, help="Your own HUD profile JSON (e.g. from calibrate-hud); overrides --hud"
    )


def _add_static_flags(p):
    p.add_argument(
        "--static-analysis",
        choices=("on", "off"),
        default="on",
        help="Filter frozen ads, BRB screens and loading/static frames from VOD candidates",
    )
    p.add_argument("--static-bucket", type=float, default=STATIC_BUCKET_S, help="Static-analysis bucket in seconds")
    p.add_argument("--static-min", type=float, default=STATIC_MIN_S, help="Minimum consecutive static duration")
    p.add_argument("--static-threshold", type=float, default=STATIC_THRESHOLD, help="Mean luma difference threshold")


def _add_game_flags(p):
    p.add_argument(
        "--game",
        default="",
        help="Manual game override; aliases are accepted (GTA VI is auto-detected from GTA 6/GTA VI text)",
    )


def _add_select_flags(p, default_max: int, what: str):
    p.add_argument(
        "--max-clips",
        "--top",
        "--top-k",
        dest="max_clips",
        type=int,
        default=default_max,
        help=f"Most {what} to make, best first; a cap, not a target (0 = every clip that passes the quality bar; "
        f"default {default_max}; --top is the old name)",
    )
    p.add_argument(
        "--min-score",
        type=float,
        default=DEFAULT_BAR["min_score"],
        help=f"Score threshold of the quality bar (0-100, default {DEFAULT_BAR['min_score']:g}); "
        "0 lists every candidate without the bar",
    )
    p.add_argument(
        "--chat",
        default="",
        help='Chat replay file: yt-dlp live_chat.json, Twitch rechat JSON or JSON lines {"t": sec, "text": ...} '
        "(with --url the chat replay is fetched automatically when the site has one)",
    )
    p.add_argument(
        "--llm",
        choices=list(PROVIDERS),
        default="none",
        help="Optional LLM for story segments, titles and hashtags: ollama (local) or gemini (env GEMINI_API_KEY)",
    )
    p.add_argument("--llm-model", default="", help="LLM model (default qwen2.5:7b for ollama, gemini-3.5-flash)")
    add_content_flag(p)


def add_content_flag(p):
    p.add_argument(
        "--content",
        choices=list(CONTENTS),
        default=None,
        help="What the video is: gaming (loud reactions, default) or talk (podcasts, interviews: strong lines, "
        "laughter; beta). The podcast-9x16 preset sets talk",
    )


def _add_caption_flags(p):
    p.add_argument(
        "--caption-style",
        default=None,
        help="Caption style, or auto (default: picked per clip from its category; list and preview them with "
        "'streamclipper styles --preview')",
    )
    p.add_argument(
        "--caption-case", choices=list(CAPTION_CASES), default=None, help="Caption letter case (default: the style's)"
    )
    p.add_argument(
        "--words-per-caption",
        type=int,
        choices=[1, 2, 3, 4],
        default=None,
        help="Words shown at once (default: the style's)",
    )
    p.add_argument(
        "--caption-position",
        choices=list(CAPTION_POSITIONS),
        default=None,
        help="Where captions sit (default auto: below the facecam, never over the face)",
    )
    p.add_argument("--highlight-color", choices=list(HIGHLIGHT_COLOURS), default=None, help="Colour of the spoken word")
    p.add_argument(
        "--caption-font", choices=list(CAPTION_FONTS), default=None, help="Caption font (default: the style's)"
    )
    p.add_argument(
        "--emoji",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Add a matching emoji after keywords like fire, dead, no way (default: on for emoji-pop only)",
    )
    p.add_argument(
        "--profanity-filter",
        action="store_true",
        default=None,
        help="Mask common swear words in captions as f*** (default off)",
    )
    p.add_argument(
        "--hook-style", choices=list(HOOK_STYLES), default=None, help="Hook headline banner look (default bold-box)"
    )


def _add_effect_flags(p):
    p.add_argument(
        "--zoom",
        choices=list(ZOOM_LEVELS),
        default=None,
        help="Punch-in zoom on the payoff and hype words: light 1.08x, medium 1.12x, strong 1.2x; never crops a "
        "known face (default off)",
    )
    p.add_argument(
        "--remove-fillers",
        action="store_true",
        default=None,
        help="Cut um/uh and filler 'like,'/'you know' between words; the payoff and the first 2 s keep theirs "
        "(default off)",
    )
    p.add_argument(
        "--enhance-speech",
        choices=list(ENHANCE_MIX),
        default=None,
        help="Reduce background noise and some room echo in the voice (RNNoise); light keeps more game sound "
        "(default off)",
    )
    p.add_argument(
        "--brand-kit",
        default=None,
        help="Brand kit JSON: logo, @handle, caption font, colours, intro/outro card "
        "(see docs/examples/brand_kit.json)",
    )


def _add_layout_flags(p):
    p.add_argument(
        "--preset",
        default=None,
        help="Preset name (e.g. streamer-split-9x16, square-frame-9x16, hook-grabber-9x16)",
    )
    _add_caption_flags(p)
    p.add_argument("--cam-crop", default=None, help="Webcam box in source pixels x,y,w,h (overrides auto detection)")
    p.add_argument("--game-crop", default=None, help="Gameplay box in source pixels x,y,w,h")
    p.add_argument("--filler-video", default=None, help="Filler video for the hook-grabber bottom panel")


def _add_publish_source(p, rank_default=1):
    p.add_argument("--mp4", default="", help="Rendered clip MP4")
    p.add_argument("--clips", default="", help="clips.json (title, description and hashtags come from it)")
    p.add_argument(
        "--rank", type=int, default=rank_default, help="Clip rank in --clips" + (" (default 1)" if rank_default else "")
    )
    p.add_argument("--title", default="", help="Title (default: the clip's title)")
    p.add_argument("--description", default="", help="Description (default: the clip's description)")
    p.add_argument("--tags", default="", help="Hashtags, e.g. '#gaming #funny' (default: the clip's hashtags)")


def _add_publish_parser(sub):
    pu = sub.add_parser(
        "publish",
        help="Post clips with your own accounts: YouTube Shorts, Instagram Reels, a TikTok publish pack, a week plan",
        description="Upload with your own API credentials (see docs/GUIDE.md, Publishing). --dry-run prints what "
        "would be sent and sends nothing.",
    )
    ps = pu.add_subparsers(dest="platform_cmd", required=True)
    yt = ps.add_parser("youtube", help="Upload a clip as a YouTube Short (private by default)")
    _add_publish_source(yt)
    yt.add_argument("--privacy", choices=list(PRIVACY), default="private", help="private (default) or unlisted")
    yt.add_argument(
        "--publish-at",
        default="",
        help="Publish later, e.g. 2026-11-21T18:00 (your local time; the upload is private until then)",
    )
    yt.add_argument(
        "--youtube-credentials",
        default="",
        help="client_secret.json of your Google OAuth 'Desktop app' client "
        "(needed once; the sign-in token is kept in your user config folder)",
    )
    yt.add_argument("--dry-run", action="store_true", help="Print the request, send nothing")
    ig = ps.add_parser("instagram", help="Publish a clip as an Instagram Reel (professional account)")
    _add_publish_source(ig)
    ig.add_argument("--caption", default="", help="Caption (default: title, description and hashtags)")
    ig.add_argument(
        "--instagram-credentials", default="", help='JSON file {"ig_user_id", "access_token", "graph_version"}'
    )
    ig.add_argument("--dry-run", action="store_true", help="Print the request, send nothing")
    pk = ps.add_parser("pack", help="TikTok publish pack: MP4 + caption.txt + hashtags.txt + best_time.txt")
    _add_publish_source(pk, rank_default=None)
    pk.add_argument("--caption", default="", help="Caption (default: title, description and hashtags)")
    pk.add_argument("--out", default="", help="Folder for publish_pack/ (default: next to the MP4s)")
    sc = ps.add_parser("schedule", help="Plan a week of posts (YouTube publishes them itself) and upload them")
    sc.add_argument("--clips", required=True, help="clips.json; every rendered clip, best first")
    sc.add_argument("--platform", choices=["youtube", "pack", "instagram"], default="youtube", help="Where to post")
    sc.add_argument("--start", default="tomorrow", help="First day: YYYY-MM-DD, today or tomorrow (default)")
    sc.add_argument("--per-day", type=int, default=1, help="Clips per day (default 1)")
    sc.add_argument(
        "--times",
        default="",
        help="Local posting times, e.g. 18:00,20:30 (default: weekdays 18:00-21:00, weekends "
        "11:00-13:00, a common heuristic)",
    )
    sc.add_argument("--privacy", choices=list(PRIVACY), default="private", help=argparse.SUPPRESS)
    sc.add_argument("--youtube-credentials", default="", help="client_secret.json (needed once)")
    sc.add_argument(
        "--out", default="", help="Folder for publish_schedule.json and packs (default: next to clips.json)"
    )
    sc.add_argument("--dry-run", action="store_true", help="Only write and print the plan, send nothing")
    return pu


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="CapCut Desktop studio and stream clipper CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    dr = sub.add_parser("doctor", help="Check Python, ffmpeg, yt-dlp, Whisper, CapCut, fonts, effects and SFX")
    dr.add_argument("--json", action="store_true", help="Print the environment JSON instead of the table")

    an = sub.add_parser(
        "analyze", help="Analyze videos: speech islands, shock contrast, webcam box, optional Whisper words"
    )
    an.add_argument("--input", required=True, help="Video file or directory")
    an.add_argument("--out", required=True, help="Output directory for analysis artifacts")
    an.add_argument("--db", type=float, default=-48.0, help="RMS speech threshold in dB (default -48)")
    an.add_argument("--min-pause", type=float, default=0.20, help="Pause split threshold in seconds")
    an.add_argument("--transcribe", action="store_true", help="Run Whisper word timestamps if installed")
    an.add_argument("--whisper-model", default="small", help="Whisper model name")
    an.add_argument("--language", default="auto", help="Whisper language code (en, hi) or auto")
    an.add_argument("--exposure", action="store_true", help="Compute brightness, overexposure & motion stats")
    an.add_argument("--exp-step", type=float, default=0.5, help="Exposure sample interval (seconds)")

    dc = sub.add_parser("discover-clips", help="Find the best clip moments in a stream and write clips.json")
    dc.add_argument("--url", default="", help="Stream/VOD URL (YouTube uses the Most Replayed heatmap)")
    dc.add_argument("--video", "--input", dest="video", default="", help="Local stream video")
    dc.add_argument("--out", required=True, help="Output clips.json (or a folder)")
    _add_select_flags(dc, 0, "clips")
    dc.add_argument("--min-dur", type=float, default=15.0, help="Shortest clip in seconds")
    dc.add_argument(
        "--max-dur", type=float, default=60.0, help="Longest clip in seconds (rendered clips stay <= 59.5 s)"
    )
    dc.add_argument("--target-dur", type=float, default=30.0, help="Preferred clip length in seconds")
    tg = dc.add_mutually_exclusive_group()
    tg.add_argument(
        "--transcribe", dest="transcribe", action="store_true", default=None, help="Force Whisper word timestamps"
    )
    tg.add_argument("--no-transcribe", dest="transcribe", action="store_false", help="Skip Whisper (= --asr none)")
    _add_asr_flags(dc)
    dc.add_argument("--language", default="auto", help="Whisper language code (en, hi) or auto")
    dc.add_argument("--db", type=float, default=-48.0, help="RMS speech threshold in dB")
    dc.add_argument("--min-pause", type=float, default=0.55, help="Pauses shorter than this stay inside a sentence")
    _add_hud_flags(dc)
    _add_static_flags(dc)
    _add_game_flags(dc)

    dv = sub.add_parser("download-vod", help="Download only a slice of a VOD as H.264/AAC MP4 (<= 1080p)")
    dv.add_argument("--url", required=True, help="Video URL (YouTube, Twitch, Kick, ...)")
    dv.add_argument("--start", required=True, help="Start time: seconds or [HH:]MM:SS")
    dv.add_argument("--end", required=True, help="End time: seconds or [HH:]MM:SS")
    dv.add_argument("--out", required=True, help="Output .mp4 file (or folder)")
    dv.add_argument("--max-height", type=int, default=1080, help="Maximum video height (never upscaled)")

    cs = sub.add_parser("clip-stream", help="Turn a 16:9 stream into vertical/square clips as CapCut drafts")
    cs.add_argument("--input", default="", help="Source 16:9 stream/gameplay video")
    cs.add_argument("--url", default="", help="YouTube URL: use the most-replayed slice")
    cs.add_argument("--out", required=True, help="Working directory for analysis, panels and specs")
    cs.add_argument("--prefix", default="clip", help="Name prefix of the drafts and MP4s (clip_01, clip_02, ...)")
    cs.add_argument("--ratio", choices=["9:16", "1:1"], default=None, help="Target aspect ratio (default 9:16)")
    cs.add_argument(
        "--hook-mode", choices=list(OPT_CHOICES["hook_mode"]), default=None, help="Hook style (default tease-climax)"
    )
    cs.add_argument(
        "--caption-mode",
        choices=list(OPT_CHOICES["caption_mode"]),
        default=None,
        help="Old name of --caption-style (hormozi-pop = bold-pop, karaoke-word = karaoke-highlight)",
    )
    cs.add_argument("--font", choices=list(OPT_CHOICES["font"]), default=None, help="Old name of --caption-font")
    cs.add_argument("--hook-text", default="", help="Hook banner text; \\n starts a new line (default: auto)")
    _add_select_flags(cs, 3, "drafts (all clips go to clips.json)")
    cs.add_argument("--target-dur", type=float, default=30.0, help="Target clip duration in seconds")
    cs.add_argument("--min-dur", type=float, default=15.0, help="Shortest clip in seconds")
    cs.add_argument(
        "--max-dur", type=float, default=60.0, help="Longest clip in seconds (rendered clips stay <= 59.5 s)"
    )
    cs.add_argument("--db", type=float, default=-48.0, help="RMS gate in dB")
    cs.add_argument("--min-pause", type=float, default=0.55, help="Pauses shorter than this stay inside a clip")
    cs.add_argument(
        "--transcribe",
        action="store_true",
        help="Require Whisper on the whole stream (= --asr full)",
    )
    cs.add_argument("--no-transcribe", action="store_true", help="Never run Whisper (= --asr none)")
    _add_asr_flags(cs)
    cs.add_argument("--language", default="auto", help="Whisper language code (en, hi) or auto")
    cs.add_argument(
        "--hindi-script", choices=["devanagari", "roman"], default="devanagari", help="How Hindi captions are written"
    )
    _add_hud_flags(cs)
    _add_static_flags(cs)
    _add_game_flags(cs)
    _add_layout_flags(cs)
    _add_effect_flags(cs)
    _add_draft_flags(cs)

    for name, hlp in (
        ("build", "Build a CapCut draft from a spec (low-level timeline or high-level clip spec)"),
        ("build-clipper", "Same as build (README name)"),
    ):
        bd = sub.add_parser(name, help=hlp)
        bd.add_argument("--spec", required=True, help="Path to the spec JSON")
        bd.add_argument("--out", default="", help="Work folder for panels/specs (default: next to the spec)")
        _add_layout_flags(bd)
        _add_effect_flags(bd)
        _add_draft_flags(bd)

    au = sub.add_parser("audit", help="Check a built CapCut draft (files, materials, UTF-16 ranges, registration)")
    au.add_argument("--draft", default="", help="Draft folder name")
    au.add_argument("--project", default="", help="Same as --draft (old name)")
    au.add_argument("--draft-root", default="", help="Optional custom draft root")
    au.add_argument("--mp4", "--video", dest="mp4", default="", help="Run the export gate on a rendered MP4")
    au.add_argument("--timeline", default="", help="Timeline sidecar for --mp4 (default: <mp4 name>.timeline.json)")

    rn = sub.add_parser(
        "render", help="Export ready-to-post MP4s (no CapCut needed) from a clip plan, a clip spec or clips.json"
    )
    rn.add_argument("--spec", default="", help="<name>.clip.json (clip plan) or a README-style clip spec")
    rn.add_argument("--clips", default="", help="clips.json from discover-clips")
    rn.add_argument("--video", "--input", dest="video", default="", help="Source stream for --clips")
    rn.add_argument(
        "--max-clips",
        "--top",
        "--top-k",
        dest="max_clips",
        type=int,
        default=0,
        help="Render only the N best clips of --clips (default 0 = all; --top is the old name)",
    )
    rn.add_argument("--out", default="", help="Output folder (default: next to the spec / clips.json)")
    rn.add_argument("--prefix", default="clip", help="Output name prefix for --clips (<prefix>_NN.mp4)")
    rn.add_argument("--ratio", choices=["9:16", "1:1"], default=None, help="Target aspect ratio (default 9:16)")
    rn.add_argument(
        "--hook-mode", choices=list(OPT_CHOICES["hook_mode"]), default=None, help="Hook style (default tease-climax)"
    )
    rn.add_argument(
        "--caption-mode", choices=list(OPT_CHOICES["caption_mode"]), default=None, help="Old name of --caption-style"
    )
    rn.add_argument("--font", choices=list(OPT_CHOICES["font"]), default=None, help="Old name of --caption-font")
    rn.add_argument("--hook-text", default="", help="Hook text; \\n starts a new line (default: from clips.json)")
    rn.add_argument(
        "--hindi-script", choices=["devanagari", "roman"], default="devanagari", help="How Hindi captions are written"
    )
    _add_layout_flags(rn)
    _add_effect_flags(rn)
    _add_draft_flags(rn, render=False)

    sy = sub.add_parser("styles", help="List the caption styles, hook banner styles and caption fonts")
    sy.add_argument(
        "--preview",
        action="store_true",
        help="Render a ~4 s sample MP4 + PNG of every caption style and hook style, and styles_gallery.jpg",
    )
    sy.add_argument("--input", default="", help="Video for the samples (default: a synthetic background)")
    sy.add_argument("--out", default="style_previews", help="Folder for the samples [style_previews]")

    lv = add_live_parser(sub, _add_layout_flags, _add_draft_flags, PROVIDERS, DEFAULT_BAR["min_score"])
    add_content_flag(lv)

    im = sub.add_parser(
        "import-draft", help="Import a zip of CapCut drafts (e.g. capcut_drafts.zip from Colab) into CapCut Desktop"
    )
    im.add_argument("--zip", required=True, help="The drafts zip (capcut_drafts.zip)")
    im.add_argument(
        "--draft-root", default="", help="CapCut draft folder (default: auto-detected; env CAPCUT_DRAFT_ROOT)"
    )
    im.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace drafts with the same name (the old folder is moved next to the draft root, not deleted; "
        "default: add _2, _3, ...)",
    )
    im.add_argument(
        "--close-capcut", action="store_true", help="Force-close CapCut if it is open (unsaved CapCut edits are lost)"
    )

    ch = sub.add_parser(
        "calibrate-hud",
        help="Find a game's HUD text regions and words (e.g. GTA 6 after its release) and write a HUD profile JSON",
    )
    ch.add_argument("--video", required=True, help="Gameplay video with the HUD events you want (WASTED screens ...)")
    ch.add_argument("--profile", default="gta6", help="Profile to start from (gta5, gta6, generic or a JSON file)")
    ch.add_argument("--times", default="", help="Times to read, e.g. 12:34,15:10 (default: every --every seconds)")
    ch.add_argument("--every", type=float, default=30.0, help="Seconds between sampled frames (default 30)")
    ch.add_argument("--out", default="", help="Profile JSON to write (default <video>_hud.json)")
    ch.add_argument("--check", default="", help="Print the detections of this profile JSON on the video instead")

    _add_publish_parser(sub)

    db = sub.add_parser("dashboard", help="Open the local web dashboard: start jobs, watch progress, manage clips")
    db.add_argument("--port", type=int, default=8765, help="Port (default 8765)")
    db.add_argument("--host", default="127.0.0.1", help="Listen address (default 127.0.0.1: this PC only)")
    db.add_argument("--workspace", default="", help="Folder for jobs, uploads and clips (default ~/StreamClipper)")
    db.add_argument("--no-browser", action="store_true", help="Do not open the browser")
    db.add_argument(
        "--lan",
        action="store_true",
        help="Also allow other devices on your network (needs the printed link with its access token)",
    )
    db.add_argument("--draft-root", default="", help="CapCut draft folder for Open in CapCut (default: auto-detected)")
    return ap


def cmd_dashboard(args):
    from streamclipper.dashboard.server import run_dashboard  # late import: only the dashboard needs it

    run_dashboard(args)


COMMANDS = {
    "doctor": cmd_doctor,
    "analyze": cmd_analyze,
    "discover-clips": cmd_discover_clips,
    "download-vod": cmd_download_vod,
    "clip-stream": cmd_clip_stream,
    "build": cmd_build,
    "build-clipper": cmd_build,
    "audit": cmd_audit,
    "render": cmd_render,
    "styles": cmd_styles,
    "import-draft": cmd_import_draft,
    "live": cmd_live,
    "calibrate-hud": cmd_calibrate_hud,
    "publish": cmd_publish,
    "dashboard": cmd_dashboard,
}


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args()
    try:
        COMMANDS[args.cmd](args)
    except ToolError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        if e.hint:
            print(f"Fix: {e.hint}", file=sys.stderr)
        sys.exit(2)
    except FileNotFoundError as e:
        print(f"ERROR: file not found: {e.filename or e}", file=sys.stderr)
        print("Fix: check the path; put quotes around paths with spaces", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON ({e})", file=sys.stderr)
        print("Fix: check the spec/JSON file for missing commas or quotes", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        sys.exit(130)
