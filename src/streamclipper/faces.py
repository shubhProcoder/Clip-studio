"""Face detection backends (MediaPipe BlazeFace with tiling, Haar fallback), smoothed tracks, active speaker.

get_detector() -> MediaPipeFaces | HaarFaces | None. detect(rgb) -> [{box, score, keypoints}] largest first,
box = [x0, y0, x1, y1] normalised to the frame. smooth_track and pick_speaker are pure functions.
"""

import atexit
import importlib.util
import threading
from pathlib import Path

from streamclipper.audio import _np
from streamclipper.paths import DATA_DIR

MODEL_DIR = DATA_DIR / "models"
# full range: measured on a corner facecam (60 px face in a 1080p frame) it found the face on 14/20 frames
# with tiles, short range on 0/20 (FEAT-001 notes), so the full-range model is the one bundled
FULL_RANGE = MODEL_DIR / "blaze_face_full_range.tflite"
MP_GRAB_W = 960  # MediaPipe frames: 960 px wide, whole frame + 3x2 overlapping tiles
HAAR_GRAB_W = 640
MIN_SCORE = 0.5
DEDUPE_IOU = 0.3
# BlazeFace boxes are tighter than Haar's (measured: 0.76-0.82x the height on the same faces, brow to chin). The
# size thresholds still hold: full-cam faces measured 0.40 (FULL_FACE_H 0.18), podcast close-ups 0.22,
# corner-cam faces 0.08 (corner limit 0.35), so the constants stay. Only the facecam box padding, which must
# keep hair and ears in, uses a Haar-sized box (haar_sized).
HAAR_SCALE = 1.25


def haar_sized(box: list) -> list:
    """A BlazeFace box grown about its centre to the size Haar gives the same face (forehead and ears in)."""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    hw, hh = (box[2] - box[0]) * HAAR_SCALE / 2, (box[3] - box[1]) * HAAR_SCALE / 2
    return [round(min(1.0, max(0.0, v)), 4) for v in (cx - hw, cy - hh, cx + hw, cy + hh)]


_DETECTOR = {}


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _dedupe(found: list) -> list:
    """Detections (dicts with box) -> largest first, a box overlapping a kept one by IoU >= 0.3 dropped."""
    found = sorted(found, key=lambda d: -(d["box"][2] - d["box"][0]) * (d["box"][3] - d["box"][1]))
    keep = []
    for d in found:
        if all(_iou(d["box"], k["box"]) < DEDUPE_IOU for k in keep):
            keep.append(dict(d, box=[round(v, 4) for v in d["box"]]))
    return keep


def tiles(w: int, h: int) -> list:
    """(x, y, tw, th) pixel tiles: a 3x2 grid of half-width x two-thirds-height tiles (50% overlap)."""
    tw, th = w // 2, (2 * h) // 3
    xs = [0, (w - tw) // 2, w - tw]
    ys = [0, h - th]
    return [(x, y, tw, th) for y in ys for x in xs]


def map_tile(det: dict, tile: tuple, w: int, h: int) -> dict:
    """A detection normalised to a tile -> normalised to the whole frame (box and keypoints)."""
    x, y, tw, th = tile
    b = det["box"]
    box = [(x + b[0] * tw) / w, (y + b[1] * th) / h, (x + b[2] * tw) / w, (y + b[3] * th) / h]
    kp = {k: ((x + px * tw) / w, (y + py * th) / h) for k, (px, py) in (det.get("keypoints") or {}).items()}
    return dict(det, box=box, keypoints=kp)


_KP_NAMES = ("right_eye", "left_eye", "nose", "mouth", "right_ear", "left_ear")


class MediaPipeFaces:
    """BlazeFace full-range (MediaPipe Tasks) on the whole frame plus 3x2 tiles, so small faces are found too."""

    name = "MediaPipe"
    grab_w = MP_GRAB_W

    def __init__(self, model: Path = FULL_RANGE, min_score: float = MIN_SCORE):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        self._mp = mp
        opts = vision.FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(model)), min_detection_confidence=min_score
        )
        self._det = vision.FaceDetector.create_from_options(opts)
        # close before interpreter shutdown: mediapipe's __del__ prints a traceback once its dispatcher thread pool
        # is shut down; threading's exit hooks run before concurrent.futures' own (atexit runs too late)
        getattr(threading, "_register_atexit", atexit.register)(self.close)

    def close(self):
        det, self._det = getattr(self, "_det", None), None
        if det is not None:
            try:
                det.close()
            except Exception:
                pass

    def _run(self, rgb) -> list:
        np = _np()
        h, w = rgb.shape[:2]
        img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        out = []
        for d in self._det.detect(img).detections:
            bb = d.bounding_box
            box = [bb.origin_x / w, bb.origin_y / h, (bb.origin_x + bb.width) / w, (bb.origin_y + bb.height) / h]
            box = [min(1.0, max(0.0, v)) for v in box]
            if box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
                continue
            kp = {n: (k.x, k.y) for n, k in zip(_KP_NAMES, d.keypoints or [], strict=False)}
            score = float(d.categories[0].score) if d.categories else 0.0
            out.append({"box": box, "score": round(score, 3), "keypoints": kp})
        return out

    def detect(self, rgb) -> list:
        h, w = rgb.shape[:2]
        found = self._run(rgb)
        for t in tiles(w, h):
            x, y, tw, th = t
            for d in self._run(rgb[y : y + th, x : x + tw]):
                b = d["box"]
                # a box cut by the tile border is a partial face: the whole-frame pass or a neighbour tile has it
                edge = (b[0] <= 0.002 and x > 0) or (b[1] <= 0.002 and y > 0)
                edge = edge or (b[2] >= 0.998 and x + tw < w) or (b[3] >= 0.998 and y + th < h)
                if not edge:
                    found.append(map_tile(d, t, w, h))
        return _dedupe(found)


