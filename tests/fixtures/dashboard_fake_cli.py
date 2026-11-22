"""
Fake `python -m streamclipper` for the dashboard tests: prints the real log lines, writes small fake outputs.
No ffmpeg, GPU or network. SC_FAKE_SLEEP = seconds per stage (default 0.05).
"""

import json
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from streamclipper.draft.rootmeta import OUTPUT_SUFFIXES, RENDER_SUFFIXES, unique_output_name  # noqa: E402

SLEEP = float(__import__("os").environ.get("SC_FAKE_SLEEP", "0.05"))
STOP = []


def _stop(*_a):
    STOP.append(True)


def arg(argv, flag, default=""):
    return argv[argv.index(flag) + 1] if flag in argv else default


def nap(k=1.0):
    time.sleep(SLEEP * k)
    sys.stdout.flush()


GATE_ROWS = [["resolution", "PASS", "1080x1920"], ["loudness", "PASS", "I=-11.0 LUFS"], ["blackdetect", "PASS", "none"]]


def write_clip(out: Path, name: str, rank: int, draft_root: str = ""):
    win = out / "windows" / f"win_{rank}.mp4"
    win.parent.mkdir(parents=True, exist_ok=True)
    win.write_bytes(b"\x00" * 2048)
    (out / f"{name}.mp4").write_bytes(bytes(range(256)) * 16)
    (out / f"{name}_thumb.jpg").write_bytes(b"\xff\xd8fakejpg")
    (out / f"{name}.gate.json").write_text(json.dumps({"pass": True, "rows": GATE_ROWS}), encoding="utf-8")
    words = [
        {"w": w, "s": 1.0 + i * 0.5, "e": 1.4 + i * 0.5} for i, w in enumerate(["no", "way", "bro", "thats", "insane"])
    ]
    plan = {
        "type": "clip_plan",
        "name": name,
        "draft": name,
        "source": "https://example.com/vod",
        "source_window": {
            "path": str(win),
            "start": 100.0,
            "end": 130.0,
            "offset": 0.0,
            "width": 1920,
            "height": 1080,
            "fps": 30,
        },
        "ratio": "9:16",
        "layout": "split",
        "boxes_px": {"cam": [0, 0, 412, 260], "game": None, "person": None},
        "segments": [{"src_s": 1.0, "src_e": 25.0, "tl_start": 0.0, "kind": "story"}],
        "words": words,
        "hook_text": "NO WAY",
        "caption_style": "bold-pop",
        "hook_style": "bold-box",
        "caption_opts": {"position": "auto", "highlight": ""},
    }
    (out / f"{name}.clip.json").write_text(json.dumps(plan), encoding="utf-8")
    if draft_root:
        d = Path(draft_root) / name
        (d / "media").mkdir(parents=True, exist_ok=True)
        (d / "media" / "gameplay.mp4").write_bytes(b"v")
        (d / "draft_content.json").write_text(
            json.dumps({"id": f"ID-{name}", "name": name, "duration": 1, "materials": {}}), encoding="utf-8"
        )
        (d / "draft_meta_info.json").write_text(
            json.dumps({"draft_id": f"ID-{name}", "draft_name": name}), encoding="utf-8"
        )


def clip_doc(n: int, live: bool = False):
    clips = []
    for r in range(1, n + 1):
        c = {
            "rank": r,
            "start": 100.0 * r,
            "end": 100.0 * r + 24,
            "duration": 24.0,
            "score": 80 - r,
            "breakdown": {"loudness": 20, "speech": 15},
            "category": "funny",
            "caption_style": "bold-pop",
            "hook_headline": f"HOOK {r}",
            "title": f"Title <b>{r}</b>",
            "description": f"Description {r}",
            "hashtags": ["#gaming", "#clips"],
            "reason": "loud reaction",
            "mp4": f"{'LIVE' if live else 'clip'}_{r:02d}.mp4",
            "thumb": f"{'LIVE' if live else 'clip'}_{r:02d}_thumb.jpg",
        }
        if live:
            c["latency_s"] = 12.5 + r
        clips.append(c)
    return {"clips": clips, "source": "x", "duration": 600}


