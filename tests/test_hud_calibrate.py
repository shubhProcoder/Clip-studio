import json
import shutil
import subprocess

import numpy as np
import pytest

from streamclipper.cli import build_parser
from streamclipper.hud import calibrate as C
from streamclipper.hud.profiles import load_profile


def test_calibrate_hud_help_returns_0():
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["calibrate-hud", "--help"])
    assert e.value.code == 0
    a = build_parser().parse_args(["calibrate-hud", "--video", "v.mp4"])
    assert a.profile == "gta6" and a.every == 30.0 and a.check == ""


def test_sampling_and_grid():
    assert C.sample_times(100, None, 30) == [15.0, 45.0, 75.0]
    assert C.sample_times(100, [5, 50, 150], 30) == [5, 50]
    assert len(C.sample_times(10_000, None, 10)) == C.MAX_FRAMES
    cells = C.grid_cells()
    assert len(cells) == 16 and cells[0] == [0.0, 0.0, 0.25, 0.25] and cells[-1] == [0.75, 0.75, 1.0, 1.0]


def test_profile_skeleton_from_reads():
    base = load_profile("gta6")
    cell_a, cell_b = [0.25, 0.5, 0.5, 0.75], [0.5, 0.5, 0.75, 0.75]
    reads = [(10.0, cell_a, "WASTED", 0.9), (40.0, cell_b, "WASTED", 0.9)]
    reads += [(10.0, [0.0, 0.0, 0.25, 0.25], "HEIST", 0.8), (70.0, [0.0, 0.0, 0.25, 0.25], "HEIST", 0.8)]
    reads += [(70.0, cell_a, "ONCE", 0.8)]  # read on one frame only: not an event
    doc = C.build_profile(base, reads, "my_gta6")
    ev = {e["id"]: e for e in doc["events"]}
    assert ev["wasted"]["region"] == [0.1, 0.425, 0.9, 0.825]  # both read areas, grown by 30%
    assert ev["heist"]["words"] == ["HEIST"] and ev["heist"]["method"] == "ocr"
    assert "once" not in ev and doc["experimental"] and doc["name"] == "my_gta6"


class BrightOCR:
    """Reads WASTED from any crop with a bright block (no model needed)."""

    def read(self, rgb):
        return ("WASTED", 0.95) if (rgb.mean(axis=2) > 200).mean() > 0.2 else ("", 0.0)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_calibrate_on_a_synthetic_video_writes_profile_and_sheet(tmp_path):
    video = tmp_path / "hud.mp4"
    # grey video with a white text-like bar in the cell at column 2, row 2 (x 0.25-0.5, y 0.5-0.75)
    vf = "drawbox=x=180:y=200:w=120:h=24:color=white:t=fill,drawbox=x=190:y=206:w=100:h=4:color=black:t=fill"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x505050:s=640x360:r=10"]
        + ["-t", "6", "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    out = tmp_path / "my_gta6.json"
    doc = C.calibrate(video, load_profile("gta6"), BrightOCR(), out, every=2.0)
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["name"] == "my_gta6" and load_profile(out)["name"] == "my_gta6"
    x0, y0, x1, y1 = next(e for e in doc["events"] if e["id"] == "wasted")["region"]
    # the drawn bar is x 180-300, y 200-224 of 640x360: the region holds it and little else
    assert x0 < 300 / 640 and x1 > 180 / 640 and y0 < 224 / 360 and y1 > 200 / 360  # overlaps the bar
    assert (x1 - x0) * (y1 - y0) < 0.1
    sheet = out.with_name("calibrate_sheet.jpg")
    assert sheet.is_file() and sheet.stat().st_size > 1000


def test_sheet_draws_boxes(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    fr = np.full((360, 640, 3), 80, np.uint8)
    p = C.write_sheet([(fr, [[0.0, 0.0, 0.5, 0.5]])], tmp_path / "s.jpg")
    assert p.is_file()
