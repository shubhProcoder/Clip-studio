"""Face-based layout detection (MediaPipe BlazeFace, Haar fallback: see faces.py; pip install -e .[vision])."""

from pathlib import Path

from streamclipper import faces as _faces
from streamclipper.audio import _np
from streamclipper.util import run_cmd

SAMPLES = 8
FULL_FACE_H = 0.18  # face height (fraction of the frame) that means a full-screen cam
CORNER_MIN_HITS = 5  # of SAMPLES frames
MAX_MOVE = 0.08  # corner face centre may move at most this much of the frame width between samples


def _detector():
    """The face detector backend (faces.get_detector), or None: callers then keep their old fallbacks."""
    return _faces.get_detector()


def _grab_rgb(video: Path, t: float, w: int, h: int, crop: tuple = None):
    np = _np()
    res = run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, t):.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            (f"crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]}," if crop else "") + f"scale={w}:{h},format=rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        "face detection frame grab",
        binary=True,
        check=False,
    )
    buf = res.stdout or b""
    if len(buf) < w * h * 3:
        return None
    return np.frombuffer(buf[: w * h * 3], np.uint8).reshape(h, w, 3)


def _grab_size(det, vw: int, vh: int) -> tuple:
    w = det.grab_w
    return w, max(2, int(round(w * vh / max(1, vw) / 2)) * 2)


def detect_faces(video: Path, times: list, vw: int = 1920, vh: int = 1080, crop: tuple = None):
    """
    Faces per sample time as normalised boxes [x0, y0, x1, y1] (largest first), or None when no face
    detector is installed (callers then keep the skin-colour heuristic). MediaPipe BlazeFace on a 960 px
    frame plus tiles, or the Haar cascades on 640 px (faces.get_detector), of the whole frame or of the
    pixel box crop=(x, y, w, h) (vw/vh are then the crop size). The grey frames are kept on
    detect_faces.frames (edge snapping, dark share), the full detections (score, keypoints) on
    detect_faces.detections.
    """
    det = _detector()
    if det is None:
        return None
    w, h = _grab_size(det, vw, vh)
    out, frames, dets = [], [], []
    for t in times:
        img = _grab_rgb(video, t, w, h, crop)
        if img is None:
            out.append([])
            dets.append([])
            continue
        frames.append(_faces.to_gray(img))
        found = det.detect(img)
        dets.append(found)
        out.append([d["box"] for d in found])
    detect_faces.frames = frames
    detect_faces.detections = dets
    return out


def scene_cuts(video: Path, start: float, end: float, threshold: float = 0.15) -> list:
    """Hard cut times (seconds in the file) in [start, end], from ffmpeg's scene score on a 320 px copy.
    0.15: cuts between shots on a dark studio background score only 0.2-0.3; a false cut only splits a
    shot in two (each part is framed on its own)."""
    import re

    res = run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "info",
            "-ss",
            f"{max(0.0, start):.3f}",
            "-t",
            f"{max(0.1, end - start):.3f}",
            "-i",
            str(video),
            "-an",
            "-vf",
            f"scale=320:-2,select='gt(scene,{threshold})',showinfo",
            "-f",
            "null",
            "-",
        ],
        "scene cut scan",
        check=False,
    )
    return [round(start + float(t), 3) for t in re.findall(r"pts_time:([\d.]+)", res.stderr or "")]


def track_faces(video: Path, start: float, end: float, step: float = 0.5, vw: int = 1920, vh: int = 1080):
    """
    Faces every `step` seconds in [start, end] for the speaker layout, or None without a face detector:
    {times, faces (normalised boxes per sample, largest first), mouths (BlazeFace mouth keypoint per box,
    None with Haar), frames (grey, the detector's grab size), cuts (scene cuts), detector}.
    One ffmpeg decode for all samples; the backend of detect_faces.
    """
    det = _detector()
    if det is None:
        return None
    np = _np()
    w, h = _grab_size(det, vw, vh)
    res = run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, start):.3f}",
            "-t",
            f"{max(0.1, end - start):.3f}",
            "-i",
            str(video),
            "-an",
            "-vf",
            f"fps=1/{step},scale={w}:{h},format=rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        "face tracking frames",
        binary=True,
    )
    raw = res.stdout or b""
    fs = w * h * 3
    n = len(raw) // fs
    faces, mouths, frames = [], [], []
    for k in range(n):
        rgb = np.frombuffer(raw[k * fs : (k + 1) * fs], np.uint8).reshape(h, w, 3)
        found = det.detect(rgb)
        frames.append(_faces.to_gray(rgb))
        faces.append([d["box"] for d in found])
        mouths.append([(d.get("keypoints") or {}).get("mouth") for d in found])
    times = [round(start + k * step, 3) for k in range(n)]
    return {
        "times": times,
        "faces": faces,
        "mouths": mouths,
        "frames": frames,
        "cuts": scene_cuts(video, start, end),
        "detector": det.name,
    }


