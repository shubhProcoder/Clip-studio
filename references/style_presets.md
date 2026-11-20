# CapCut Style Presets, 16:9 Split Layouts & Viral Hook Grabbers

---

## 1. Stream Clipping Layouts (`16:9` $\rightarrow$ `9:16` or `1:1` Without Cutting the Player!)

When converting `16:9` gameplay/streams (GTA 6, Twitch, YouTube) into vertical `9:16` (`1080x1920`) or square `1:1` (`1080x1080`), never blindly center-crop a single track if it cuts off the streamer or player character. Use `detect_player_and_webcam()` (or `--cam-crop`) and pre-rendered panels. Layouts as implemented in `streamclipper` (CLI presets in `src/streamclipper/data/presets/`):

| Layout Mode | Target Ratio | Top Panel (`Overlay Track 0`) | Bottom Panel (`Main Video Track`) | Seam Caption Position |
| :--- | :---: | :--- | :--- | :--- |
| **`streamer-split-9x16`** | `9:16` (`1080x1920`) | **Webcam panel 1080x672** (`pos_y: +0.65`), webcam box fitted over a blurred copy (never stretched or cropped). | **Gameplay panel 1080x1248** (`pos_y: -0.35`), aspect-correct crop below the webcam (or `--game-crop`). | `pos_y: +0.18` (just below the seam) |
| **`streamer-split-1x1`** | `1:1` (`1080x1080`) | **Webcam panel 1080x454** (`pos_y: +0.58`). | **Gameplay panel 1080x626** (`pos_y: -0.42`), aspect-correct crop. | `pos_y: -0.02` (just below the seam) |
| **`square-frame-9x16`** | `9:16` (`1080x1920`) | Hook banner in y 150-430 over the blurred background. | **Gameplay frame 1080x900** at y=470 (6:5 crop shifted so it never cuts the webcam box) over a full-bleed blurred copy (no darkening, no haze). | captions at y >= 1400 |
| **`uncut-16x9-stack`** (hook mode) | `9:16` or `1:1` | **Webcam panel** as above. | **Full 16:9 frame** fitted into the gameplay panel over a blurred fill (no horizontal crop). | as the split layout |

---

## 2. The 5 Viral Hook Grabber Methods (Anti-Scroll Openers)

1. **`tease-climax` (Flash-Forward Climax Tease):**
   - Places a `1.2s–2.0s` flash of the payoff at `t = 0.0s` full-canvas with a giant `font_size: 13.5` Glowing Multi-Color Hook Banner (`{yellow:WAIT FOR} {red:THE END} 💀`), then cuts to the Setup.
2. **`story-stitch` (Setup $\rightarrow$ Payoff, in order):**
   - Plays the story window in order with pauses over 0.55 s trimmed; the hook headline shows over the first 2 s. It does not search for a claim minutes earlier.
3. **`detail-zoom` (Missed NPC / Easter Egg 1.5x–2.2x Zoom):**
   - Zooms the first piece by `1.18x` (centred; it does not find a detail), with the hook headline over the first `2.0s`.
4. **`split-shock` (Loud Chaos $\rightarrow$ Pin-Drop Silence Contrast):**
   - Ranks clips by `shock_contrast = peak_db - post_silence_db` to catch moments where something so crazy happens that the streamer goes completely silent in disbelief.
5. **`uncut-16x9-stack` (Wide Cinematic + Reaction Stack):**
   - Ideal for GTA 6 trailers, cutscenes, and wide driving/heist sequences where every pixel of the `16:9` width matters.

---

## 3. Glowing Effect & Big Fancy Multi-Color Font Spec

Every caption or hook banner supports:
- **`font_size`:** `11.0` to `15.0` (big impact viral typography).
- **Inline Multi-Color Tags:** `{yellow:TEXT}`, `{green:TEXT}`, `{red:TEXT}`, `{cyan:TEXT}`, `{orange:TEXT}`, `{pink:TEXT}`, `{purple:TEXT}`, or `*TEXT*` (auto-yellow).
- **Automatic Power-Word Highlighting (`auto_highlight: true`):** Automatically colors numbers (`100%`, `6`, `5`), uppercase words, and viral trigger words (`GTA6`, `WAIT`, `CRAZY`, `INSANE`, `WTF`, `SECRET`, `NPC`, `WASTED`) in alternating neon colors using CapCut's `useLetterColor: true` multi-range `styles[]`.
- **Dual-Layer Neon Glow (`glow: true`):**
  1. Attaches CapCut's native `TextBloom` shader (`effect_id: 7194814295517958657`, `panel_id: "text_glow"`) in `materials.effects`.
  2. Sets `has_shadow: true`, `check_flag: 71`, `shadow_smoothing: 0.025`, `shadow_alpha: 0.88`, `shadow_distance: 4.5` + black outline (`stroke_width: 0.07` by default) so the glow is crisp on any background. Never raise the shadow `diffuse` above 0.15.

