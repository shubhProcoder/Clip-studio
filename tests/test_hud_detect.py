import shutil
import subprocess

import numpy as np
import pytest

from streamclipper.hud import detect as D
from streamclipper.hud import ocr as O
from streamclipper.hud.profiles import load_profile
from streamclipper.paths import DATA_DIR

W, H = 640, 360


def colourful(seed=0):
    rng = np.random.default_rng(seed)
    fr = np.zeros((H, W, 3), np.uint8)
    for _ in range(40):  # saturated blocks like a sunny street
        x, y = rng.integers(0, W - 80), rng.integers(0, H - 60)
        fr[y : y + 60, x : x + 80] = rng.choice([[200, 60, 40], [40, 160, 60], [50, 80, 200], [210, 180, 40]])
    fr[fr.sum(axis=2) == 0] = [90, 140, 200]
    return fr


def wasted_frame():
    """Grey (desaturated) screen with a dark band and red block letters in the middle, like GTA V's WASTED."""
    rng = np.random.default_rng(1)
    g = rng.integers(60, 200, (H, W), dtype=np.uint8)
    fr = np.stack([g, g, g], axis=2)
    fr[150:210, 100:540] = 50
    for i in range(6):  # six letters
        x = 200 + i * 42
        fr[160:200, x : x + 30] = [190, 30, 35]
        fr[170:190, x + 8 : x + 22] = 50
    return fr


def star_mask(cx, cy, r_out, r_in):
    ang = np.pi / 2 + np.arange(10) * np.pi / 5
    rad = np.where(np.arange(10) % 2 == 0, r_out, r_in)
    px, py = cx + rad * np.cos(ang), cy - rad * np.sin(ang)
    yy, xx = np.mgrid[0:H, 0:W]
    inside = np.zeros((H, W), bool)
    j = 9
    for i in range(10):  # even-odd point-in-polygon
        cond = ((py[i] > yy) != (py[j] > yy)) & (xx < (px[j] - px[i]) * (yy - py[i]) / (py[j] - py[i] + 1e-9) + px[i])
        inside ^= cond
        j = i
    return inside


def stars_frame(n):
    fr = colourful(2)
    fr[0:20, 512:640] = [120, 150, 190]
    for i in range(n):
        cx = 540 + i * 19
        fr[star_mask(cx, 9, 7.5, 3.2)] = [20, 20, 20]  # dark outline
        fr[star_mask(cx, 9, 6, 2.5)] = [255, 255, 255]
    return fr


class FakeOCR:
    """Reads 'wasted' from a crop with red letters, nothing otherwise (no model needed)."""

    calls = 0

    def read(self, rgb):
        FakeOCR.calls += 1
        red = D.colour_mask(rgb, "red").mean()
        return ("wasted", 0.98) if red > 0.05 else ("", 0.0)


def test_desaturation_and_colour_cues():
    assert D.desaturation(wasted_frame()) >= 0.9
    assert D.desaturation(colourful()) < 0.6
    assert D.desaturation(np.zeros((H, W, 3), np.uint8)) == 0.0  # a black frame is not a WASTED screen
    centre = [0.1, 0.2, 0.9, 0.7]
    assert D.colour_text_band(wasted_frame(), centre, "red") > 0.01
    sub, hshare = D.text_crop(wasted_frame(), centre, "red")
    assert sub is not None and 0.08 < hshare < 0.16


def test_count_stars():
    region = [0.8, 0.0, 1.0, 0.05]
    assert D.count_stars(stars_frame(3), region) == 3
    assert D.count_stars(stars_frame(5), region) == 5
    assert D.count_stars(colourful(), region) == 0


def test_wasted_is_detected_once_with_cooldown_and_play_gives_nothing():
    prof = load_profile("gta5")
    frames = [(t * 0.5, colourful(t)) for t in range(10)]
    frames += [(5.0 + t * 0.5, wasted_frame()) for t in range(10)]  # 5 s of WASTED screen
    frames += [(10.0 + t * 0.5, colourful(t)) for t in range(6)]
    got = D.detect_events(prof, frames, FakeOCR())
    assert [(e["event"], e["t"]) for e in got] == [("wasted", 5.0)]
    assert got[0]["reason"] == "WASTED on screen" and got[0]["strength"] == 1.0
    assert D.detect_events(prof, [(t, colourful(t)) for t in range(10)], FakeOCR()) == []


def test_cue_only_mode_without_ocr_is_weaker():
    prof = load_profile("gta5")
    got = D.detect_events(prof, [(0.0, wasted_frame()), (0.5, wasted_frame())], None)
    assert {e["event"] for e in got} == {"wasted", "busted", "mission_failed"}  # colour cues cannot tell them apart
    assert all(e["strength"] < 1.0 for e in got)


def test_rising_wanted_level_and_blinking_stars():
    prof = load_profile("gta5")
    seq = [(t, stars_frame(1)) for t in range(0, 10)]
    seq += [(t, stars_frame(3) if t % 3 else stars_frame(0)) for t in range(10, 30)]  # blinking at level 3
    got = D.detect_events(prof, seq, FakeOCR())
    assert [(e["event"], e["t"]) for e in got] == [("wanted_3plus", 10)]
    assert got[0]["strength"] == 0.7
    five = D.detect_events(prof, [(t, stars_frame(5)) for t in range(5)], FakeOCR())
    assert five[0]["strength"] == 0.9


