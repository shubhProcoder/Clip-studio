# CapCut Desktop Schema, `crop` Split-Screen & Glowing Text Contract

## 1. Mandatory CapCut Desktop Safety Rules
1. **Close CapCut Before Writing**: CapCut must not be running while a draft folder or `root_meta_info.json` is modified. `streamclipper` stops with an error when CapCut is open; it force-closes `CapCut.exe` and `VEDetector.exe` only with `--close-capcut`.
2. **Never Overwrite Existing User Drafts**: Only overwrite a folder when `"overwrite": true` (or `--overwrite`) is explicitly passed; otherwise `_2`, `_3`, ... is appended to the name.
3. **Test Drafts Go To a Test Root**: Pass `--draft-root <folder>` (or env `CAPCUT_DRAFT_ROOT`). The CapCut-open guard only blocks writes into CapCut's own draft folder.
4. **Clean Lock & Timeline Cache**: When overwriting, delete `Timelines/` and `.locked`; always write the exact 26-byte `EMPTY_DRAFT_EXTRA` binary into `draft.extra`.
5. **Update All Sync Files Together**: Write `draft_content.json`, `draft_content.json.bak`, `draft_meta_info.json`, `timeline_layout.json`, `draft_settings`, `draft.extra` and the companion JSON files from `src/streamclipper/data/templates/`, then back up `root_meta_info.json` (`.streamclipper_first.bak` once + `.streamclipper_<timestamp>.bak`, newest 5) and prepend the new entry.

---

## 2. `timeline_spec.json` Full Contract (Supports 16:9 Split `crop` & Glowing Multi-Color Captions)

```json
{
  "project_name": "GTA6_Viral_01",
  "overwrite": true,
  "canvas_ratio": "9:16",
  "fps": 30.0,
  "caption_style": {
    "font_size": 11.5,
    "pos_y": 0.24,
    "stroke_width": 0.08,
    "glow": true,
    "glow_color": "yellow",
    "glow_strength": 0.45,
    "glow_range": 0.72,
    "auto_highlight": true
  },
  "video_track": [
    {
      "source": "D:/Streams/gta6_stream.mp4",
      "src_start": 120.40,
      "src_end": 124.80,
      "crop": [0.21, 0.0, 0.79, 1.0],
      "volume": 2.2,
      "zoom": 1.28,
      "pos_x": 0.0,
      "pos_y": -0.34
    }
  ],
  "overlay_tracks": [
    [
      {
        "source": "D:/Streams/gta6_stream.mp4",
        "src_start": 120.40,
        "src_end": 124.80,
        "tl_start": 0.00,
        "crop": [0.0, 0.0, 0.36, 0.42],
        "volume": 0.0,
        "zoom": 1.0,
        "pos_x": 0.0,
        "pos_y": 0.64
      }
    ]
  ],
  "captions": [
    {
      "text": "{yellow:WAIT FOR} {red:THE NPC} 💀",
      "tl_start": 0.00,
      "tl_end": 1.50,
      "font_size": 13.5,
      "pos_y": 0.24,
      "glow": true,
      "glow_color": "yellow"
    },
    {
      "text": "Bro this GTA6 physics is INSANE",
      "tl_start": 1.50,
      "tl_end": 3.20,
      "glow": true,
      "auto_highlight": true
    }
  ],
  "audio_tracks": []
}
```

### Key Capabilities
- **`crop: [x0, y0, x1, y1]`**: Normalized `0.0..1.0` bounding box applied natively to `materials.videos[i]["crop"]`. Used to keep the streamer's webcam or in-game player 100% uncut in the top panel while centering gameplay in the bottom panel.
- **Multi-Color + Glowing Captions**:
  - Tag specific words with `{yellow:...}`, `{green:...}`, `{red:...}`, `{cyan:...}`, `{orange:...}`, `{pink:...}`, `{purple:...}`, or `*...*`.
  - Or leave `"auto_highlight": true` and let `streamclipper` automatically highlight numbers, uppercase words, and viral power words in rotating neon colors.
  - Setting `"glow": true` attaches CapCut's native `TextBloom` shader (`effect_id: 7194814295517958657` by default) and sets `check_flag: 71` (15 without glow). The drop shadow stays crisp black: `has_shadow: true`, `shadow_alpha: 0.88`, `shadow_smoothing: 0.025`, `shadow_distance: 4.5`, style shadow `diffuse: 0.025`.

---

## 3. Portable Drafts and `import-draft`

Drafts built on another machine (Colab, or with `--portable-drafts`) must survive a move. The writer switches to portable mode when `--portable-drafts` is passed or CapCut is not installed:

- Every video/audio file the draft uses is copied into `<draft>/media/` and the material `path` points there.
- Paths inside the user's CapCut folder are written as `__CAPCUT_LOCAL__/...`; paths of the bundled fonts as `__STREAMCLIPPER_FONTS__/...`.
- `streamclipper_portable.json` in the draft folder: `{"type": "streamclipper_portable", "version": 1, "draft_name", "media": {path_in_draft: file_name}, "capcut_local_token", "fonts_token"}`.

`streamclipper import-draft --zip capcut_drafts.zip [--draft-root R] [--overwrite]`:

1. Refuses zip entries with absolute paths, drive letters or `..` parts (zip-slip) before extracting.
2. Extracts to a temp folder next to R, runs the CapCut-open guard for R and finds every folder with `draft_content.json`.
3. Picks a free name (`_2`, `_3` ...), or with `--overwrite` moves the old draft to `streamclipper_replaced_drafts/` next to R (never deletes it).
4. Rewrites each material path to `R/<draft>/media/<file>` (manifest entry first, else the same base name), fills in both tokens for this PC, updates `name`/`id` in `draft_content.json(.bak)` and `draft_name`, `draft_fold_path`, `draft_root_path` in `draft_meta_info.json`. A draft id already registered in R gets a new id.
5. Backs up `root_meta_info.json` and registers the draft (same entry as the writer).