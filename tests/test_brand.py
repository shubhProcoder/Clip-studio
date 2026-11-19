import json
import shutil
import subprocess
from pathlib import Path

import pytest

from streamclipper.brand import (
    MAX_CARD_S,
    card_seconds,
    load_brand_kit,
    nearest_colour,
    place_box,
    plan_brand,
    ui_zones,
)
from streamclipper.util import ToolError

EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "examples"
HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _kit(tmp_path, data, logo=True):
    if logo:
        shutil.copy2(EXAMPLES / "brand_logo.png", tmp_path / "logo.png")
    p = tmp_path / "kit.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_example_kit_loads_with_absolute_paths():
    kit = load_brand_kit(EXAMPLES / "brand_kit.json")
    assert Path(kit["logo"]["image"]).is_absolute() and Path(kit["logo"]["image"]).is_file()
    assert kit["handle"] == "@yourname" and kit["intro"]["seconds"] == 1.0 and kit["outro"]["seconds"] == 1.5
    assert len(kit["sha1"]) == 40 and card_seconds(kit) == (1.0, 1.5)


@pytest.mark.parametrize(
    "data, msg",
    [
        ({"logo": {"image": "missing.png"}}, "not found"),
        ({"logo": {"image": "logo.png", "position": "middle"}}, "position"),
        ({"logo": {"image": "logo.png", "width_pct": 60}}, "width_pct"),
        ({"logo": {"image": "logo.png", "opacity": 0.05}}, "opacity"),
        ({"colours": {"highlight": "yellow"}}, "#FFE600"),
        ({"handle": "x" * 31}, "handle"),
        ({"font": "logo.png"}, "TTF/OTF"),
        ({"intro": {"seconds": 1.0}}, "image or a text"),
        ({"logos": {}}, "unknown key"),
    ],
)
def test_schema_errors_have_hints(tmp_path, data, msg):
    with pytest.raises(ToolError) as e:
        load_brand_kit(_kit(tmp_path, data))
    assert msg in str(e.value) and e.value.hint


def test_relative_paths_and_intro_capped(tmp_path, capsys):
    p = _kit(tmp_path, {"logo": "logo.png", "intro": {"text": "HI", "seconds": 4}})
    kit = load_brand_kit(p)
    assert kit["logo"]["image"] == str((tmp_path / "logo.png").resolve())
    assert kit["intro"]["seconds"] == MAX_CARD_S and "shortened" in capsys.readouterr().out


def test_nearest_colour():
    assert nearest_colour("#FFE000") == "yellow"
    assert nearest_colour("#FF3030") == "red"


def test_corner_avoids_captions_faces_and_the_app_buttons():
    W, H = 1080, 1920
    assert place_box("auto", 130, 130, W, H, [])[0] == "top-left"
    cap_top_left = [[0, 100, 500, 400]]
    assert place_box("auto", 130, 130, W, H, cap_top_left)[0] == "top-right"
    face_top_right = [[700, 80, 1080, 500]]
    # both top corners blocked; the bottom corners are under the platform UI on 9:16 -> nothing (after a shrink)
    assert place_box("auto", 130, 130, W, H, cap_top_left + face_top_right) is None
    assert ui_zones(1080, 1080) == []
    assert place_box("auto", 130, 130, 1080, 1080, cap_top_left + face_top_right)[0] == "bottom-left"


def test_shrink_then_skip():
    W, H = 1080, 1920
    # a caption just under a full-size top-left logo: the 30% smaller logo fits above it
    blocker = [[0, 230, 1080, 600]]
    corner, box = place_box("top-left", 160, 160, W, H, blocker)
    assert corner == "top-left" and box[3] - box[1] < 160 and box[3] <= 230
    assert place_box("top-left", 160, 160, W, H, [[0, 0, 1080, 600]]) is None


def test_plan_brand_puts_the_handle_under_the_logo(tmp_path):
    kit = load_brand_kit(_kit(tmp_path, {"logo": {"image": "logo.png"}, "handle": "@me"}))
    p = plan_brand(kit, 1080, 1920, [])
    lg, hd = p["logo"]["box"], p["handle"]["box"]
    assert p["logo"]["corner"] == "top-left" and hd[1] >= lg[3]
    skipped = plan_brand(kit, 1080, 1920, [[0, 0, 1080, 1920]])
    assert skipped["logo"] is None and skipped["handle"] is None and len(skipped["notes"]) == 2


def _panel(path: Path, dur: float = 3.0):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=1080x1920:r=30:d={dur}"]
        + ["-f", "lavfi", "-i", f"sine=f=220:d={dur},volume=0.3", "-f", "lavfi", "-i", f"anoisesrc=d={dur}:a=0.02"]
        + ["-filter_complex", "[1:a][2:a]amix=inputs=2,aformat=channel_layouts=stereo[a]", "-map", "0:v", "-map"]
        + ["[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000"]
        + [str(path)],
        check=True,
    )


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_render_with_every_effect_shifts_the_timeline(tmp_path):
    from streamclipper.render import render_plan_mp4

    _panel(tmp_path / "main.mp4")
    words = [{"w": w, "s": 0.4 + i * 0.5, "e": 0.8 + i * 0.5} for i, w in enumerate(["this", "is", "so", "insane"])]
    plan = {
        "name": "fx",
        "panels": {"main_src": str(tmp_path / "main.mp4"), "overlay_src": None},
        "layout": "full",
        "ratio": "9:16",
        "segments": [{"src_s": 0.0, "src_e": 3.0, "tl_start": 0.0, "kind": "story", "fade_ms": 0, "zoom": 1.0}],
        "words": words,
        "peak": 1.5,
        "captions": [{"text": w["w"].upper(), "tl_start": w["s"], "tl_end": w["e"], "anim": "none"} for w in words],
        "hook_text": "",
        "caption_style": "bold-pop",
        "caption_opts": {},
        "sfx": [],
        "expected_duration": 3.0,
        "effects": {
            "zoom": "medium",
            "enhance_speech": "light",
            "brand_kit": str(EXAMPLES / "brand_kit.json"),
        },
        # a cached face-safe placement (render skips the face lookup when the level matches)
        "zoom_plan": {"level": "medium", "main": [{"t": 1.0, "level": 1.12, "fx": 0.5, "fy": 0.5}], "overlay": []},
    }
    mp4 = render_plan_mp4(plan, tmp_path / "out" / "fx.mp4")
    side = json.loads(mp4.with_name("fx.timeline.json").read_text(encoding="utf-8"))
    graph = mp4.with_name("fx.graph.txt").read_text(encoding="utf-8")
    assert (
        "arnndn=m=../_models/bd.rnnn:mix=1" in graph
        and "weights='0.6 0.4'" in graph
        and "perspective=" in graph
        and "overlay=" in graph
    )
    assert "[introv][introa][vbody][abody][outrov][outroa]concat=n=3" in graph
    assert side["expected_duration"] == pytest.approx(5.5)
    assert side["segments"][0]["tl_start"] == pytest.approx(1.0)
    assert min(s[0] for s in side["caption"]["spans"]) >= 1.0
    assert side["caption"]["words_tl"][0][1] == pytest.approx(1.4)
    assert side["effects"]["brand"]["cards"] == {"intro": 1.0, "outro": 1.5}
    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(mp4)],
        capture_output=True,
        text=True,
    ).stdout
    assert float(dur) == pytest.approx(5.5, abs=0.06)
