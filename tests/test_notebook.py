from pathlib import Path

import pytest

nbformat = pytest.importorskip("nbformat")

NB = Path(__file__).resolve().parents[1] / "notebooks" / "StreamClipper_Colab.ipynb"
FORBIDDEN = ("gradio", "streamlit", "ngrok", "share=true", "localtunnel", "cloudflared", "pyngrok")


def _nb():
    return nbformat.read(str(NB), as_version=4)


def test_notebook_is_valid():
    nbformat.validate(_nb())


def test_code_cells_compile():
    code = [c for c in _nb().cells if c.cell_type == "code"]
    assert len(code) == 10
    for i, cell in enumerate(code, start=1):
        lines = [ln for ln in cell.source.splitlines() if not ln.lstrip().startswith(("!", "%"))]
        compile("\n".join(lines), f"cell {i}", "exec")


def test_no_web_ui_or_share_links():
    text = NB.read_text(encoding="utf-8").lower()
    for word in FORBIDDEN:
        assert word not in text, word


def test_live_cell_runs_streamclipper_live():
    live = [c.source for c in _nb().cells if c.cell_type == "code" and "Live clipping" in c.source]
    assert len(live) == 1
    for param in ("live_url", "stop_after_minutes", "min_score", "max_clips_per_hour", "caption_style"):
        assert f"{param} = " in live[0] and "#@param" in live[0]
    assert '"live", "--url"' in live[0] and "drive_out_dir(" in live[0]
    # the rolling buffer stays on the Colab disk; only finished results are copied to Drive
    assert '"--out", str(LIVE_WORK)' in live[0] and "LIVE_WORK = live_work_dir(" in live[0]
    assert "sync_live_outputs(LIVE_WORK, LIVE_OUT)" in live[0]


def test_run_cell_calls_run_pipeline():
    src = "\n".join(c.source for c in _nb().cells if c.cell_type == "code")
    assert "run_pipeline(" in src
    assert "#@param" in src


def test_drive_is_optional_and_drive_links_are_opt_in():
    cells = [c.source for c in _nb().cells]
    src = "\n".join(cells)
    assert 'use_google_drive = True  #@param {type:"boolean"}' in src
    assert 'share_on_drive = False  #@param {type:"boolean"}' in src
    assert "Drive access stays inside your own Google account. StreamClipper has no server." in cells[0]
    assert "Anyone with the link can see the clip." in src
    assert "if result and share_on_drive:\n            share_run(result)" in src
    out = next(c for c in cells if c.startswith("#@title 9."))
    assert (
        "zip_outputs(" in out and "if is_colab():" in out and out.index("if is_colab():") < out.index("files.download(")
    )
    assert "progress is not saved" in src


def test_53_options_and_vision_install():
    cells = [c.source for c in _nb().cells]
    install = next(c for c in cells if c.startswith("#@title 2."))
    assert install.count("[{EXTRAS}]") == 3 and "[asr]" not in install
    assert 'EXTRAS = "asr,gpu,vision" if globals().get("HAS_GPU", False) else "asr,vision"' in install
    assert "capture_output=True" in install and "print(doctor.stdout, doctor.stderr)" in install
    opts = next(c for c in cells if c.startswith("#@title 6."))
    assert 'hud = "auto"  #@param ["auto", "off", "gta5", "gta6", "generic"]' in opts
    assert 'zoom = "off"  #@param ["off", "light", "medium", "strong"]' in opts
    assert 'remove_fillers = False  #@param {type:"boolean"}' in opts
    assert 'enhance_speech = "off"  #@param ["off", "light", "strong"]' in opts
    run = next(c for c in cells if c.startswith("#@title 7."))
    for kw in ("hud=hud", "zoom=zoom", "remove_fillers=remove_fillers", "enhance_speech=enhance_speech"):
        assert kw in run
    assert "first, then run this cell again." in run  # no NameError when cells 5/6 were skipped
    assert "Publishing to YouTube Shorts or Instagram Reels runs from your PC" in cells[0]


def test_caption_style_defaults_to_auto_with_the_gallery():
    src = "\n".join(c.source for c in _nb().cells if c.cell_type == "code")
    assert src.count('caption_style = "auto"  #@param ["auto", ') == 2
    assert "docs/media/styles_gallery.jpg" in src