---

## 3b. Caption Styles and Hook Styles (`--caption-style`, `--hook-style`)

`src/streamclipper/captions/styles.py` holds the registry (`STYLES`, `STYLE_ALIASES`, `HOOK_STYLES`); `streamclipper styles` prints it. Each style has an ASS renderer (MP4) and a CapCut mapping (draft):

| Style | MP4 (ASS) | CapCut draft |
| :--- | :--- | :--- |
| `bold-pop` | 1-2 words, Barlow, yellow active word, pop scale | pop-bounce keyframes + glow |
| `beast-bold` | Luckiest Guy, outline 10, green active word | thick stroke + keyframes |
| `karaoke-highlight` | 3 words, active word coloured per event | per-word style ranges |
| `word-by-word` | 1 word, Bebas Neue, no animation | one segment per word |
| `two-line-stack` | 4 words on 2 lines | 2-line text, glow |
| `highlight-box` | active word on a yellow BorderStyle 3 box | yellow text background on the caption, spoken word red |
| `neon-glow` | blurred colour layer under a crisp layer | TextBloom neon |
| `impact-shake` | 1 word, Anton, +-8 px jitter for 120 ms | 3 position keyframes |
| `emoji-pop` | bold-pop + keyword emoji (Noto Emoji) | native emoji |
| `typewriter` | up to 4 words revealed per word (never per character) | one segment per step |
| `bounce` | `\move` overshoot 30 px | position keyframes |
| `gradient-stroke` | two outline layers in 2 colours | stroke colour + shadow |
| `comic-burst` | Bangers, yellow fill, black outline 8, `\frz-4` + pop | rotation + keyframes |
| `minimal-clean` | 3-5 words, Poppins ExtraBold, outline 3 | plain |
| `gaming-glitch` | cyan/magenta +-4 px ghosts for 150 ms | two offset ghost segments |

Hook styles: `bold-box` (black on a yellow box, hype words red), `outline-glow` (white with a blurred yellow outline), `top-bar` (white on a full-width red bar). Devanagari words always use Baloo 2 and keep UTF-16 ranges.

---

## 4. Style Guidance for Other Formats (not CLI presets; the CLI presets are in `src/streamclipper/data/presets/`)

| Preset ID | Aspect Ratio | Audio / Music | Cutting & Zoom Rhythm | Captions |
| :--- | :---: | :--- | :--- | :--- |
| **`viral-stream-clipper`** | `9:16` or `1:1` | Stream audio `+7 dB` (`2.2`). Optional riser/music bed (`0.12`). | `shock_contrast` + RMS `-48 dB` cuts; Uncut Top Player + Bottom Gameplay split. | Glowing Multi-Color (`size: 11.5–13.5`, `pos_y: +0.24` seam, `glow: true`). |
| **`talking-head`** | `9:16` | Voice `+8 dB` (`2.51`), Music OFF. | `-48 dB` RMS gate (`min_pause: 0.22s`), `< 3.0s` non-overexposed B-roll overlays. | Clean bold (`size: 7.0–9.0`, `pos_y: -0.10`), 2–4 words synced. |
| **`podcast-clip`** | `9:16` or `4:5` | Voice `+6 dB` (`2.0`). | Alternating `1.0x / 1.12x` speaker zoom punch-ins; natural `0.28s` breath tolerance. | Glowing multi-color keywords (`size: 10.0`, `pos_y: -0.15`). |
| **`faceless-montage`** | `9:16` | Music ON (`0.85`), 0.3s fade-in, 0.8s fade-out. | Fast `0.7s–1.8s` visual cuts synced to beat/energy; zero duplicate windows. | Big glowing hook title (`size: 12.0`, `pos_y: 0.10`). |
| **`tutorial-screen`** | `16:9` | Mic voice (`1.8`) + optional `-24 dB` lo-fi (`0.06`). | Cut dead pauses `>= 0.35s`; zoom `1.35x` onto active UI regions. | Step banners or lower subtitles (`size: 6.0`, `pos_y: -0.75`). |
| **`product-ad`** | `9:16` | Voice `+7 dB` + optional bed (`0.10`). | Hook `< 2.0s` -> Problem -> `< 2.8s` close-up B-roll (`hi240 <= 1.25%`) -> Offer/CTA. | Bold benefit highlights (`size: 9.5`, `pos_y: -0.10`, `glow: true`). |
