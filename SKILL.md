---
name: capcut-universal-editor
description: StreamClipper, a stream clipper (Python package `streamclipper`). Turns long 16:9 stream VODs (local file, Drive path or Twitch/YouTube/Kick URL) into ranked 9:16 or 1:1 clips with a 0-100 score breakdown and category, ready-to-post MP4s (1080 wide, 30 fps, H.264, -11 LUFS) with 15 caption styles and 3 hook styles, thumbnails, clips.csv/index.html, and editable CapCut Desktop drafts (portable zip + import-draft). English-first; Hindi (Devanagari) supported. Commands - doctor, discover-clips, clip-stream, render, build, build-clipper, import-draft, styles, download-vod, audit, analyze.
---
# StreamClipper
Install from the repo folder: `python -m pip install -e ".[asr]"` (add `gpu` on NVIDIA machines). The command is `streamclipper` (or `python -m streamclipper`; the old `python scripts/capcut_studio.py` runs the same CLI). Full reference: `README.md`.

## Rules for agents
- Run `streamclipper doctor` first on a new machine; fix every MISSING row before anything else.
- CapCut Desktop must be closed before writing drafts into its real draft folder (`CapCut is open; close it first`). Ask the user to close it. Use `--close-capcut` only when the user agrees (unsaved CapCut edits are lost). For tests use `--draft-root <test folder>`. `render` and `--no-draft --render` need no CapCut.
- Names are never overwritten (`_2`, `_3` ... for drafts, MP4s and plans) unless `--overwrite` is passed. `import-draft --overwrite` moves the old draft to `streamclipper_replaced_drafts\` next to the draft root.
- Exit codes: 0 ok, 1 export gate or audit FAIL, 2 `ERROR:` with a `Fix:` line.
- Never print, log or commit cookies files or `GEMINI_API_KEY`.
- Only clip streams the user has permission to clip.

## Typical flow
```bash
streamclipper doctor
streamclipper discover-clips --video stream.mp4 --out clips.json
streamclipper render --clips clips.json --video stream.mp4 --max-clips 3 --out renders
streamclipper audit --mp4 renders/clip_01.mp4
streamclipper clip-stream --input stream.mp4 --out work --prefix MyClip --max-clips 3 --render
streamclipper audit --draft MyClip_01
streamclipper import-draft --zip capcut_drafts.zip
streamclipper styles
```
`--url URL` downloads the audio only for analysis (resumable), fetches the chat replay when the site has one, and on YouTube blends in the Most Replayed heatmap; clip-stream then downloads only the picked windows.

## Commands and main flags
| Command | Main flags (defaults) |
|---|---|
| `doctor` | `--json` |
| `discover-clips` | `--video` or `--url`, `--out` (required), `--max-clips 0` (= all; old `--top`), `--min-score 50`, `--chat FILE`, `--llm none\|ollama\|gemini`, `--llm-model`, `--asr auto`, `--whisper-model auto`, `--device auto`, `--fresh`, `--language auto`, `--min-dur 15`, `--max-dur 45`, `--target-dur 24` |
| `clip-stream` | `--input` or `--url`, `--out` (required), `--prefix clip`, `--max-clips 3` (drafts; all clips go to clips.json), the discovery flags, layout/caption flags, `--draft-root`, `--overwrite`, `--close-capcut`, `--no-draft`, `--portable-drafts`, `--render` |
| `render` | `--clips clips.json --video SRC` or `--spec <clip.json or clip spec>`, `--max-clips 0`, `--out`, `--prefix clip`, layout/caption flags; never writes a draft |
| `build` / `build-clipper` | `--spec` (required), `--out`, layout/caption flags, draft flags, `--portable-drafts`, `--render` |
| `import-draft` | `--zip` (required), `--draft-root`, `--overwrite`, `--close-capcut` |
| `styles` | lists caption styles, hook styles, fonts |
| `download-vod` | `--url --start --end --out` (required), `--max-height 1080` |
| `audit` | `--draft NAME`, `--draft-root`, `--mp4 FILE`, `--timeline` |

Layout/caption flags: `--preset`, `--ratio 9:16|1:1`, `--hook-mode`, `--hook-text`, `--caption-style` (default bold-pop), `--caption-case upper|title|as-is`, `--words-per-caption 1-4`, `--caption-position auto|top|center|lower`, `--highlight-color`, `--caption-font`, `--emoji/--no-emoji`, `--profanity-filter`, `--hook-style bold-box|outline-glow|top-bar`, `--cam-crop x,y,w,h`, `--game-crop x,y,w,h`, `--filler-video`, `--hindi-script devanagari|roman`.

- Presets (`src/streamclipper/data/presets/*.json`): `streamer-split-9x16` (alias `gta6-streamer-9x16`), `streamer-split-1x1`, `square-frame-9x16`, `caption-punch-9x16` (alias `hormozi-caption-punch`), `hook-grabber-9x16` (needs `--filler-video`). Precedence: CLI flag > spec key > preset > default.
- Caption styles: bold-pop, beast-bold, karaoke-highlight, word-by-word, two-line-stack, highlight-box, neon-glow, impact-shake, emoji-pop, typewriter, bounce, gradient-stroke, comic-burst, minimal-clean, gaming-glitch (old names hormozi-pop, karaoke-word, glow-neon, shake-impact, bounce-word still work). Max 2 lines; `auto` position never covers the facecam.
- Categories: funny, rage, clutch, reaction, fail, hot take, reveal, wholesome, chaos.

## How clips are picked
Every voice peak (6+ dB above the rolling 60 s median), chat spike and LLM payoff is a candidate. The window starts at a sentence end or silence 10-25 s before the peak and ends 2-5 s after it, never inside a word; pauses over 0.55 s are trimmed (not around the peak). Score 0-100 = weighted mean of hook 20, story 25, emotion 25, pacing 10, engagement 20 over the available components; non-overlapping clips best first, `--min-score` and `--max-clips` apply. Read `reason`, `category` and `hook_headline` in `clips.json` (or `index.html`) before building. Progress of long runs is in `<work>/state.json`; a re-run resumes, `--fresh` starts over.

## Colab
`notebooks/StreamClipper_Colab.ipynb` only sets form values and calls `streamclipper.colab.run_pipeline(source, out_dir, preset, caption_style, hook_style, min_score, max_clips, ratio, llm, render, drafts, draft_root, cookies, resume)`, which runs clip-stream with portable drafts and writes `<out_dir>/capcut_drafts.zip`. Rebuild the notebook with `python tools/make_notebook.py`. No Gradio, Streamlit, tunnels or share links.

## CapCut draft facts (keep when editing the writer)
- Text shadow `diffuse` must stay small (0.025; audit fails above 0.15).
- Text glow is a `materials.effects` entry with `panel_id: "text_glow"` (TextBloom `7194814295517958657` neon, `7367637998579487248` deep, `7194814374001775105` rays), referenced from the text segment's `extra_material_refs`; text `check_flag` is 71 with glow, 15 without.
- `styles[].range` counts UTF-16 code units (`utf16_len`), not Python `len()`.
- `draft.extra` is the fixed 26 bytes CapCut writes; draft fps 30; canvas `ratio: "original"` with explicit width/height.
- Portable drafts: media copied to `<draft>/media`, CapCut paths as `__CAPCUT_LOCAL__`, bundled font paths as `__STREAMCLIPPER_FONTS__`, map in `streamclipper_portable.json`. `import-draft` refuses zip entries with absolute paths or `..`.
- `root_meta_info.json` is backed up before the first write of a run (`.streamclipper_first.bak` once, plus `.streamclipper_<YYYYmmdd-HHMMSS>.bak`, newest 5 kept), then written atomically.
- Tested with CapCut Desktop 9.7.1.4203 on Windows 11.