class HaarFaces:
    """OpenCV Haar cascades (frontal + profile + mirrored profile) on an equalised grey frame (5.2 behaviour)."""

    name = "Haar (OpenCV)"
    grab_w = HAAR_GRAB_W

    def __init__(self, cv2):
        self.cv2 = cv2
        base = Path(cv2.data.haarcascades)
        self.cascades = []
        for name in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml"):
            c = cv2.CascadeClassifier(str(base / name))
            if not c.empty():
                self.cascades.append(c)

    def detect_gray(self, img) -> list:
        cv2 = self.cv2
        h, w = img.shape[:2]
        eq = cv2.equalizeHist(img)
        min_side = max(16, int(h * 0.05))
        found = []
        for k, c in enumerate(self.cascades):
            views = [(eq, False)] + ([(cv2.flip(eq, 1), True)] if k == 1 else [])
            for im, flipped in views:
                for x, y, fw, fh in c.detectMultiScale(im, 1.1, 6, minSize=(min_side, min_side)):
                    x0 = (w - x - fw) if flipped else x
                    found.append({"box": [x0 / w, y / h, (x0 + fw) / w, (y + fh) / h], "score": 1.0, "keypoints": {}})
        return _dedupe(found)

    def detect(self, rgb) -> list:
        return self.detect_gray(to_gray(rgb))


def to_gray(rgb):
    """RGB uint8 -> grey uint8 (BT.601 luma, like ffmpeg's format=gray)."""
    np = _np()
    if rgb.ndim == 2:
        return rgb
    g = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    return np.clip(g + 0.5, 0, 255).astype(np.uint8)


def _haar_cv2():
    """cv2 with Haar cascades, or None (not installed, or an OpenCV 5 wheel without cascades)."""
    try:
        import cv2
    except ImportError:
        return None
    if not hasattr(cv2, "CascadeClassifier") or not hasattr(cv2, "data"):
        return None
    return cv2


def _mediapipe_ok() -> bool:
    return importlib.util.find_spec("mediapipe") is not None and FULL_RANGE.is_file()


def get_detector():
    """The best installed backend (cached): MediaPipe, else Haar, else None (callers keep their fallbacks)."""
    if "d" in _DETECTOR:
        return _DETECTOR["d"]
    det = None
    if _mediapipe_ok():
        try:
            det = MediaPipeFaces()
        except Exception as e:  # a broken mediapipe install: fall back, say why once
            _DETECTOR["err"] = str(e).splitlines()[0][:120] if str(e) else type(e).__name__
            print(f"MediaPipe face detector could not start ({_DETECTOR['err']}); using Haar.")
            det = None
    if det is None:
        cv2 = _haar_cv2()
        if cv2 is not None:
            det = HaarFaces(cv2)
            if not det.cascades:
                det = None
    _DETECTOR["d"] = det
    return det


MP_SYSLIB_HINT = "on Linux install its system libraries: apt-get install -y libegl1 libgles2"


def detector_status() -> tuple:
    """(name, detail) for doctor: 'MediaPipe' | 'Haar fallback' | 'none'."""
    if _mediapipe_ok():
        import mediapipe as mp

        det = get_detector()  # really start it: an import can work while a system library is missing
        if det is not None and det.name == "MediaPipe":
            return "MediaPipe", f"BlazeFace full-range + tiling (mediapipe {getattr(mp, '__version__', '?')})"
        why = f"MediaPipe could not start ({_DETECTOR.get('err', 'unknown error')}); " + MP_SYSLIB_HINT
        return ("Haar fallback" if det is not None else "none"), why
    if _haar_cv2() is not None:
        return "Haar fallback", "OpenCV Haar cascades (pip install -e .[vision] for MediaPipe)"
    return "none", "no face detector: layouts use the skin-colour heuristic (pip install -e .[vision])"