def clip_stream(argv):
    out = Path(arg(argv, "--out"))
    out.mkdir(parents=True, exist_ok=True)
    print(f"Source: {arg(argv, '--url') or arg(argv, '--input')}")
    nap()
    print("Analysing audio of x.mp4 (10:00) ...")
    nap()
    print("Transcribing 10:00 of audio in 2 chunk(s) (2 to do) ...")
    for k in (1, 2):
        nap()
        print(f"  ASR chunk {k}/2 00:00-05:00: 100 words, 1.0s")
    nap()
    print("RANK START-END          SCORE  CATEGORY   HOOK                          REASON")
    print("1    01:40-02:04       79     funny      HOOK 1                        loud")
    print("2    03:20-03:44       78     funny      HOOK 2                        loud")
    print("Analysis time: 1.0s (5 candidates, 2 clips passed the quality bar)")
    droot = arg(argv, "--draft-root") if "--portable-drafts" in argv else ""
    doc = clip_doc(2)
    for r in (1, 2):
        nap()
        print(f"Rendering {out}/clip_{r:02d}.mp4 (24.00s, 1 pieces, split 9:16) ...")
        write_clip(out, f"clip_{r:02d}", r, droot)
        print(f"EXPORT GATE: clip_{r:02d}.mp4")
    (out / "clips.json").write_text(json.dumps(doc), encoding="utf-8")
    (out / "clips.csv").write_text("rank,title\n1,x\n", encoding="utf-8")
    print("RENDER SUMMARY")
    print("  PASS  clip_01")


def live(argv):
    out = Path(arg(argv, "--out"))
    out.mkdir(parents=True, exist_ok=True)
    for s in ("SIGINT", "SIGBREAK"):
        if hasattr(signal, s):
            signal.signal(getattr(signal, s), _stop)
    print("Live: file source x | buffer 15 min (Ctrl+C stops cleanly)")
    n = 0
    t0 = time.time()
    while not STOP and time.time() - t0 < 30:
        nap(2)
        n += 1
        name = f"LIVE_{n:02d}"
        write_clip(out, name, n)
        (out / "clips.json").write_text(json.dumps(clip_doc(n, live=True)), encoding="utf-8")
        print(f"Live: buffered 0{n}:00 | 10 words in memory | {n} clips ready, 0 queued")
        print(f"CLIP {n:02d} | 00:30 into stream | score 70 | 'x' | {out}/{name}.mp4 | ready 13 s after the moment")
    (out / "live_summary.json").write_text(json.dumps({"clips": n}), encoding="utf-8")
    print("LIVE SUMMARY")


def render(argv):
    sp = Path(arg(argv, "--spec"))
    spec = json.loads(sp.read_text(encoding="utf-8"))
    out = Path(arg(argv, "--out")) if "--out" in argv else sp.parent
    if spec.get("type") == "clip_plan":
        name = unique_output_name(out, spec["name"], suffixes=RENDER_SUFFIXES)
    else:
        name = unique_output_name(out, spec["draft_name"], suffixes=OUTPUT_SUFFIXES)
        (out / f"{name}.clip.json").write_text(
            json.dumps({"type": "clip_plan", "name": name, "spec": spec}), encoding="utf-8"
        )
    nap()
    print(f"Rendering {out}/{name}.mp4 (20.00s, 1 pieces, split 9:16) ...")
    (out / f"{name}.mp4").write_bytes(b"fake mp4 " + json.dumps(argv).encode())
    (out / f"{name}.gate.json").write_text(json.dumps({"pass": True, "rows": GATE_ROWS}), encoding="utf-8")
    print(f"EXPORT GATE: {name}.mp4")


def build(argv):
    sp = Path(arg(argv, "--spec"))
    spec = json.loads(sp.read_text(encoding="utf-8"))
    out = Path(arg(argv, "--out"))
    name = unique_output_name(out, spec["draft_name"])
    (out / f"{name}_timeline.json").write_text("{}", encoding="utf-8")
    (out / f"{name}.clip.json").write_text(json.dumps({"type": "clip_plan", "name": name}), encoding="utf-8")
    print(f"Timeline spec -> {out.as_posix()}/{name}_timeline.json (layout=split, preset=-, volume x1.0)")


def main():
    argv = sys.argv[1:]
    cmd = argv[0]
    {"clip-stream": clip_stream, "live": live, "render": render, "build": build}[cmd](argv)


if __name__ == "__main__":
    main()
