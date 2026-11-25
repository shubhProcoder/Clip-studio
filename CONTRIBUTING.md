# Contributing to StreamClipper

Thanks for helping. Bug reports, fixes, new caption styles and docs are all welcome.

## Dev setup

You need Python 3.10+ and ffmpeg (with libass and harfbuzz) on PATH.

```powershell
git clone https://github.com/shubhProcoder/Clip-studio.git
cd streamclipper
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e ".[asr,dev]"
streamclipper doctor
```

On Linux/macOS activate with `source .venv/bin/activate`. Add `gpu` to the extras on an NVIDIA machine.

## Tests and lint

```powershell
python -m pytest -q
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
```

- Tests must not need the network, a GPU, CapCut or ffmpeg. Where ffmpeg is unavoidable, skip with `pytest.mark.skipif(shutil.which("ffmpeg") is None, ...)`. The whole suite should finish in under 60 s.
- Put pure logic (scoring, story windows, captions, draft JSON) where it can be tested without media, and add a test with each change.
- CI runs the same commands on Ubuntu and Windows with Python 3.10 and 3.12.

## The Colab notebook

Do not edit `notebooks/StreamClipper_Colab.ipynb` by hand. Change `tools/make_notebook.py` and run `python tools/make_notebook.py`. Colab free-tier rules apply: form fields (`#@param`) and inline HTML output only; no Gradio, Streamlit, tunnels or web servers; Drive share links only when the user ticks share_on_drive. Keep the logic in `streamclipper.colab.run_pipeline`, so it stays testable on a PC.

## What not to put in a pull request

- **No proprietary CapCut assets** (effects, shaders, transitions, templates copied from a CapCut install). They are in `.gitignore`; keep it that way.
- **No media of unknown licence**: stream recordings, music, SFX or fonts you cannot redistribute. New fonts need an OFL or Apache licence file in `src/streamclipper/data/templates/fonts/licenses/` and a row in `FONTS.md`.
- **No secrets**: cookies files, API keys, `.env`, `env_detected.json`.
- **No renders or work folders** (`*.mp4`, `work/`, `renders/`).

## Copy and style

- English first: plain, natural English for CLI messages, docs and generated text. No hype words, no emoji spam.
- Hindi stays supported (Devanagari, UTF-16 ranges, Baloo 2). Write Hindi in Devanagari, not Hinglish.
- Follow the existing code style: `ToolError(message, hint)` for expected failures, `run_cmd()` for subprocesses, ruff line length 120.
- Never overwrite user files: new names get `_2`, `_3`; old drafts are moved, not deleted.

## Reporting a bug

Use the bug report template. Include the command, the `ERROR:`/`Fix:` lines, the output of `streamclipper doctor` and your OS. Remove any cookies paths or keys before you post logs.