def mouth_motion(frames: list, boxes: list) -> float:
    """Mean frame difference in the mouth region (lower 40% of the face box) over consecutive samples
    where the face was found (boxes[k] None = not found); talking moves the mouth, listening does not."""
    np = _np()
    diffs = []
    for k in range(1, min(len(frames), len(boxes))):
        a, b = boxes[k - 1], boxes[k]
        if a is None or b is None:
            continue
        h, w = frames[k].shape
        x0, x1 = int(b[0] * w), max(int(b[0] * w) + 2, int(b[2] * w))
        y0 = int((b[1] + 0.6 * (b[3] - b[1])) * h)
        y1 = max(y0 + 2, int(b[3] * h))
        cur = frames[k][y0:y1, x0:x1].astype(np.int16)
        prev = frames[k - 1][y0:y1, x0:x1].astype(np.int16)
        if cur.size and cur.shape == prev.shape:
            diffs.append(float(np.abs(cur - prev).mean()))
    return sum(diffs) / len(diffs) if diffs else 0.0


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _corner(cx: float, cy: float):
    if 0.3 <= cx <= 0.7:
        return None
    return ("top_" if cy < 0.5 else "bottom_") + ("left" if cx < 0.5 else "right")


def _centre(b) -> tuple:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def _median(vals: list) -> float:
    v = sorted(vals)
    k = len(v) // 2
    return v[k] if len(v) % 2 else (v[k - 1] + v[k]) / 2


def _median_box(boxes: list) -> list:
    return [round(_median([b[i] for b in boxes]), 4) for i in range(4)]


def classify_faces(faces: list) -> dict:
    """
    Decision rules on per-sample face lists (normalised boxes, largest first; see detect_faces):
      corner-cam - a face whose centre is in the same side region (x < 0.3 or x > 0.7) on >= 5 of 8
                   samples, each within 8% of the frame width of the median centre (a stable overlay).
                   Large corner cams count (a large cut-out cam is ~23% of the frame height);
                   only a face >= 35% of the frame height is treated as a full-screen cam;
      full-cam   - on at least half of the samples the biggest face has its centre in the middle 60%
                   of the frame and is >= 18% of the frame height (or is >= 35% anywhere);
      no-cam     - anything else; a side face that moves more than 8% is reported with moving=True.
    Returns {kind, corner, face_box (median box of the stable faces), hits}.
    """
    n = max(1, len(faces))
    need = max(2, int(round(CORNER_MIN_HITS * n / SAMPLES)))
    big, by_corner = [], {}
    for fl in faces:
        if not fl:
            continue
        f = fl[0]
        fh = f[3] - f[1]
        cx, _ = _centre(f)
        if fh >= 2 * FULL_FACE_H - 0.01 or (0.2 <= cx <= 0.8 and _corner(cx, 0.5) is None and fh >= FULL_FACE_H):
            big.append(f)
        for g in fl:
            c = _corner(*_centre(g))
            if c and (g[3] - g[1]) < 2 * FULL_FACE_H - 0.01:
                by_corner.setdefault(c, []).append(g)
                break
    if by_corner:
        corner, hits = max(by_corner.items(), key=lambda kv: len(kv[1]))
        if len(hits) >= need:
            mx, my = _centre(_median_box(hits))
            stable = [g for g in hits if abs(_centre(g)[0] - mx) <= MAX_MOVE and abs(_centre(g)[1] - my) <= 0.12]
            if len(stable) >= need and len(big) < len(stable):
                return {"kind": "corner-cam", "corner": corner, "face_box": _median_box(stable), "hits": len(stable)}
            if len(big) * 2 < n:
                return {"kind": "no-cam", "corner": corner, "face_box": None, "hits": len(hits), "moving": True}
    if len(big) * 2 >= n and len(big) >= 2:
        return {"kind": "full-cam", "corner": None, "face_box": _median_box(big), "hits": len(big)}
    return {"kind": "no-cam", "corner": None, "face_box": None, "hits": 0}


