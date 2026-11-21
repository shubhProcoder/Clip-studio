"""Text recognition for HUD regions: PaddleOCR's English PP-OCRv4 recognition model run with onnxruntime.

No OCR package: the game profile fixes where text appears, so only the recognition model is needed. The
model (Apache-2.0, ~7.3 MB) is downloaded on first use into the user folder and checked by SHA-256; the
character dictionary is bundled (data/models/en_dict.txt). Without onnxruntime or the model, OCR is off and
only the colour/shape cues run.
"""

import hashlib
import importlib.util
import os
import urllib.request
from pathlib import Path

from streamclipper.audio import _np
from streamclipper.paths import DATA_DIR, USER_DIR

MODEL_NAME = "en_PP-OCRv4_rec_mobile.onnx"
# RapidOCR's model hosting, pinned to release v3.10.0 (rapidocr default_models.yaml lists this file and hash)
MODEL_URL = (
    "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv4/rec/en_PP-OCRv4_rec_mobile.onnx"
)
MODEL_SHA256 = "e8770c967605983d1570cdf5352041dfb68fa0c21664f49f47b155abd3e0e318"
MODEL_BYTES = 7_653_044
DICT_FILE = DATA_DIR / "models" / "en_dict.txt"
REC_H = 48  # the model's input height
MAX_W = 640
_NOTICE = []
_CACHE = {}


def model_path() -> Path:
    return USER_DIR / "models" / MODEL_NAME


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_model(download: bool = True, url: str = MODEL_URL, dest: Path = None):
    """Path of the checked model, downloading it once (atomic write, SHA-256 checked); None when unavailable."""
    dest = Path(dest or model_path())
    if dest.is_file() and dest.stat().st_size == MODEL_BYTES:
        return dest
    if not download:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    print(f"Downloading the OCR model for HUD detection ({MODEL_BYTES / 1e6:.1f} MB, once) ...")
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        print(f"OCR model download failed ({e}); HUD uses colour cues only.")
        return None
    if _sha256(tmp) != MODEL_SHA256:
        tmp.unlink(missing_ok=True)
        print("OCR model download did not match its SHA-256; deleted. HUD uses colour cues only.")
        return None
    os.replace(tmp, dest)
    return dest


def load_chars() -> list:
    """CTC classes: blank, the 95 dictionary characters, space (PaddleOCR use_space_char)."""
    chars = [ln.rstrip("\r\n") for ln in DICT_FILE.read_text(encoding="utf-8").splitlines()]
    return ["<blank>"] + [c for c in chars if c] + [" "]


def ctc_decode(probs, chars: list) -> tuple:
    """Greedy CTC: (text, mean probability of the kept characters) from a (T, classes) probability array."""
    np = _np()
    idx = probs.argmax(axis=1)
    conf = probs.max(axis=1)
    text, ps, last = [], [], -1
    for i, p in zip(idx.tolist(), conf.tolist(), strict=True):
        if i != last and i != 0 and i < len(chars):
            text.append(chars[i])
            ps.append(p)
        last = i
    return "".join(text), (float(np.mean(ps)) if ps else 0.0)


def _resize(img, h: int, w: int):
    """Bilinear resize of an (H, W, C) float array (numpy only)."""
    np = _np()
    H, W = img.shape[:2]
    ys = (np.arange(h) + 0.5) * H / h - 0.5
    xs = (np.arange(w) + 0.5) * W / w - 0.5
    y0 = np.clip(np.floor(ys).astype(int), 0, H - 1)
    x0 = np.clip(np.floor(xs).astype(int), 0, W - 1)
    y1, x1 = np.clip(y0 + 1, 0, H - 1), np.clip(x0 + 1, 0, W - 1)
    wy = np.clip(ys - y0, 0, 1)[:, None, None]
    wx = np.clip(xs - x0, 0, 1)[None, :, None]
    a, b = img[y0][:, x0], img[y0][:, x1]
    c, d = img[y1][:, x0], img[y1][:, x1]
    return (a * (1 - wx) + b * wx) * (1 - wy) + (c * (1 - wx) + d * wx) * wy


def preprocess(rgb):
    """RGB uint8 crop -> (1, 3, 48, W) float32: BGR order, height 48, (x / 255 - 0.5) / 0.5 like PaddleOCR."""
    np = _np()
    h, w = rgb.shape[:2]
    nw = int(min(MAX_W, max(16, round(REC_H * w / max(1, h)))))
    img = _resize(rgb[..., ::-1].astype(np.float32), REC_H, nw)
    img = (img / 255.0 - 0.5) / 0.5
    return img.transpose(2, 0, 1)[None].astype(np.float32)


class Recognizer:
    """read(rgb crop) -> (text, confidence). session: an onnxruntime InferenceSession (tests pass a fake)."""

    def __init__(self, model: Path = None, session=None):
        if session is None:
            import onnxruntime as ort

            session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        self.sess = session
        self.inp = session.get_inputs()[0].name
        self.chars = load_chars()
        self.calls = 0

    def read(self, rgb) -> tuple:
        if rgb is None or rgb.shape[0] < 4 or rgb.shape[1] < 4:
            return "", 0.0
        self.calls += 1
        probs = self.sess.run(None, {self.inp: preprocess(rgb)})[0][0]
        return ctc_decode(probs, self.chars)


def ocr_available() -> bool:
    return importlib.util.find_spec("onnxruntime") is not None


def get_recognizer(download: bool = True):
    """The cached Recognizer, or None (one notice) without onnxruntime or the model."""
    if "r" in _CACHE:
        return _CACHE["r"]
    rec = None
    if not ocr_available():
        msg = "onnxruntime is not installed: HUD uses colour cues only (pip install -e .[vision])."
    else:
        path = ensure_model(download)
        msg = "" if path else "OCR model not available: HUD uses colour cues only."
        if path:
            try:
                rec = Recognizer(path)
            except Exception as e:  # a broken onnxruntime install
                msg = f"OCR could not start ({str(e).splitlines()[0][:120]}); HUD uses colour cues only."
    if rec is None and msg and not _NOTICE:
        _NOTICE.append(msg)
        print(msg)
    _CACHE["r"] = rec
    return rec


def _norm(s: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in s.upper()).split())


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def match_words(text: str, words: list, max_dist: float = 0.25):
    """(word, normalised edit distance) of the best expected word read in text (whole text, a token or two
    adjacent tokens; spaces ignored), or None when nothing is within max_dist."""
    t = _norm(text)
    if not t:
        return None
    toks = t.split()
    views = {t.replace(" ", "")} | set(toks) | {a + b for a, b in zip(toks, toks[1:], strict=False)}
    best = None
    for w in words:
        target = _norm(w).replace(" ", "")
        if not target:
            continue
        for v in views:
            d = edit_distance(v, target) / max(len(target), 1)
            if d <= max_dist and (best is None or d < best[1]):
                best = (w, round(d, 3))
    return best
