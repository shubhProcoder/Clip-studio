# Changelog
## 5.3.0 (2026-11-19)
### Added
- Game HUD detection: `--hud auto|off|gta5|gta6|generic` and `--hud-profile FILE` on `discover-clips` and `clip-stream`. The GTA V profile finds WASTED, BUSTED, MISSION PASSED, MISSION FAILED and 3+ wanted stars (test videos: precision 1.00, recall 0.71); `gta6` is experimental; `generic` looks for big text, flashes and kill-feed bursts. Events add a `hud` score component and reasons such as "WASTED on screen". OCR uses a PaddleOCR ONNX model downloaded once (SHA-256 checked).
- `streamclipper calibrate-hud` writes a HUD profile and a contact sheet from a game video (for GTA 6 after release).
- MediaPipe face tracking (BlazeFace full range, bundled) with tiling for small facecams, smoothing with a dead zone and active-speaker choice for 2+ faces; OpenCV Haar stays as the fallback (`vision-haar` extra). `doctor` shows the face detector and warns about two OpenCV packages.
- Editing effects, all off by default: `--zoom light|medium|strong` (punch-in on the payoff and hype words, faces kept in frame), `--remove-fillers`, `--enhance-speech light|strong` (ffmpeg `arnndn` with a bundled RNNoise model), `--brand-kit FILE` (logo, handle, font, colours, intro/outro card). In the dashboard (Home Advanced drawer, editor Effects card, Settings Brand kit) and the Colab notebook.
- Publishing with your own credentials: `streamclipper publish youtube|instagram|pack|schedule` (YouTube Shorts private by default, scheduled uploads, Instagram Reels, TikTok publish pack, a 7-day schedule, `--dry-run`); dashboard Publish dialog, Schedule week and Settings > Publishing. Tested only against a mocked API.
- Export gate row `caption_coverage`: share of spoken words on screen and the longest uncaptioned stretch of speech (FAIL below 97% or above 0.6 s).
- Selection check on 8 unseen public streams with a blind, pre-registered tune/holdout split; `tools/eval_selection.py --split tune|holdout|all`, `--bar`, `--weights`, `--before`. The numbers are in the guide (Selection quality); no threshold changed.
- Colab notebook: installs the `vision` extra; cell 6 has `hud`, `zoom`, `remove_fillers` and `enhance_speech`.
### Fixed
- Captions: very short and zero-length words were dropped or joined badly; shouted speech that Whisper's voice filter skipped is recovered with a second pass per clip window; captions no longer count small background faces as faces to avoid.
- Speech enhancement output is delayed back into sync (ffmpeg `arnndn` adds 10 ms of latency).
- Colab (found on a real T4 run): Whisper fell back to the CPU because the image has no CUDA 12 cuBLAS; the notebook now installs the `gpu` extra on a GPU runtime and StreamClipper preloads its CUDA 12 libraries on Linux (T4: 58.8x realtime). Running cell 7 before cells 5-6 prints which cell to run instead of a NameError. Cell 2 prints the pip and doctor output.
- Resume: re-running `clip-stream` (or Colab cell 7) with the same inputs keeps the clips that are already made instead of making `clip_01_2` copies.
- Captions: several words that Whisper puts at the same instant now share one caption instead of getting a 0 s caption (found by the Linux check).
- Loudness: the AAC overshoot check no longer lowers the limiter ceiling below -6 dB; a hard-limited live clip stayed at -14.4 LUFS before.
- `doctor` says when MediaPipe is installed but cannot start (missing libEGL on headless Linux) and gives the apt fix.
## 5.2.1 (2026-11-18)
### Fixed
- Editor preview: in the first 2 s the hook banner and the caption no longer draw on top of each other; the preview places the hook and moves the caption below it with the same rules as the render.
- Editor toolbar: grouped controls, icon-only buttons with tooltips under 1280 px and a More menu (Merge, Delete keyframe, Reset crop), so it fits on two rows at 1024 px. Home: a shorter link placeholder on phones.
- Auto-reframe: two quick clicks (or two tabs) start one face-tracking run; the second request gets "already running" (HTTP 409).
- Auto-reframe with a facecam overlay in a corner: segments where the face is in the cam box are set to Split (facecam on top, gameplay below) instead of a full-height pan with the face low; a full-layout crop puts the face in the upper third where the source allows.
- Jobs API: `render`, `drafts` and the delete `confirm` flag are parsed strictly (true/false, 1/0, yes/no); `"render": "false"` no longer counts as true, and other values give HTTP 400.
## 5.2.0 (2026-11-18)
### Added
- Premium dashboard: left sidebar (bottom bar on phones), Home with one-click **Get clips** and an Advanced drawer, a first-run set-up check (doctor in the background), Projects grid with thumbnails, source icons, progress rings and clip counts, vertical clip cards with hover preview and score breakdown, a Styles page with Set as default, skeleton loaders, toasts, confirm dialogs, keyboard shortcuts (`/`, `?`) and Lucide icons (ISC).
- Reframe editor: Split at playhead into segments with their own layout (full, split, square frame, caption punch, speaker) and crop windows, pan keyframes with smooth or linear easing, cut/smooth joins, a live canvas preview drawn with the render maths, a timeline with thumbnails, waveform, segment blocks, keyframe diamonds and word chips, undo/redo (Ctrl+Z / Ctrl+Shift+Z), caption drag with safe-zone snapping and a face warning, style tiles, and editor shortcuts (Space, J/K/L, S, [, ], Delete).
- Auto-reframe (face tracking in the background, keyframes about once a second) and Reset to auto-detected.
- Versions with Compare: the original and an edited MP4 side by side, played and seeked together, with both audit tables.
- Timeline thumbnail sprite (`GET .../clips/<key>/thumbs`, one ffmpeg call, cached).
- Segmented edits render as one ffmpeg graph (frame-exact trim per segment + concat, continuous source audio) through the normal export gate; CapCut drafts get one video segment per editor segment (`reframe_segments` and `caption_anchor` in the clip spec).
### Changed
- Dashboard routes: `#/home`, `#/projects`, `#/project/<id>`, `#/edit/<job>/<key>`, `#/live`, `#/styles`, `#/settings`; the old `#/new`, `#/jobs` and `#/job/<id>` links redirect.
- Default `--prefix` is now `clip` (was GTA6_Viral): `clip-stream` without `--prefix` writes `clip_01.mp4`, the same names as `render` and the dashboard (B07).
### Fixed
- B01: in `--lan` mode the editor serves only videos inside the workspace.
- B02: a bad share `days` value no longer gives a 500 (it is parsed once; default 7 days).
- B03: the README screenshot shows the dashboard UI (Projects page) instead of only video frames.
- B04: Settings and Options no longer wait on the CapCut/Ollama checks (background, cached).
- B05: a slow page can no longer replace the page you moved to.
- B06: the editor caches its source probe, so reopening a clip runs no ffprobe.
## 5.1.0 (2026-11-17)

