"""Hybrid facecam detection.

Method 1 uses persistent border geometry in distributed frames.
Method 2 uses the existing MediaPipe/Haar face detector through vision.classify_layout.
Both methods are fused conservatively; low-confidence results become no-camera or
manual/configured layout fallbacks.
"""

from __future__ import annotations

from pathlib import Path

from streamclipper.audio import _np
from streamclipper.util import run_cmd

W, H = 320, 180
MIN_SIZE = 0.12
MAX_SIZE = 0.60
ASPECT_MIN = 0.55
ASPECT_MAX = 3.20


def _to_gray(rgb):
    np = _np()
    if rgb.ndim == 2:
        return rgb.astype(np.float32)
    f = rgb.astype(np.float32)
    return f[..., 0] * 0.299 + f[..., 1] * 0.587 + f[..., 2] * 0.114


def extract_frames(video: str | Path, dur: float, count: int = 12) -> list:
    np = _np()
    dur = max(0.0, float(dur))
    if dur <= 0:
        return []
    out = []
    for k in range(count):
        t = dur * (k + 0.5) / max(1, count)
        try:
            res = run_cmd(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{t:.3f}",
                    "-i",
                    str(video),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={W}:{H},format=rgb24",
                    "-f",
                    "rawvideo",
                    "-",
                ],
                "facecam border frame grab",
                binary=True,
                check=False,
            )
        except Exception:
            continue
        raw = getattr(res, "stdout", b"") or b""
        if len(raw) >= W * H * 3:
            out.append(np.frombuffer(raw[: W * H * 3], dtype=np.uint8).reshape(H, W, 3))
    return out


def _peaks(profile, limit: int = 16) -> list[tuple[float, float]]:
    np = _np()
    if len(profile) < 3:
        return []
    med = float(np.median(profile)) or 1.0
    out = []
    for i in range(1, len(profile) - 1):
        if profile[i] >= profile[i - 1] and profile[i] > profile[i + 1] and profile[i] > 2.2 * med:
            out.append((float(i), float(profile[i] / med)))
    return sorted(out, key=lambda x: -x[1])[:limit]


def border_candidates(frames: list) -> list:
    """Return persistent rectangular border candidates as normalized boxes."""
    np = _np()
    if len(frames) < 4:
        return []
    grey = np.stack([_to_gray(frame) for frame in frames], axis=0)
    gx = np.median(np.abs(np.diff(grey, axis=2)).sum(axis=1), axis=0)
    gy = np.median(np.abs(np.diff(grey, axis=1)).sum(axis=2), axis=0)
    still = grey.std(axis=0)
    game_motion = float(np.median(still))
    light = grey.mean(axis=0)
    vpeaks = _peaks(gx)
    hpeaks = _peaks(gy)
    xs = sorted({0.0, float(W)} | {x for x, _ in vpeaks})
    ys = sorted({0.0, float(H)} | {y for y, _ in hpeaks})
    vertical = {x: strength for x, strength in vpeaks}
    horizontal = {y: strength for y, strength in hpeaks}
    out = []
    for i, x0 in enumerate(xs):
        for x1 in xs[i + 1:]:
            bw = (x1 - x0) / W
            if not (MIN_SIZE <= bw <= MAX_SIZE):
                continue
            for j, y0 in enumerate(ys):
                for y1 in ys[j + 1:]:
                    bh = (y1 - y0) / H
                    if not (MIN_SIZE <= bh <= MAX_SIZE):
                        continue
                    aspect = bw / bh
                    if not (ASPECT_MIN <= aspect <= ASPECT_MAX):
                        continue
                    a, b, c, d = int(x0), int(y0), int(x1), int(y1)
                    inside = still[b:d, a:c]
                    if not inside.size:
                        continue
                    moving = float(np.median(inside))
                    cut = max(3.0, 0.30 * game_motion)
                    if moving > cut:
                        continue
                    if light[b:d, a:c].mean() < 0.30 * light.mean():
                        continue
                    border_strength = min(
                        (
                            [vertical[x] for x in (x0, x1) if x in vertical]
                            + [horizontal[y] for y in (y0, y1) if y in horizontal]
                        ),
                        default=1.0,
                    )
                    confidence = round(
                        min(
                            1.0,
                            0.35
                            + 0.08 * min(border_strength, 5.0)
                            + 0.25 * (1.0 - moving / max(cut, 1.0)),
                        ),
                        3,
                    )
                    out.append(
                        {
                            "box": [round(x0 / W, 4), round(y0 / H, 4), round(x1 / W, 4), round(y1 / H, 4)],
                            "confidence": confidence,
                            "moving": round(moving, 3),
                            "method": "border",
                        }
                    )
    out.sort(key=lambda candidate: (-candidate["confidence"], -_box_area(candidate["box"])))
    deduped = []
    for candidate in out:
        if all(iou(candidate["box"], kept["box"]) < 0.45 for kept in deduped):
            deduped.append(candidate)
    return deduped[:10]