def pad_cam_box(face: list, corner: str) -> list:
    """
    Webcam box around a stable corner face: padded 15% around the face cluster, then grown so the face
    is at most ~45% of the box width / ~55% of its height (head, hair and shoulders stay in).
    """
    x0, y0, x1, y1 = face
    fw, fh = x1 - x0, y1 - y0
    px, py = max(0.15 * fw, 0.62 * fw), max(0.15 * fh, 0.45 * fh)
    return [max(0.0, x0 - px), max(0.0, y0 - 1.3 * py), min(1.0, x1 + px), min(1.0, y1 + 1.4 * py)]


def snap_to_overlay(box: list, corner: str, frames: list, face: list) -> list:
    """
    Snap the inner edges of a corner cam box to the overlay rectangle: a fixed overlay border is a
    strong edge in every frame while gameplay edges average out, so the column/row with the highest
    mean gradient between the face and the frame centre (> 3x the median) is the border.
    """
    np = _np()
    if not frames:
        return box
    st = np.stack([f.astype(np.float32) for f in frames])
    h, w = st.shape[1], st.shape[2]
    gx = np.abs(np.diff(st, axis=2)).mean(axis=0)  # h x (w-1)
    gy = np.abs(np.diff(st, axis=1)).mean(axis=0)  # (h-1) x w
    fx0, fy0, fx1, fy1 = face
    out = list(box)
    r0, r1 = int(fy0 * h), max(int(fy0 * h) + 2, int(fy1 * h))
    c0, c1 = int(fx0 * w), max(int(fx0 * w) + 2, int(fx1 * w))
    left = corner.endswith("left")
    top = corner.startswith("top")
    # vertical border (x): right of a left cam / left of a right cam
    fw, fh = fx1 - fx0, fy1 - fy0  # the border is searched at most one face width / height from the face
    lo, hi = (int(fx1 * w) + 2, int(min(0.6, fx1 + fw) * w)) if left else (int(max(0.4, fx0 - fw) * w), c0 - 2)
    if hi - lo > 4:
        prof = gx[r0:r1, lo:hi].mean(axis=0)
        k = int(prof.argmax())
        if prof[k] > 3.0 * max(1e-3, float(np.median(gx[r0:r1]))):
            out[2 if left else 0] = (lo + k + 1) / w
    # horizontal border (y): below a top cam / above a bottom cam
    lo, hi = (int(fy1 * h) + 2, int(min(0.7, fy1 + fh) * h)) if top else (int(max(0.3, fy0 - fh) * h), r0 - 2)
    if hi - lo > 4:
        prof = gy[lo:hi, c0:c1].mean(axis=1)
        k = int(prof.argmax())
        if prof[k] > 3.0 * max(1e-3, float(np.median(gy[:, c0:c1]))):
            out[3 if top else 1] = (lo + k + 1) / h
    if left:
        out[0] = 0.0
    else:
        out[2] = 1.0
    if top:
        out[1] = 0.0
    else:
        out[3] = 1.0
    # never cut the face: the snapped box must still hold it with a small margin
    out = [
        min(out[0], fx0 - 0.1 * fw),
        min(out[1], fy0 - 0.25 * fh),
        max(out[2], fx1 + 0.1 * fw),
        max(out[3], fy1 + 0.1 * fh),
    ]
    return [round(max(0.0, min(1.0, v)), 4) for v in out]