### Added

- `streamclipper dashboard`: a local web dashboard (standard library only, no new dependency): new jobs from a link, an upload or a file, a job queue with live progress, cancel and resume, a clips library (player, scores, copy buttons, audit table, re-render, Open in CapCut, posted/rejected, delete to trash), a live page and settings. Light and dark themes.
- Dashboard clip editor: trim with word-snapped handles, facecam/gameplay crop boxes with a 9:16 preview, caption word fixes, styles, hook text and post text; it renders through `render --spec` and the export gate.
- `--lan`: the dashboard on your network with an access token, and view-only share links (expiry, revoke).
- Colab: `use_google_drive` (work without Drive, zip download at the end), opt-in `share_on_drive` Drive links, and result cards with copy buttons.
- `<name>.gate.json` next to every rendered MP4 (the export gate table); `drive_link` column in clips.csv.

### Changed

- Licence changed from MIT to AGPL-3.0-or-later.
- README shortened; the full documentation moved to docs/GUIDE.md.

## 5.0.1 (2026-11-17)
### Fixed
- Full layout (full-cam, and the corner-cam fallback when the game screen is black): captions and the hook go to the zone of the frame that has no face (face detection on the panel), instead of a fixed low zone that could cover the chin and mouth. New export-gate row `captions_off_face` (every layout): FAIL when a caption covers a detected face on 2+ sampled frames.
- `live`: render and probe subprocesses start in their own process group, so Ctrl+C (or Ctrl+Break) stops the ingest and the clip being rendered and the queued ones still finish. Window files (`windows/live_*.mp4` and the copy cut from each) are deleted once their clip is made; failed or skipped clips keep theirs. New `--max-disk-gb` (default 20) warns when the output folder grows past it.
- Colab live cell: the rolling buffer, windows and panels stay on the Colab disk (`/content/streamclipper_live`); only finished clips, thumbnails, `clips.json`/`clips.csv`, `index.html` and `live_summary.json` are copied to Drive, after every clip.
- Hook headlines: a literal line is used only when it is complete (no sentence end inside, no dangling first or last word, about 3 content words or a whole short exclamation such as 'BRO WHAT IS THAT?!'); otherwise a category template.
- The labelled eval fixtures are now `eval_stream_a.json` / `eval_stream_b.json` without video id, title or transcript text (word codes keep what the quality bar reads).
- `test_live_fake`: the 40 s time budget is a warning, not a failure.

## 5.0.0 (2026-11-17)