def _box_area(box: list) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def iou(a: list, b: list) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = _box_area(a) + _box_area(b) - inter
    return inter / union if union > 0 else 0.0


def fuse(
    face_candidates: list,
    border_candidates: list,
    manual_box: list | None = None,
) -> dict | None:
    """Fuse face and border evidence into one conservative facecam result."""
    if manual_box is not None:
        box = [round(max(0.0, min(1.0, float(v))), 4) for v in manual_box]
        if len(box) != 4:
            return None
        return {
            "kind": "corner-cam",
            "cam_box": box,
            "confidence": 1.0,
            "method": "manual",
            "diagnostics": {"manual": True, "face": False, "border": False},
        }

    best_face = max(face_candidates, key=lambda x: x.get("confidence", 0.0), default=None)
    best_border = max(border_candidates, key=lambda x: x.get("confidence", 0.0), default=None)
    if best_face is None and best_border is None:
        return None
    if best_face is not None and best_border is not None:
        overlap = iou(best_face["box"], best_border["box"])
        if overlap >= 0.35:
            confidence = min(
                1.0,
                0.45 * float(best_face.get("confidence", 0.5))
                + 0.55 * float(best_border.get("confidence", 0.5))
                + 0.15 * overlap,
            )
            border_area = _box_area(best_border["box"])
            face_area = _box_area(best_face["box"])
            box = best_border["box"] if border_area >= face_area else best_face["box"]
            return {
                "kind": "corner-cam",
                "cam_box": box,
                "confidence": round(confidence, 3),
                "method": "face+border",
                "diagnostics": {"manual": False, "face": True, "border": True, "overlap": round(overlap, 3)},
            }
    if best_face is not None:
        confidence = float(best_face.get("confidence", 0.5))
        if confidence >= 0.35:
            return {
                "kind": "corner-cam" if best_face.get("corner") else "full-cam",
                "cam_box": best_face["box"] if best_face.get("corner") else None,
                "confidence": round(confidence, 3),
                "method": "face",
                "face_box": best_face.get("face_box"),
                "diagnostics": {"manual": False, "face": True, "border": False},
            }
    if best_border is not None and best_border["confidence"] >= 0.45:
        return {
            "kind": "corner-cam",
            "cam_box": best_border["box"],
            "confidence": round(best_border["confidence"], 3),
            "method": "border",
            "diagnostics": {"manual": False, "face": False, "border": True},
        }
    return None


def hybrid_detect(
    video: str | Path,
    start: float = 0.0,
    end: float | None = None,
    manual_box: list | None = None,
) -> dict:
    """Conservative hybrid detection using existing vision evidence plus static borders."""
    video = Path(video)
    if manual_box is not None:
        return fuse([], [], manual_box)
    end = float(end if end is not None else 0.0)
    dur = end - start
    face_candidates = []
    border_candidates_found = []
    try:
        from streamclipper.vision import classify_layout

        layout = classify_layout(video, start, end if dur > 0 else start + 1.0)
        if layout is not None:
            if layout.get("kind") == "corner-cam":
                box = layout.get("cam_box")
                if box:
                    face_candidates.append(
                        {
                            "box": box,
                            "face_box": layout.get("face_box"),
                            "confidence": min(1.0, 0.55 + 0.05 * layout.get("hits", 0)),
                            "corner": layout.get("corner"),
                            "method": "face",
                        }
                    )
            elif layout.get("kind") == "full-cam":
                face_candidates.append(
                    {
                        "box": layout.get("cam_box") or layout.get("face_box"),
                        "face_box": layout.get("face_box"),
                        "confidence": min(1.0, 0.55 + 0.05 * layout.get("hits", 0)),
                        "corner": None,
                        "method": "face",
                    }
                )
    except Exception:
        pass
    span = dur if dur > 0 else max(1.0, float(end or start or 1.0))
    frames = extract_frames(video, span, count=12)
    border_candidates_found = border_candidates(frames)
    fused = fuse(face_candidates, border_candidates_found)
    if fused is not None:
        return fused
    return {
        "kind": "no-cam",
        "cam_box": None,
        "confidence": 0.0,
        "method": "none",
        "diagnostics": {"manual": False, "face": False, "border": False},
    }