def classify_layout(video: Path, start: float, end: float, vw: int = 1920, vh: int = 1080, faces: list = None):
    """
    {kind: corner-cam|full-cam|no-cam|blank, corner, cam_box, face_box, dark} from 8 frames inside
    [start, end], or None when OpenCV is not available. 'blank': at least half of the frames are mostly
    black (camera-disconnected card, black screen). faces: injected per-sample face lists (tests).
    """
    frames = []
    if faces is None:
        span = max(0.1, end - start)
        times = [start + span * (k + 0.5) / SAMPLES for k in range(SAMPLES)]
        faces = detect_faces(video, times, vw, vh)
        if faces is None:
            return None
        frames = getattr(detect_faces, "frames", [])
    res = classify_faces(faces)
    res["cam_box"] = None
    res["dark"] = round(dark_share(frames), 2)
    if res["kind"] == "corner-cam":
        res["facecam_confidence"] = round(min(1.0, 0.55 + 0.05 * res.get("hits", 0)), 3)
        res["facecam_method"] = "face"
    elif res["kind"] == "full-cam":
        res["facecam_confidence"] = round(min(1.0, 0.55 + 0.05 * res.get("hits", 0)), 3)
        res["facecam_method"] = "face"
    else:
        res["facecam_confidence"] = 0.0
        res["facecam_method"] = "none"
    if res["dark"] >= 0.5:
        res.update(kind="blank", face_box=None)
    if res["kind"] == "corner-cam":
        face = res["face_box"]
        if getattr(_detector(), "name", "") == "MediaPipe":
            # the cam padding was calibrated on Haar boxes; BlazeFace boxes are ~0.78x their size (brow to chin)
            face = _faces.haar_sized(face)
        box = pad_cam_box(face, res["corner"])
        res["cam_box"] = snap_to_overlay(box, res["corner"], frames, face) if frames else box
    return res


def person_box(face: list, corner: str) -> list:
    """
    Normalised box that holds the whole streamer around a corner-cam face: 1.8 face widths to each
    side (shoulders, arms, hands of a keyed streamer), from 0.6 face heights above the face to the
    frame bottom for a lower cam, or from the frame top to 3.5 face heights below the face for a top cam.
    """
    x0, y0, x1, y1 = face
    fw, fh = x1 - x0, y1 - y0
    bx0, bx1 = max(0.0, x0 - 1.8 * fw), min(1.0, x1 + 1.8 * fw)
    if str(corner or "").startswith("top"):
        by0, by1 = 0.0, min(1.0, y1 + 3.5 * fh)
    else:
        by0, by1 = max(0.0, y0 - 0.6 * fh), 1.0
    return [round(bx0, 4), round(by0, 4), round(bx1, 4), round(by1, 4)]


def face_crop_region(face: list, vw: int, vh: int, aspect: float = 9 / 16) -> list:
    """Normalised full-height region of the given aspect centred on the face (full-screen cam layout)."""
    cx = (face[0] + face[2]) / 2
    half = min(0.5, aspect * vh / vw / 2)
    x0 = min(max(0.0, cx - half), 1.0 - 2 * half)
    return [round(x0, 4), 0.0, round(x0 + 2 * half, 4), 1.0]


# grey level below which a pixel counts as black: near pure black only. A blank card has ~73% of its pixels
# below 10; dark game scenes (night maps, dungeons) have <= 33% below 10 but often > 50% below 24.
DARK_LUMA = 10
DARK_AREA = 0.5  # a frame is blank when at least this share of its pixels is black


def dark_share(frames: list) -> float:
    """Share of grey frames that are mostly black (a blank 'camera disconnected' card, a black screen)."""
    if not frames:
        return 0.0
    return sum(1 for f in frames if float((f < DARK_LUMA).mean()) >= DARK_AREA) / len(frames)


def check_face_box(video: Path, offset: float, dur: float, box: list):
    """
    (status, detail) for the webcam box (pixels x, y, w, h) of a rendered split, from 6 frames of the
    window file: PASS when a face is found well inside the box on most samples, FAIL when the face
    touches the box edge (cut) on most samples, WARN when no face is found. None without OpenCV.
    """
    x, y, w, h = [int(v) for v in box]
    times = [offset + max(0.1, dur) * (k + 0.5) / 6 for k in range(6)]
    faces = detect_faces(video, times, w, h, crop=(x, y, w, h))
    if faces is None:
        return None
    good = cut = 0
    for fl in faces:
        if not fl:
            continue
        f = fl[0]
        if f[0] <= 0.01 or f[1] <= 0.01 or f[2] >= 0.99 or f[3] >= 0.99:
            cut += 1
        else:
            good += 1
    if good + cut == 0:
        return "WARN", f"no face found in the cam box {x},{y},{w},{h} (6 samples)"
    if cut * 2 > good + cut:
        return "FAIL", f"face touches the cam box edge in {cut}/{good + cut} samples; set --cam-crop"
    return "PASS", f"face inside the cam box in {good}/{good + cut} samples (cam box {x},{y},{w},{h})"