def test_flash_is_a_weak_event_only():
    prof = load_profile("generic")
    dark = np.full((H, W, 3), 20, np.uint8)
    bright = np.full((H, W, 3), 240, np.uint8)
    got = D.detect_events(prof, [(0.0, dark), (0.33, bright), (0.66, dark)], None)
    assert [(e["event"], e["strength"]) for e in got] == [("flash", 0.3)]


def test_text_burst_counts_new_lines():
    region = [0.7, 0.0, 1.0, 0.35]
    frames = []
    for k in range(5):
        fr = np.full((H, W, 3), 30, np.uint8)
        for row in range(k):  # one more kill-feed line per second
            y = 10 + row * 22
            fr[y : y + 10, 470:630:4] = 255
        frames.append((float(k), fr))
    counts = D.text_burst(frames, region, 4.0)
    assert counts[-1][1] >= 3


# ---------------------------------------------------------------- OCR runner (mocked session)


def test_ctc_decode_and_dictionary():
    chars = O.load_chars()
    assert len(chars) == 97 and chars[0] == "<blank>" and chars[-1] == " "
    idx = {c: i for i, c in enumerate(chars)}
    seq = [0, idx["w"], idx["w"], 0, idx["a"], idx["s"], 0, idx["t"], idx["e"], idx["e"], 0, idx["d"]]
    probs = np.full((len(seq), 97), 0.001, np.float32)
    probs[np.arange(len(seq)), seq] = 0.95
    text, conf = O.ctc_decode(probs, chars)
    assert text == "wasted" and conf == pytest.approx(0.95)


def test_fuzzy_word_match():
    words = ["MISSION FAILED", "FAILED"]
    assert O.match_words(" mission FaiLed", words)[1] == 0.0
    assert O.match_words("mission Fail.ed", words)[1] == 0.0  # punctuation inside a word is ignored
    assert O.match_words(" mission Faiced", words) is not None  # one wrong letter
    assert O.match_words("The family was alerted.", words) is None
    assert O.match_words("wasted", ["BUSTED"]) is None  # 2 of 6 letters differ
    assert O.match_words("WASTFD", ["WASTED"])[1] == pytest.approx(1 / 6, abs=0.001)
    assert O.edit_distance("kitten", "sitting") == 3


class FakeSession:
    def __init__(self, text):
        chars = O.load_chars()
        self.seq = [chars.index(c) for c in text]
        self.shapes = []

    def get_inputs(self):
        return [type("I", (), {"name": "x"})()]

    def run(self, _, feed):
        x = feed["x"]
        self.shapes.append(x.shape)
        probs = np.full((1, len(self.seq) * 2, 97), 0.001, np.float32)
        for i, c in enumerate(self.seq):
            probs[0, 2 * i, c] = 0.9  # char, blank, char, blank ...
            probs[0, 2 * i + 1, 0] = 0.9
        return [probs]


def test_recognizer_with_a_mocked_session():
    sess = FakeSession("BUSTED")
    rec = O.Recognizer(session=sess)
    text, conf = rec.read(np.zeros((30, 120, 3), np.uint8))
    assert text == "BUSTED" and conf > 0.8
    assert sess.shapes[0][:3] == (1, 3, 48) and sess.shapes[0][3] == 192  # height 48, aspect kept
    assert rec.read(np.zeros((2, 2, 3), np.uint8)) == ("", 0.0)


def test_model_download_checks_the_hash(tmp_path):
    dest = tmp_path / "m.onnx"
    assert O.ensure_model(download=False, dest=dest) is None
    fake = tmp_path / "fake.onnx"
    fake.write_bytes(b"not a model")
    assert O.ensure_model(url=fake.as_uri(), dest=dest) is None
    assert not dest.exists() and not dest.with_name("m.onnx.part").exists()
    assert O.MODEL_URL.startswith("https://") and "v3.10.0" in O.MODEL_URL and len(O.MODEL_SHA256) == 64


def test_real_model_reads_rendered_text(tmp_path):
    pytest.importorskip("onnxruntime")
    path = O.ensure_model(download=False)
    if path is None or shutil.which("ffmpeg") is None:
        pytest.skip("OCR model not cached (it is downloaded on first HUD use) or ffmpeg missing")
    out = tmp_path / "t.rgb"
    fonts = DATA_DIR / "templates" / "fonts"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x404040:s=320x64"]
        + ["-vf", "drawtext=fontfile=Anton-Regular.ttf:text=BUSTED:fontsize=44:fontcolor=white:x=60:y=8"]
        + ["-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", str(out)],
        check=True,
        cwd=fonts,
    )
    img = np.frombuffer(out.read_bytes(), np.uint8).reshape(64, 320, 3)
    text, conf = O.Recognizer(path).read(img)
    assert O.match_words(text, ["BUSTED"]) is not None, text