# ---------------------------------------------------------------- smoothing and the active speaker

EMA_ALPHA = 0.35  # weight of the newest sample in the smoothed face centre
DEAD_ZONE = 0.30  # the crop holds still while the face stays in the middle 30% of the crop
SIZE_CHANGE = 0.15  # the crop size follows the face only when it changed by more than 15%
SWITCH_LEAD_S = 1.0  # a new speaker must lead this long ...
SWITCH_RATIO = 1.3  # ... by this factor before the shot switches


def smooth_track(centres: list, times: list, crop_w: float, max_speed: float, sizes: list = None) -> list:
    """
    Raw face centres (x, normalised; None = no face) at `times` -> crop centres that hold still in a dead zone
    (the face stays inside the middle DEAD_ZONE of a crop crop_w wide), follow an exponential average
    (alpha EMA_ALPHA) otherwise, and never move faster than max_speed (normalised units per second).
    sizes (optional face heights): returns [(x, size)] where size changes only when it moved > SIZE_CHANGE.
    """
    out = []
    ema = cur = None
    size = None
    half_dz = DEAD_ZONE * crop_w / 2
    prev_t = None
    for i, (c, t) in enumerate(zip(centres, times, strict=False)):
        if c is not None:
            ema = c if ema is None else EMA_ALPHA * c + (1 - EMA_ALPHA) * ema
        if cur is None:
            cur = ema
        elif ema is not None:
            target = cur
            if abs(ema - cur) > half_dz:
                target = ema - half_dz if ema > cur else ema + half_dz
            step = max_speed * max(0.0, t - prev_t) if prev_t is not None else 0.0
            cur = cur + max(-step, min(step, target - cur))
        prev_t = t
        if sizes is not None:
            s = sizes[i] if i < len(sizes) else None
            if s is not None and (size is None or abs(s - size) > SIZE_CHANGE * size):
                size = s
            out.append((None if cur is None else round(cur, 4), size))
        else:
            out.append(None if cur is None else round(cur, 4))
    return out


def speech_spans(words: list, offset: float = 0.0, join: float = 0.35, pad: float = 0.15) -> list:
    """Word times (source s) -> merged [(s, e)] stretches of speech, shifted by -offset (window time)."""
    out = []
    for w in sorted(words or [], key=lambda x: x["s"]):
        s, e = float(w["s"]) - offset - pad, float(w["e"]) - offset + pad
        if out and s - out[-1][1] < join:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(round(max(0.0, s), 3), round(e, 3)) for s, e in out]


def voiced_at(times: list, islands: list) -> list:
    """True per sample time when it falls inside a speech island ({cut_start, cut_end} or (s, e))."""
    spans = [(i["cut_start"], i["cut_end"]) if isinstance(i, dict) else (i[0], i[1]) for i in islands or []]
    return [any(a <= t <= b for a, b in spans) for t in times]


def pick_speaker(scores: list, times: list, voiced: list = None) -> list:
    """
    Per-sample speaker index from scores[k][j] (mouth activity of face j at sample k, 0 when not seen).
    Starts on the first face with any mouth motion; switches to another face only after it led for
    SWITCH_LEAD_S with >= SWITCH_RATIO x the current speaker's score, and never during silence (voiced False).
    """
    if not scores:
        return []
    n_faces = max((len(s) for s in scores), default=0)
    if n_faces == 0:
        return [None] * len(scores)
    voiced = voiced if voiced is not None else [True] * len(scores)
    cur = None
    lead_j, lead_t0 = None, None
    out = []
    for k, (row, t) in enumerate(zip(scores, times, strict=False)):
        row = list(row) + [0.0] * (n_faces - len(row))
        best = max(range(n_faces), key=lambda j: row[j])
        if cur is None:
            if row[best] > 0:
                cur = best
                out = [cur] * len(out)  # the samples before the first motion show the same face
            out.append(cur if cur is not None else 0)
            continue
        if not voiced[k]:
            lead_j, lead_t0 = None, None
            out.append(cur)
            continue
        if best != cur and row[best] >= SWITCH_RATIO * max(row[cur], 1e-6):
            if lead_j != best:
                lead_j, lead_t0 = best, t
            if t - lead_t0 >= SWITCH_LEAD_S - 1e-6:
                cur, lead_j, lead_t0 = best, None, None
        else:
            lead_j, lead_t0 = None, None
        out.append(cur)
    return out


def mouth_box(box: list, mouth=None) -> list:
    """Normalised mouth region: around BlazeFace's mouth keypoint, else the lower 40% of the face box."""
    x0, y0, x1, y1 = box
    fw, fh = x1 - x0, y1 - y0
    if mouth:
        mx, my = mouth
        return [mx - 0.3 * fw, my - 0.15 * fh, mx + 0.3 * fw, my + 0.2 * fh]
    return [x0, y0 + 0.6 * fh, x1, y1]