### Added
- `streamclipper live`: clips Twitch, YouTube Live and Kick streams while they run (streamlink ingest into a rolling buffer on disk, Whisper on every new piece, the VOD scoring and quality bar, background renders, Twitch and YouTube chat, `--buffer-min`, `--max-clips-per-hour`, `--stop-after`). Colab cell 10 runs it.
- Podcast mode (beta): `--preset podcast-9x16`, `--content talk` (strong lines, laughter, amounts, an LLM prompt for self-contained stories) and the `speaker` layout (per camera shot: a crop that follows the face, two people stacked, B-roll shown whole). New gate row `faces_whole`.
- Face-based layout detection (OpenCV, extra `vision`): per-clip facecam detection, blank-camera clips skipped, the gameplay crop keeps the streamer out.
- Precision-first quality bar with a labelled eval (`tools/eval_selection.py`, `tests/fixtures/eval/`); plain-English titles, descriptions and hashtags without an LLM; unique hook headlines per run.
- `tools/check_sync.py` (caption timing against Whisper on the MP4) and `tools/docker/Dockerfile` (Linux test image).
- `--caption-style auto` (the new default) picks the caption style per clip from its category; the chosen style is in `clips.json`, `clips.csv` and `index.html`, with a copyable re-render command per clip. `render --spec <name>.clip.json --caption-style NAME` re-renders one MP4 in another style.
- `streamclipper styles --preview` renders a sample of every caption and hook style plus `styles_gallery.png` (shown in the README and in the Colab options cell).

### Changed
- Clips are at most 60 s by default (`--max-dur 60`), rendered clips at most 59.5 s.
- `render --clips` leaves `clips.json` as it is and writes `<stem>_rendered.json/.csv/.html`.
- The resume hash covers only the speech-recognition settings, so changing `--max-dur` or `--llm` no longer re-runs Whisper.
- `streamlink` is a core dependency.

### Fixed
- Story windows end after the reaction and never cut inside a word; gain staging avoids audible limiter pumping; dark game scenes are no longer taken for a blank camera card; slice downloads retry after a network drop.

## 4.0.0 (2026-11-17)

First public release as the `streamclipper` package.

### Added
- Installable package with the `streamclipper` command (`pip install -e ".[asr]"`, extras `gpu` and `dev`). `scripts/capcut_studio.py` still runs the same CLI.
- Long streams: audio-only download, full-stream transcription in 10-minute chunks with faster-whisper on CUDA (float16, batched) with automatic CPU fallback, and resume after an interruption (`state.json`, `--fresh`). Flags `--asr`, `--whisper-model auto`, `--device`.
- Every-clip mode: `--min-score` and `--max-clips` (0 = all), non-overlapping and ranked, each clip with a 0-100 score, a hook/story/emotion/pacing/engagement breakdown, a reason and a category (funny, rage, clutch, reaction, fail, hot take, reveal, wholesome, chaos).
- `clips.csv` and a static `index.html` report with previews, thumbnails and file links.
- Chat replay signal from YouTube live chat and Twitch rechat (`--chat`, or fetched with `--url`).
- Optional LLM (`--llm ollama|gemini`) for story segments, hooks, titles, descriptions and hashtags.
- English stream-slang triggers ("no way", "let's go", "clip that", "W", "GG" ...); Hindi as a second list.
- 15 caption styles (`--caption-style`, `streamclipper styles`), 3 hook banner styles (`--hook-style`), caption options (`--caption-case`, `--words-per-caption`, `--caption-position`, `--highlight-color`, `--caption-font`, `--emoji`, `--profanity-filter`), and new OFL/Apache fonts.
- `square-frame-9x16` preset and a 1080x1920 thumbnail per clip.
- Google Colab notebook (`notebooks/StreamClipper_Colab.ipynb`) driven by `streamclipper.colab.run_pipeline`, with Drive output, resume, YouTube bot-check help and an optional local Ollama on the T4.
- Portable CapCut drafts (`--portable-drafts`, automatic without CapCut) and `streamclipper import-draft --zip` to bring drafts from Colab into CapCut Desktop.
- MIT licence, CI (ruff + pytest on Ubuntu and Windows), issue and PR templates, CONTRIBUTING.md, unit tests.

### Changed
- `--top` is now an alias of `--max-clips`; `discover-clips` and `render` default to all clips above the score, `clip-stream` still makes 3 drafts by default.
- Re-runs never overwrite MP4s, plans or sidecars: they get the `_2` name, like drafts.
- `root_meta_info.json` backups: one first backup plus the newest 5 timestamped ones.
- Presets, fonts and templates moved into the package (`src/streamclipper/data/`); `env_detected.json` moved to the user cache folder.
- The SFX of unverified licence were replaced by SFX generated with ffmpeg (CC0).

### Fixed
- The CapCut-open guard now only blocks writes into CapCut's own draft folder, so test roots set through `CAPCUT_DRAFT_ROOT` work while CapCut is open.
