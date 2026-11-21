"""Video probing, webcam detection, panel rendering and canvas geometry."""

import json
import math
import os
import re
from pathlib import Path

from streamclipper.audio import _np
from streamclipper.faces import mouth_box
from streamclipper.util import ToolError, file_tag, fwd, require_tool, run_cmd


def probe_video(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ToolError(f"File not found: {path}", "check the path; put quotes around paths with spaces")
    require_tool("ffprobe")
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    res = run_cmd(cmd, f"ffprobe {path.name}", hint="the file may be damaged or not a media file")
    info = json.loads(res.stdout)
    vstream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    astream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = float(info.get("format", {}).get("duration", 0.0))
    w = int(vstream["width"]) if vstream else 1080
    h = int(vstream["height"]) if vstream else 1920
    rot = 0
    if vstream:
        for sd in vstream.get("side_data_list", []):
            if "rotation" in sd:
                rot = int(sd["rotation"])
        if abs(rot) in (90, 270):
            w, h = h, w
    fps = 60.0
    if vstream and "/" in str(vstream.get("r_frame_rate", "")):
        n, d = vstream["r_frame_rate"].split("/")
        if float(d) > 0:
            fps = round(float(n) / float(d), 3)
    return {
        "file": path.name,
        "path": str(path).replace("\\", "/"),
        "dur": round(dur, 4),
        "width": w,
        "height": h,
        "rotation": rot,
        "fps": fps,
        "has_audio": astream is not None,
        "vcodec": (vstream or {}).get("codec_name", ""),
        "pix_fmt": (vstream or {}).get("pix_fmt", ""),
        "acodec": (astream or {}).get("codec_name", ""),
        "sample_rate": int((astream or {}).get("sample_rate", 0) or 0),
    }


def detect_player_and_webcam(video_path: Path, dur: float) -> dict:
    """
    Hybrid facecam detection: existing MediaPipe/Haar evidence plus persistent border geometry.
    Low-confidence/no-camera results are returned explicitly and never force a split layout.
    """
    result = None
    try:
        from streamclipper.facecam import hybrid_detect

        result = hybrid_detect(video_path, 0.0, max(0.0, dur))
    except Exception:
        result = None
    webcam_box = result.get("cam_box") if result and result.get("confidence", 0.0) >= 0.35 else None
    if webcam_box is not None:
        cx = (webcam_box[0] + webcam_box[2]) / 2
        cy = (webcam_box[1] + webcam_box[3]) / 2
        if cy < 0.5:
            corner = "top-right" if cx >= 0.5 else "top-left"
        else:
            corner = "bottom-right" if cx >= 0.5 else "bottom-left"
    else:
        corner = "none"
    confidence = round(float(result.get("confidence", 0.0) if result else 0.0), 3)
    method = result.get("method", "none") if result else "none"
    return {
        "webcam_detected": webcam_box is not None,
        "webcam_corner": corner,
        "player_top_crop": [round(v, 4) for v in webcam_box] if webcam_box is not None else None,
        "facecam_confidence": confidence,
        "facecam_method": method,
        "facecam_diagnostics": result.get("diagnostics", {}) if result else {},
        "gameplay_9x16_crop": [0.1146, 0.2037, 0.8854, 1.0],
        "gameplay_1x1_crop": [0.1146, 0.1852, 0.8854, 1.0],
    }


def render_split_panels(
    video_path: Path,
    out_dir: Path,
    webcam_box: list,
    game_box: list = None,
    ratio: str = "9:16",
    start_s: float = None,
    dur_s: float = None,
    meta: dict = None,
    face_only: bool = False,
) -> dict:
    """
    Split-screen panels for one clip window (aspect-correct, never stretched):
      - Top panel (1080x672 in 9:16, 1080x454 in 1:1): the webcam box grown to the panel's aspect
        (never cropped, so the face stays whole) and scaled to fill the full width: no side bars.
      - Bottom panel (1080x1248 in 9:16, 1080x626 in 1:1): an aspect-correct crop of the gameplay
        below the webcam (or around game_box when given), centred horizontally.
    The panels meet edge to edge: no dark band at the seam (captions carry their own outline/box).
    Boxes are normalised [x0, y0, x1, y1]. start_s/dur_s limit the render to the clip window.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = meta or probe_video(video_path)
    vw, vh = meta["width"], meta["height"]
    px0, py0, pw, ph = _norm_to_px(webcam_box, vw, vh)

    if ratio == "9:16":
        top_w, top_h = 1080, 672
        bot_w, bot_h = 1080, 1248
        top_pos_y, bot_pos_y, seam_y = 0.65, -0.35, 0.18  # captions just below the 672 px face seam
    else:  # 1:1 (1080x1080)
        top_w, top_h = 1080, 454
        bot_w, bot_h = 1080, 626
        top_pos_y, bot_pos_y, seam_y = 0.58, -0.42, -0.02

    if game_box:
        gw, gh, gx0, gy0 = _fill_crop(_norm_to_px(game_box, vw, vh), vw, vh, bot_w / bot_h)
    elif py0 < vh // 4 and py0 + ph < vh // 2:
        gy0 = (py0 + ph) & ~1
        gw, gh, gx0, gy0 = _fill_crop((0, gy0, vw, vh - gy0), vw, vh, bot_w / bot_h)
    else:  # a lower or tall cam: shift the gameplay crop sideways so the face is not shown twice
        gw, gh, gx0, gy0 = frame_crop(vw, vh, None, (px0, py0, pw, ph), bot_w / bot_h)

    cx0, cy0, cw_, ch_ = grow_to_aspect((px0, py0, pw, ph), vw, vh, top_w / top_h)
    tag = file_tag(video_path, (cx0, cy0, cw_, ch_), (gw, gh, gx0, gy0), start_s, dur_s, ratio, "v5")
    rtag = ratio.replace(":", "x")
    face_mp4 = out_dir / f"{video_path.stem}_facecam_{rtag}_{tag}.mp4"
    game_mp4 = out_dir / f"{video_path.stem}_gameplay_{rtag}_{tag}.mp4"
    ss_args = _ss_args(start_s, dur_s)

    if not face_mp4.is_file():
        fc = (
            f"[0:v]crop={cw_}:{ch_}:{cx0}:{cy0},scale={top_w}:{top_h}:force_original_aspect_ratio=increase,"
            f"crop={top_w}:{top_h},setsar=1[v]"
        )
        _encode_panel(video_path, face_mp4, fc, ss_args, keep_audio=False, what="facecam panel render")

    if not face_only and not game_mp4.is_file():
        fc = f"[0:v]crop={gw}:{gh}:{gx0}:{gy0},scale={bot_w}:{bot_h}:flags=lanczos,setsar=1[v]"
        _encode_panel(video_path, game_mp4, fc, ss_args, keep_audio=True, what="gameplay panel render")
    return {
        "facecam_mp4": fwd(face_mp4),
        "gameplay_mp4": None if face_only else fwd(game_mp4),
        "bot_w": bot_w,
        "bot_h": bot_h,
        "top_pos_y": top_pos_y,
        "bot_pos_y": bot_pos_y,
        "seam_caption_y": seam_y,
        "top_scale": 1.0,
        "bot_scale": 1.0,
    }


def _norm_to_px(box: list, vw: int, vh: int) -> tuple:
    """Normalised [x0, y0, x1, y1] -> even pixel (x, y, w, h) inside the frame."""
    x0, y0, x1, y1 = [float(v) for v in box]
    px0 = max(0, int(round(x0 * vw)) & ~1)
    py0 = max(0, int(round(y0 * vh)) & ~1)
    pw = max(64, int(round((x1 - x0) * vw)) & ~1)
    ph = max(64, int(round((y1 - y0) * vh)) & ~1)
    pw = min(pw, (vw - px0) & ~1)
    ph = min(ph, (vh - py0) & ~1)
    return px0, py0, pw, ph


def _fill_crop(region: tuple, vw: int, vh: int, aspect: float) -> tuple:
    """Largest crop with the given aspect (w/h) centred in region (x, y, w, h); even ints (w, h, x, y)."""
    rx, ry, rw, rh = region
    rw, rh = max(2, rw), max(2, rh)
    if rw / rh > aspect:
        cw, ch = rh * aspect, rh
    else:
        cw, ch = rw, rw / aspect
    cw = min(vw & ~1, max(2, int(round(cw)) & ~1))
    ch = min(vh & ~1, max(2, int(round(ch)) & ~1))
    x0 = int(round(rx + rw / 2 - cw / 2))
    y0 = int(round(ry + rh / 2 - ch / 2))
    x0 = max(0, min(vw - cw, x0)) & ~1
    y0 = max(0, min(vh - ch, y0)) & ~1
    return cw, ch, x0, y0


def _ss_args(start_s, dur_s) -> list:
    args = []
    if start_s is not None:
        args += ["-ss", f"{float(start_s):.3f}"]
    if dur_s is not None:
        args += ["-t", f"{float(dur_s):.3f}"]
    return args


def grow_to_aspect(box_px: tuple, vw: int, vh: int, aspect: float) -> tuple:
    """
    Pixel box (x, y, w, h) grown (never shrunk) along its short side to the aspect w/h, centred on
    the box and shifted to stay inside the frame; when the frame is too small for that, the other side
    is trimmed around the centre. Even ints (x, y, w, h).
    """
    x, y, w, h = box_px
    cx, cy = x + w / 2, y + h / 2
    if w / max(1, h) < aspect:
        w = min(vw, h * aspect)
        h = min(h, w / aspect)
    else:
        h = min(vh, w / aspect)
        w = min(w, h * aspect)
    w, h = max(2, int(round(w)) & ~1), max(2, int(round(h)) & ~1)
    x0 = max(0, min(vw - w, int(round(cx - w / 2)))) & ~1
    y0 = max(0, min(vh - h, int(round(cy - h / 2)))) & ~1
    return x0, y0, w, h


def black_spans(video_path: Path, box: list, vw: int, vh: int, start: float, end: float) -> list:
    """[(a, b)] seconds in [start, end] where the region box (normalised) is a black picture for >= 1 s
    (70% of it black; a chat overlay
    may cover the rest; shorter game fades do not count)."""
    x, y, w, h = _norm_to_px(box, vw, vh)
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
            str(video_path),
            "-an",
            "-vf",
            f"crop={w}:{h}:{x}:{y},scale=240:-2,blackdetect=d=1.0:pix_th=0.12:pic_th=0.7",
            "-f",
            "null",
            "-",
        ],
        "gameplay black scan",
    )
    return [
        (start + float(a), start + float(b))
        for a, b in re.findall(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)", res.stderr or "")
    ]


def gameplay_box_avoiding(person: list, vw: int, vh: int, aspect: float, min_h: float = 0.75):
    """
    Largest aspect-correct gameplay crop (normalised [x0, y0, x1, y1]) that does not overlap the
    person box (normalised), from the free regions left, right, above and below it; None when the best
    one is shorter than min_h of the frame height (the caller then picks another layout).
    """
    px0, py0, px1, py1 = person
    regions = [
        (int(px1 * vw + 0.999) + 2, 0, vw - int(px1 * vw + 0.999) - 2, vh),
        (0, 0, int(px0 * vw) - 2, vh),
        (0, 0, vw, int(py0 * vh) - 2),
        (0, int(py1 * vh + 0.999) + 2, vw, vh - int(py1 * vh + 0.999) - 2),
    ]
    best = None
    for rx, ry, rw, rh in regions:
        if rw < 64 or rh < 64:
            continue
        cw, ch, x0, y0 = _fill_crop((rx, ry, rw, rh), vw, vh, aspect)
        # _fill_crop clamps to the frame; keep it inside the region
        x0 = max(rx, min(rx + rw - cw, x0))
        y0 = max(ry, min(ry + rh - ch, y0))
        if cw > rw or ch > rh:
            continue
        if best is None or ch > best[1]:
            best = (cw, ch, x0, y0)
    if best is None or best[1] < min_h * vh:
        return None
    cw, ch, x0, y0 = best
    return [round(x0 / vw, 5), round(y0 / vh, 5), round((x0 + cw) / vw, 5), round((y0 + ch) / vh, 5)]


def _encode_panel(video_path: Path, out_mp4: Path, filter_complex: str, ss_args: list, keep_audio: bool, what: str):
    tmp = out_mp4.with_name(out_mp4.stem + ".part.mp4")
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        *ss_args,
        "-i",
        str(video_path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
    ]
    if keep_audio:
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p", str(tmp)]
    run_cmd(cmd, what)
    os.replace(tmp, out_mp4)


def render_fill_panel(
    video_path: Path,
    out_dir: Path,
    name: str,
    region_px,
    w: int,
    h: int,
    start_s=None,
    dur_s=None,
    keep_audio: bool = True,
    meta: dict = None,
) -> str:
    """Aspect-correct crop-to-fill of region_px (x, y, w, h) or the full frame into a w x h panel."""
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = meta or probe_video(video_path)
    vw, vh = meta["width"], meta["height"]
    region = region_px or (0, 0, vw, vh)
    cw, ch, x0, y0 = _fill_crop(region, vw, vh, w / h)
    tag = file_tag(video_path, (cw, ch, x0, y0), w, h, start_s, dur_s, keep_audio, "v5")
    out = out_dir / f"{Path(video_path).stem}_{name}_{w}x{h}_{tag}.mp4"
    if not out.is_file():
        fc = f"[0:v]crop={cw}:{ch}:{x0}:{y0},scale={w}:{h}:flags=lanczos,setsar=1[v]"
        _encode_panel(video_path, out, fc, _ss_args(start_s, dur_s), keep_audio, f"{name} panel render")
    return fwd(out)


def render_uncut_stack_panel(video_path: Path, out_dir: Path, w: int, h: int, start_s=None, dur_s=None) -> str:
    """Whole 16:9 frame scaled to the panel width (1080x608) over a blurred, darkened fill of the panel."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = file_tag(video_path, w, h, start_s, dur_s, "stack2")
    out = out_dir / f"{Path(video_path).stem}_uncut_{w}x{h}_{tag}.mp4"
    if not out.is_file():
        fc = (
            f"[0:v]split=2[a][b];"
            f"[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:5,eq=brightness=-0.18[bg];"
            f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos[fg];"
            f"[bg][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2,setsar=1[v]"
        )
        _encode_panel(video_path, out, fc, _ss_args(start_s, dur_s), True, "uncut 16:9 panel render")
    return fwd(out)


# Speaker layout (podcasts): per camera shot, one face -> a 9:16 crop that follows it, two faces side by
# side -> stacked halves (left person on top), no face -> the whole frame over a blurred fill.
SPEAKER_HALF = (1080, 960)
SPEAKER_STACK_FACE = 0.30  # face height as a share of its stacked half
SPEAKER_FACE_Y = 0.42  # face centre at this share of the half's height (head room above)
SPEAKER_MAX_SPEED = 0.04  # a following crop moves at most this share of the source width per second
SPEAKER_MIN_HITS = 0.3  # a face must be found on this share of a shot's samples
SPEAKER_PAIR_HITS = 0.2  # ... the second face of a side-by-side pair on this share (profiles are found less often)
SPEAKER_PAIR_SIZE = 1.4  # a pair has faces at most this many times bigger than each other ...
SPEAKER_PAIR_DY = 0.12  # ... and centres at most this share of the frame height apart
# caption + hook zones on the canvas (y px); "lower" sits under the chin of a big close-up (face down to
# ~1410 px) and above the Shorts/Reels description overlay
SPEAKER_BANDS = {"lower": (1440, 1660), "seam": (840, 1080)}
SPEAKER_LOW_SHARE, SPEAKER_LOW_Y = 0.2, 0.2  # bottom stacked face with low captions: y 1056-1248 px
SPEAKER_MAX_CY = 0.75  # detections centred below this share of the frame height are logos or hands, not faces


def _clean_faces(fl: list) -> list:
    """One sample's detections without logos/hands low in the frame and without a second hit centred
    inside a bigger face (frontal + profile cascades on the same head)."""
    keep = []
    for f in sorted(fl, key=lambda b: -(b[3] - b[1])):
        cx, cy = (f[0] + f[2]) / 2, (f[1] + f[3]) / 2
        if cy > SPEAKER_MAX_CY:
            continue
        if any(k[0] <= cx <= k[2] and k[1] <= cy <= k[3] for k in keep):
            continue
        keep.append(f)
    return keep


def _face_tracks(times: list, faces: list, a: float, b: float, max_dx: float = 0.1) -> tuple:
    """(sample indexes inside [a, b), face clusters [{boxes {k: box}, cx, cy, size}]) for one shot."""
    idx = [k for k, t in enumerate(times) if a <= t < b]
    tracks = []
    for k in idx:
        for f in _clean_faces(faces[k]):
            cx, cy, fh = (f[0] + f[2]) / 2, (f[1] + f[3]) / 2, f[3] - f[1]
            tr = next(
                (
                    t
                    for t in tracks
                    if k not in t["boxes"]
                    and abs(t["cx"] - cx) <= max_dx
                    and abs(t["cy"] - cy) <= 0.15
                    and 0.5 <= fh / t["size"] <= 2.0
                ),
                None,
            )
            if tr is None:
                tr = {"boxes": {}, "cx": cx, "cy": cy, "size": fh}
                tracks.append(tr)
            tr["boxes"][k] = f
            m = len(tr["boxes"])
            tr["cx"] += (cx - tr["cx"]) / m
            tr["cy"] += (cy - tr["cy"]) / m
            tr["size"] += (fh - tr["size"]) / m
    return idx, tracks


def _track_box(tr: dict) -> list:
    """Union-ish box of a face cluster: median edges, so one bad detection does not move it."""
    boxes = list(tr["boxes"].values())

    def med(i):
        v = sorted(b[i] for b in boxes)
        return v[len(v) // 2]

    return [med(0), med(1), med(2), med(3)]


def follow_path(boxes: list, times: list, a: float, vw: int, vh: int, cw: int) -> list:
    """
    [(t, x)] keyframes (t relative to the shot start a) of a full-height crop of width cw px that keeps
    the face (normalised boxes per sample, None = not found) inside: exponential smoothing with a dead zone
    and at most SPEAKER_MAX_SPEED x the source width per second (faces.smooth_track). A path that moves
    < 2% of the width is one keyframe.
    """
    from streamclipper.faces import smooth_track

    known = [(t, b) for t, b in zip(times, boxes, strict=False) if b is not None]
    if not known:
        return [(0.0, (vw - cw) // 2 & ~1)]
    pad = 0.02 * vw
    ts = [t - a for t in times[: len(boxes)]]
    targets, last = [], known[0][1]
    for b in boxes[: len(ts)]:
        last = b or last
        fx0, fx1 = last[0] * vw, last[2] * vw
        x = (fx0 + fx1) / 2 - cw / 2
        lo, hi = fx1 + pad - cw, fx0 - pad  # x range that keeps the face inside
        if lo <= hi:
            x = min(max(x, lo), hi)
        targets.append((min(max(0.0, x), vw - cw) + cw / 2) / vw if b is not None else None)
    if targets[0] is None:  # the shot starts before the face is found: start where it is first seen
        targets[0] = next(c for c in targets if c is not None)
    centres = smooth_track(targets, ts, cw / vw, SPEAKER_MAX_SPEED)
    path = [min(max(0.0, c * vw - cw / 2), vw - cw) for c in centres]
    if max(path) - min(path) < 0.02 * vw:
        mid = sorted(path)[len(path) // 2]
        return [(0.0, int(mid) & ~1)]
    return [(round(max(0.0, t), 3), int(round(x)) & ~1) for t, x in zip(ts, path, strict=True)]


def stack_region(
    face: list, vw: int, vh: int, share: float = SPEAKER_STACK_FACE, face_y: float = SPEAKER_FACE_Y
) -> tuple:
    """Pixel (w, h, x, y) of the source region for one stacked half (1080x960) around a face: the face is
    `share` of the half's height with its centre at `face_y` of it (as far as the frame allows)."""
    aspect = SPEAKER_HALF[0] / SPEAKER_HALF[1]
    fh = (face[3] - face[1]) * vh
    rh = min(vh, fh / share, vw / aspect)
    rw = rh * aspect
    rw, rh = int(round(rw)) & ~1, int(round(rh)) & ~1
    cx, cy = (face[0] + face[2]) / 2 * vw, (face[1] + face[3]) / 2 * vh
    x = int(round(min(max(0.0, cx - rw / 2), vw - rw))) & ~1
    y = int(round(min(max(0.0, cy - face_y * rh), vh - rh))) & ~1
    return rw, rh, x, y


def _out_face(face: list, region: tuple, vw: int, vh: int, top: int, out_w: int, out_h: int) -> list:
    """Face box (normalised source) -> canvas pixels [x0, y0, x1, y1] through a crop region + scale."""
    rw, rh, rx, ry = region
    sx, sy = out_w / rw, out_h / rh
    return [
        round((face[0] * vw - rx) * sx),
        round(top + (face[1] * vh - ry) * sy),
        round((face[2] * vw - rx) * sx),
        round(top + (face[3] * vh - ry) * sy),
    ]


def speaker_shots(track: dict, dur: float, vw: int, vh: int, fps: float, spans: list = None) -> list:
    """
    Shot plan for the speaker layout from vision.track_faces output (times/cuts in the panel file).
    Each shot: {f0, f1 (frame range), mode single|stack|fit, path [(t, x)] for single, regions
    [top, bottom] for stack, faces_out (canvas px boxes), why}. Two stable faces of similar size at
    about the same height that do not fit one 9:16 crop are stacked (left person on top); a pair that
    fits one crop gets a crop centred on both; any other 2+ faces -> the active speaker (faces.pick_speaker:
    mouth motion, a switch only after a 1 s lead and never while nobody talks; spans = [(s, e)] speech in
    panel time, None = always voiced). A switch splits the shot on an exact frame.
    """
    from streamclipper.faces import pick_speaker, voiced_at

    times, faces = track["times"], track["faces"]
    frames = track.get("frames") or []
    n_frames = max(1, int(round(dur * fps)))
    cut_f = sorted({int(round(c * fps)) for c in track.get("cuts") or []})
    edges = [0] + [f for f in cut_f if 0 < f < n_frames] + [n_frames]
    cw = min(vw, int(round(vh * 9 / 16)) & ~1)
    crop_w = cw / vw
    shots, prev = [], None
    for f0, f1 in zip(edges, edges[1:], strict=False):
        if f1 <= f0:
            continue
        a, b = f0 / fps, f1 / fps
        idx, tracks = _face_tracks(times, faces, a, b)
        need = max(1, math.ceil(SPEAKER_MIN_HITS * len(idx)))
        stable = sorted((t for t in tracks if len(t["boxes"]) >= need), key=lambda t: -t["size"])
        if len(stable) == 1:  # a side-by-side partner found on fewer samples (a profile in a wide shot)
            need2 = max(2, math.ceil(SPEAKER_PAIR_HITS * len(idx)))
            A = stable[0]
            partner = [
                t
                for t in tracks
                if t is not A
                and len(t["boxes"]) >= need2
                and max(A["size"], t["size"]) / max(1e-6, min(A["size"], t["size"])) <= SPEAKER_PAIR_SIZE
                and abs(A["cy"] - t["cy"]) <= SPEAKER_PAIR_DY
            ]
            stable += sorted(partner, key=lambda t: -len(t["boxes"]))[:1]
        shot = {"f0": f0, "f1": f1}
        if not idx and prev is not None:  # a flash shorter than one sample: keep the previous framing
            shot.update({k: prev[k] for k in prev if k not in ("f0", "f1")})
            shot["why"] = "short shot, previous framing"
        elif not stable:
            # no face found (B-roll, or a wide shot whose faces are too small): show the whole frame, so
            # nobody is cut in half at a crop edge
            shot.update(mode="fit", faces_out=[], why="no face: whole frame over a blurred fill")
        else:
            chosen = stable[0]
            why = "one face"
            if len(stable) >= 2:
                A, B = stable[0], stable[1]
                ba, bb = _track_box(A), _track_box(B)
                span = max(ba[2], bb[2]) - min(ba[0], bb[0])
                ratio = max(A["size"], B["size"]) / max(1e-6, min(A["size"], B["size"]))
                if span <= crop_w * 0.92:
                    mid = (min(ba[0], bb[0]) + max(ba[2], bb[2])) / 2
                    x = int(round(min(max(0.0, mid * vw - cw / 2), vw - cw))) & ~1
                    region = (cw, vh, x, 0)
                    shot.update(
                        mode="single",
                        path=[(0.0, x)],
                        faces_out=[_out_face(fb, region, vw, vh, 0, 1080, 1920) for fb in (ba, bb)],
                        why="two faces in one crop",
                    )
                elif (
                    ratio <= SPEAKER_PAIR_SIZE
                    and abs(A["cy"] - B["cy"]) <= SPEAKER_PAIR_DY
                    and (ba[2] < bb[0] or bb[2] < ba[0])
                ):
                    left, right = sorted(((A, ba), (B, bb)), key=lambda p: p[0]["cx"])
                    regions = [stack_region(left[1], vw, vh), stack_region(right[1], vw, vh)]
                    hw, hh = SPEAKER_HALF
                    shot.update(
                        mode="stack",
                        pair=[left[1], right[1]],
                        regions=regions,
                        faces_out=[
                            _out_face(left[1], regions[0], vw, vh, 0, hw, hh),
                            _out_face(right[1], regions[1], vw, vh, hh, hw, hh),
                        ],
                        why="two faces side by side: stacked",
                    )
                else:
                    # active speaker per sample (mouth motion while someone talks), with hysteresis
                    ts_idx = [times[k] for k in idx]
                    scores = _mouth_scores(frames, stable, idx, faces, track.get("mouths"))
                    who = pick_speaker(scores, ts_idx, voiced_at(ts_idx, spans) if spans is not None else None)
                    runs = _runs(who)
                    why = f"{len(stable)} faces: active speaker (mouth motion)"
                    if len(runs) > 1:
                        g_prev = f0
                        for r_i, (j, i0, i1) in enumerate(runs):
                            g1 = f1 if r_i == len(runs) - 1 else min(f1, int(round(ts_idx[i1] * fps)))
                            if g1 <= g_prev:
                                continue
                            sub = {"f0": g_prev, "f1": g1}
                            sub.update(_follow_shot(stable[j], idx[i0:i1], times, g_prev / fps, vw, vh, cw, why))
                            shots.append(sub)
                            g_prev = g1
                        prev = shots[-1]
                        continue
                    chosen = stable[runs[0][0]] if runs else stable[0]
            if "mode" not in shot:
                shot.update(_follow_shot(chosen, idx, times, a, vw, vh, cw, why))
        shots.append(shot)
        prev = shot
    return shots


def _follow_shot(tr: dict, idx: list, times: list, a: float, vw: int, vh: int, cw: int, why: str) -> dict:
    """A single-face following crop for the samples idx of one face track."""
    boxes = [tr["boxes"].get(k) for k in idx]
    if all(b is None for b in boxes):
        boxes = [_track_box(tr)] * max(1, len(idx))
        idx = idx or [0]
    path = follow_path(boxes, [times[k] if k < len(times) else a for k in idx], a, vw, vh, cw)
    fb = _track_box(tr)
    x_mid = sorted(x for _, x in path)[len(path) // 2]
    return {
        "mode": "single",
        "path": path,
        "faces_out": [_out_face(fb, (cw, vh, x_mid, 0), vw, vh, 0, 1080, 1920)],
        "why": why,
    }


def active_speaker_boxes(track: dict, a: float, b: float, spans: list = None):
    """
    (sample indexes in [a, b), the active speaker's box per sample) when 2+ stable faces are found there and the
    track has frames; else None (callers then follow the largest face). Same rules as the speaker layout.
    """
    from streamclipper.faces import pick_speaker, voiced_at

    times, faces, frames = track.get("times") or [], track.get("faces") or [], track.get("frames") or []
    idx, tracks = _face_tracks(times, faces, a, b)
    need = max(1, math.ceil(SPEAKER_MIN_HITS * len(idx)))
    stable = [t for t in tracks if len(t["boxes"]) >= need]
    if len(stable) < 2 or not frames:
        return None
    ts = [times[k] for k in idx]
    who = pick_speaker(
        _mouth_scores(frames, stable, idx, faces, track.get("mouths")),
        ts,
        voiced_at(ts, spans) if spans is not None else None,
    )
    return idx, [stable[j]["boxes"].get(k) for j, k in zip(who, idx, strict=False)]


def _runs(who: list) -> list:
    """[(value, i0, i1)] runs of equal values in a list."""
    out = []
    for i, v in enumerate(who):
        if out and out[-1][0] == v:
            out[-1][2] = i + 1
        else:
            out.append([v, i, i + 1])
    return [tuple(r) for r in out]


def _mouth_scores(frames: list, tracks: list, idx: list, faces: list, mouths: list = None) -> list:
    """
    scores[n][j]: mean frame difference in the mouth region of track j between samples idx[n-1] and idx[n]
    (BlazeFace mouth keypoint when known, else the lower 40% of the face box; 0 when the face is not seen).
    """
    np = _np()
    out = []
    for n, k in enumerate(idx):
        row = []
        for tr in tracks:
            b = tr["boxes"].get(k)
            if n == 0 or b is None or k >= len(frames) or idx[n - 1] >= len(frames):
                row.append(0.0)
                continue
            m = None
            if mouths and k < len(mouths) and b in (faces[k] or []):
                pts = mouths[k]
                j = faces[k].index(b)
                m = pts[j] if j < len(pts) else None
            h, w = frames[k].shape[:2]
            x0, y0, x1, y1 = mouth_box(b, m)
            c0, c1 = max(0, int(x0 * w)), min(w, max(int(x0 * w) + 2, int(x1 * w)))
            r0, r1 = max(0, int(y0 * h)), min(h, max(int(y0 * h) + 2, int(y1 * h)))
            cur = frames[k][r0:r1, c0:c1].astype(np.int16)
            old = frames[idx[n - 1]][r0:r1, c0:c1].astype(np.int16)
            row.append(float(np.abs(cur - old).mean()) if cur.size and cur.shape == old.shape else 0.0)
        out.append(row)
    return out


def speaker_caption_zone(shots: list, bands: dict = None) -> str:
    """
    The caption zone (a key of bands, default SPEAKER_BANDS: 'lower' or 'seam') that covers a face for the
    shortest time; ties -> the first zone. shots: [{f0, f1, faces_out: [[x0, y0, x1, y1] canvas px]}].
    """
    bands = bands or SPEAKER_BANDS
    cover = {}
    for zone, (z0, z1) in bands.items():
        cover[zone] = sum(
            s["f1"] - s["f0"] for s in shots if any(f[1] < z1 and z0 < f[3] for f in s.get("faces_out") or [])
        )
    return min(cover, key=lambda z: cover[z])


# Caption zones of the 9:16 full layout (y px on the canvas) and their ASS anchors (align, y): the default low
# zone, the upper middle and the top. A face-centred crop (full-cam, or the corner-cam fallback when the game
# screen is black) often has the face in the low zone; the captions then go to the zone the face is not in.
FULL_BANDS = {"lower": (1230, 1500), "upper": (560, 830), "top": (260, 530)}
FULL_ANCHORS = {"lower": (2, 1500), "upper": (2, 830), "top": (8, 260)}
FULL_MIN_FACE = 0.06  # detections smaller than this share of the canvas height are not the streamer's face


def face_shots(faces: list, W: int = 1080, H: int = 1920) -> list:
    """
    Per-sample face lists (normalised boxes of a canvas-size panel, see vision.detect_faces) -> one-frame
    pseudo shots for speaker_caption_zone, faces as canvas px boxes padded for the hair (20% of the face
    height) and the chin (15%).
    """
    shots = []
    for k, fl in enumerate(faces):
        out = []
        for f in fl:
            h = f[3] - f[1]
            if h >= FULL_MIN_FACE:
                out.append([f[0] * W, (f[1] - 0.2 * h) * H, f[2] * W, (f[3] + 0.15 * h) * H])
        shots.append({"f0": k, "f1": k + 1, "faces_out": out})
    return shots


def full_caption_zone(panel: Path, dur: float, samples: int = 8) -> tuple:
    """
    (zone, why) for a 1080x1920 full-layout panel: the FULL_BANDS zone that covers a face on the fewest of
    `samples` frames (speaker_caption_zone; ties -> the default 'lower'). 'lower' without OpenCV or when a
    face shows on fewer than 2 frames (a stray detection in the game scenery).
    """
    from streamclipper.vision import detect_faces

    times = [max(0.1, dur) * (k + 0.5) / samples for k in range(samples)]
    faces = detect_faces(panel, times, 1080, 1920)
    if faces is None:
        return "lower", "OpenCV not installed"
    shots = face_shots(faces)
    hits = sum(1 for s in shots if s["faces_out"])
    if hits < 2:
        return "lower", f"a face on {hits}/{samples} frames"
    return speaker_caption_zone(shots, FULL_BANDS), f"a face on {hits}/{samples} frames"


def apply_caption_anchor(lay: dict, anchor) -> dict:
    """The face-aware caption anchor (align, y) chosen at panel time replaces the layout default; the hook
    banner, which shares the caption zone in the full layout, moves with it."""
    if anchor:
        a, y = int(anchor[0]), int(anchor[1])
        lay["caption"] = {"align": a, "y": y}
        lay["hook"] = dict(lay.get("hook") or {}, align=a, y=y)
    return lay


def black_frames(video_path: Path) -> list:
    """[(a, b)] seconds of black picture in a panel file (the export gate's blackdetect, pix_th=0.12,
    on a 480 px copy with d=0.04 so nothing the gate would find is missed)."""
    res = run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "info",
            "-i",
            str(video_path),
            "-an",
            "-vf",
            "scale=480:-2,blackdetect=d=0.04:pix_th=0.12",
            "-f",
            "null",
            "-",
        ],
        "black frame scan",
    )
    return [
        (float(a), float(b))
        for a, b in re.findall(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)", res.stderr or "")
    ]


def cut_black(pieces: list, spans: list, words: list, pad: float = 1 / 30) -> list:
    """
    Pieces (cut_start/cut_end in source time) with black spans (source time) cut out, when no word is
    spoken in the span (a B-roll fade between sentences); a span with speech in it stays (the export
    gate reports it). Parts shorter than 0.2 s are dropped.
    """
    out = []
    for pc in pieces:
        parts = [(pc["cut_start"], pc["cut_end"])]
        for a, b in spans:
            a, b = a - pad, b + pad
            if any(w["e"] > a and w["s"] < b for w in words):
                continue
            nxt = []
            for s, e in parts:
                if b <= s or e <= a:
                    nxt.append((s, e))
                    continue
                nxt += [x for x in ((s, a), (b, e)) if x[1] - x[0] >= 0.2]
            parts = nxt
        for s, e in parts:
            out.append(dict(pc, cut_start=s, cut_end=e, words=[w for w in pc["words"] if w["e"] > s and w["s"] < e]))
    return out


def _path_expr(path: list) -> str:
    """Piecewise-linear crop x(t) for ffmpeg (t from the shot start), at most one keyframe per second."""
    keys = [path[0]]
    for t, x in path[1:]:
        if t - keys[-1][0] >= 1.0:
            keys.append((t, x))
    if keys[-1] != path[-1]:
        keys.append(path[-1])
    if len(keys) == 1:
        return str(keys[0][1])
    expr = str(keys[-1][1])
    for (t0, x0), (t1, x1) in reversed(list(zip(keys, keys[1:], strict=False))):
        span = max(1e-3, t1 - t0)
        expr = f"if(lt(t,{t1:.3f}),{x0}+({x1 - x0})*(t-{t0:.3f})/{span:.3f},{expr})"
    return f"if(lt(t,{keys[0][0]:.3f}),{keys[0][1]},{expr})"


def render_speaker_panel(video_path: Path, out_dir: Path, meta: dict, opts: dict) -> dict:
    """
    1080x1920 speaker-layout panel of the whole window file + {shots, zone, note}. Without OpenCV:
    the whole frame over a blurred fill (one notice). Shots are cut on exact frame numbers, so audio stays in sync.
    """
    from streamclipper.vision import track_faces

    if opts.get("ratio") != "9:16":
        raise ToolError("The speaker layout supports 9:16 only.", "use --ratio 9:16 or another preset")
    out_dir.mkdir(parents=True, exist_ok=True)
    vw, vh, fps, dur = meta["width"], meta["height"], float(meta["fps"] or 30.0), float(meta["dur"])
    track = track_faces(video_path, 0.0, dur, 0.5, vw, vh)
    if track is None:
        print("Speaker layout: OpenCV is not installed, so clips show the whole frame (pip install -e .[vision]).")
        track = {"times": [], "faces": [], "cuts": []}
    shots = speaker_shots(track, dur, vw, vh, fps, opts.get("speech_spans"))
    zone = speaker_caption_zone(shots)
    if zone == "lower":
        # low captions: the bottom person's head goes to the top of the lower half, the caption sits on the chest
        for s in shots:
            if s["mode"] == "stack":
                s["regions"][1] = stack_region(s["pair"][1], vw, vh, SPEAKER_LOW_SHARE, SPEAKER_LOW_Y)
                s["faces_out"][1] = _out_face(s["pair"][1], s["regions"][1], vw, vh, SPEAKER_HALF[1], *SPEAKER_HALF)
    cw = min(vw, int(round(vh * 9 / 16)) & ~1)
    hw, hh = SPEAKER_HALF
    parts, labels = [], []
    for i, s in enumerate(shots):
        src = f"[s{i}]trim=start_frame={s['f0']}:end_frame={s['f1']},setpts=PTS-STARTPTS"
        if s["mode"] == "stack":
            (w0, h0, x0, y0), (w1, h1, x1, y1) = s["regions"]
            parts.append(
                f"{src},split=2[t{i}][b{i}];"
                f"[t{i}]crop={w0}:{h0}:{x0}:{y0},scale={hw}:{hh}:flags=lanczos,setsar=1[tt{i}];"
                f"[b{i}]crop={w1}:{h1}:{x1}:{y1},scale={hw}:{hh}:flags=lanczos,setsar=1[bb{i}];"
                f"[tt{i}][bb{i}]vstack=2[v{i}]"
            )
        elif s["mode"] == "fit":
            parts.append(
                f"{src},split=2[fa{i}][fb{i}];"
                f"[fa{i}]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                f"scale=270:480,gblur=sigma=8,scale=1080:1920:flags=bicubic,eq=brightness=-0.08[bg{i}];"
                f"[fb{i}]scale=1080:-2:flags=lanczos[fg{i}];"
                f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1[v{i}]"
            )
        else:
            parts.append(
                f"{src},crop={cw}:{vh}:x='{_path_expr(s['path'])}':y=0,scale=1080:1920:flags=lanczos,setsar=1[v{i}]"
            )
        labels.append(f"[v{i}]")
    n = len(shots)
    fc = f"[0:v]split={n}" + "".join(f"[s{i}]" for i in range(n)) + ";" if n > 1 else "[0:v]null[s0];"
    fc += ";".join(parts) + ";" + "".join(labels) + f"concat=n={n}:v=1:a=0,format=yuv420p[v]"
    plan_key = [(s["f0"], s["f1"], s["mode"], s.get("path"), s.get("regions")) for s in shots]
    tag = file_tag(video_path, json.dumps(plan_key, default=str), "spk4")
    out = out_dir / f"{Path(video_path).stem}_speaker_1080x1920_{tag}.mp4"
    if not out.is_file():
        _encode_panel(video_path, out, fc, [], True, "speaker panel render")
    counts = {m: sum(1 for s in shots if s["mode"] == m) for m in ("stack", "single", "fit")}
    note = (
        f"speaker: {n} shot(s) ({counts['stack']} stacked, {counts['single']} face crop, {counts['fit']} whole frame), "
        f"captions {'on the seam' if zone == 'seam' else 'low'}"
    )
    return {"src": fwd(out), "shots": shots, "zone": zone, "note": note}


def render_reframe_panel(
    video_path: Path, out_dir: Path, meta: dict, segs: list, ws: float, we: float, ratio: str, anchor=None
) -> dict:
    """
    Reframe editor panel: one full-canvas composite of [ws, we] with a layout and crop window(s) per segment and
    panned windows (reframe.build_panel_graph), source audio kept. Returns a panels dict like the 'full' branch of
    render_layout_panels; anchor [align, y] (None = the first segment's layout default) places the captions.
    """
    from streamclipper import reframe

    out_dir.mkdir(parents=True, exist_ok=True)
    W, H = reframe.canvas(ratio)
    fc = reframe.build_panel_graph(segs, ws, we, ratio, meta["width"], meta["height"])
    tag = file_tag(video_path, json.dumps(segs, sort_keys=True), round(ws, 3), round(we, 3), ratio, "rf1")
    out = out_dir / f"{Path(video_path).stem}_reframe_{W}x{H}_{tag}.mp4"
    if not out.is_file():
        _encode_panel(video_path, out, fc, _ss_args(ws, we - ws), True, "reframe panel render")
    anchor = list(anchor) if anchor else reframe.auto_anchor(segs[0]["layout"], ratio)
    caption_y, banner_y = -0.45, 0.55
    if anchor:
        caption_y = capcut_y(anchor[0], anchor[1], H)
        banner_y = caption_y if anchor[1] < FULL_BANDS["upper"][0] else banner_y
    keys = sum(len(s.get("keys") or []) for s in segs)
    return {
        "layout": "full",
        "main_src": fwd(out),
        "main_pos_y": 0.0,
        "overlay_src": None,
        "overlay_pos_y": 0.0,
        "overlay_loop": False,
        "overlay_dur": None,
        "caption_y": caption_y,
        "banner_y": banner_y,
        "caption_anchor": anchor,
        "caption_note": f"reframe: {len(segs)} segment(s), {keys} keyframe(s)",
    }


def with_caption_anchor(panels: dict, anchor, ratio: str) -> dict:
    """Panels of any layout with the editor's caption anchor [align, y] (canvas px) instead of the layout's own."""
    if not anchor:
        return panels
    H = 1920 if ratio == "9:16" else 1080
    out = dict(panels, caption_anchor=[int(anchor[0]), int(anchor[1])])
    out["caption_y"] = capcut_y(int(anchor[0]), int(anchor[1]), H)
    return out


def render_layout_panels(
    video_path: Path, meta: dict, opts: dict, cam_box: list, game_box: list, start_s: float, dur_s: float, out_dir: Path
) -> dict:
    """
    Render the panels one clip needs for the chosen layout and return where they sit on the canvas.
    Keys: layout, main_src, main_pos_y, overlay_src, overlay_pos_y, overlay_loop, overlay_dur,
    caption_y, banner_y. CapCut y is +up, normalised to half the canvas height.
    """
    layout = opts["layout"]
    ratio = opts["ratio"]
    vw, vh = meta["width"], meta["height"]
    game_px = _norm_to_px(game_box, vw, vh) if game_box else None
    if layout == "speaker":
        sp = render_speaker_panel(video_path, out_dir, meta, opts)
        name = "speaker-seam" if sp["zone"] == "seam" else "speaker"
        lay = layout_boxes(ratio, name)
        cap, hook = lay["caption"], lay["hook"]
        return {
            "layout": name,
            "main_src": sp["src"],
            "main_pos_y": 0.0,
            "overlay_src": None,
            "overlay_pos_y": 0.0,
            "overlay_loop": False,
            "overlay_dur": None,
            "caption_y": capcut_y(cap["align"], cap["y"], 1920),
            "banner_y": capcut_y(hook["align"], hook["y"], 1920),
            "speaker_note": sp["note"],
            "speaker_shots": [
                {"f0": s["f0"], "f1": s["f1"], "mode": s["mode"], "why": s["why"], "faces_out": s["faces_out"]}
                for s in sp["shots"]
            ],
        }
    if layout in ("split", "uncut-stack"):
        stack = layout == "uncut-stack"
        rp = render_split_panels(
            video_path,
            out_dir,
            cam_box,
            game_box,
            ratio=ratio,
            start_s=start_s,
            dur_s=dur_s,
            meta=meta,
            face_only=stack,
        )
        main_src = rp["gameplay_mp4"]
        if stack:
            main_src = render_uncut_stack_panel(video_path, out_dir, rp["bot_w"], rp["bot_h"], start_s, dur_s)
        return {
            "layout": layout,
            "main_src": main_src,
            "main_pos_y": rp["bot_pos_y"],
            "overlay_src": rp["facecam_mp4"],
            "overlay_pos_y": rp["top_pos_y"],
            "overlay_loop": False,
            "overlay_dur": None,
            "caption_y": rp["seam_caption_y"],
            "banner_y": round(rp["seam_caption_y"] - 0.22, 3),
        }
    if layout == "hook-grabber":
        if ratio != "9:16":
            raise ToolError("The hook-grabber layout supports 9:16 only.", "use --ratio 9:16 or another preset")
        filler = opts.get("filler_video")
        if not filler:
            raise ToolError(
                "The hook-grabber layout needs a filler video for the bottom panel.",
                "pass --filler-video <path to a satisfying/gameplay loop>",
            )
        top_h = int(round(1920 * float(opts.get("main_ratio") or 0.55))) & ~1
        bot_h = 1920 - top_h
        main_src = render_fill_panel(
            video_path, out_dir, "main", game_px, 1080, top_h, start_s, dur_s, keep_audio=True, meta=meta
        )
        fmeta = probe_video(Path(filler))
        f_dur = min(fmeta["dur"], 60.0)
        filler_src = render_fill_panel(
            Path(filler), out_dir, "filler", None, 1080, bot_h, 0.0, f_dur, keep_audio=False, meta=fmeta
        )
        return {
            "layout": layout,
            "main_src": main_src,
            "main_pos_y": round((960 - top_h / 2) / 960, 4),
            "overlay_src": filler_src,
            "overlay_pos_y": round((960 - (top_h + bot_h / 2)) / 960, 4),
            "overlay_loop": True,
            "overlay_dur": probe_video(Path(filler_src))["dur"],
            "caption_y": -0.08,
            "banner_y": 0.70,  # captions on the 1056 px seam
        }
    if layout == "square-frame":
        lay = layout_boxes(ratio, layout)  # raises for 1:1
        cam_px = _norm_to_px(cam_box, vw, vh) if cam_box else None
        main_src = render_square_frame_panel(video_path, out_dir, game_px, cam_px, start_s, dur_s, meta)
        return {
            "layout": layout,
            "main_src": main_src,
            "main_pos_y": 0.0,
            "overlay_src": None,
            "overlay_pos_y": 0.0,
            "overlay_loop": False,
            "overlay_dur": None,
            "caption_y": capcut_y(lay["caption"]["align"], lay["caption"]["y"], 1920),
            "banner_y": capcut_y(5, SQUARE_FRAME["hook_y"], 1920),
        }
    # full: one aspect-correct centre crop filling the canvas (caption-punch)
    w, h = (1080, 1920) if ratio == "9:16" else (1080, 1080)
    main_src = render_fill_panel(video_path, out_dir, "full", game_px, w, h, start_s, dur_s, keep_audio=True, meta=meta)
    caption_y, banner_y, anchor, note = -0.45, 0.55, None, ""
    if ratio == "9:16":
        # a face-centred crop puts the face low in the frame: captions go where no face is
        zone, why = full_caption_zone(Path(main_src), probe_video(Path(main_src))["dur"])
        if zone != "lower":
            anchor = list(FULL_ANCHORS[zone])
            caption_y = capcut_y(anchor[0], anchor[1], 1920)
            banner_y = caption_y if zone == "top" else banner_y
            note = f"captions: {zone} zone (the face covers the low zone; {why})"
    return {
        "layout": "full",
        "main_src": main_src,
        "main_pos_y": 0.0,
        "overlay_src": None,
        "overlay_pos_y": 0.0,
        "overlay_loop": False,
        "overlay_dur": None,
        "caption_y": caption_y,
        "banner_y": banner_y,
        "caption_anchor": anchor,
        "caption_note": note,
    }


def sample_exposure(video_path: Path, dur: float, step_s: float = 0.5):
    samples = []
    w, h = 108, 192
    fps_rate = 1.0 / step_s
    res = run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vf",
            f"fps={fps_rate},scale={w}:{h},format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        "exposure scan",
        binary=True,
    )
    frame_bytes = w * h
    raw = res.stdout
    n_frames = len(raw) // frame_bytes
    prev_fb = None
    for idx in range(n_frames):
        fb = raw[idx * frame_bytes : (idx + 1) * frame_bytes]
        mean_val = sum(fb) / frame_bytes
        hi240 = sum(1 for p in fb if p > 240) * 100.0 / frame_bytes
        motion = 0.0
        if prev_fb is not None:
            motion = sum(abs(a - b) for a, b in zip(fb, prev_fb, strict=False)) / frame_bytes
        prev_fb = fb
        samples.append(
            {
                "t": round(idx * step_s, 2),
                "mean": round(mean_val, 1),
                "hi240": round(hi240, 2),
                "motion": round(motion, 2),
            }
        )
    return samples


def make_crop_dict(crop_box):
    if not crop_box or len(crop_box) != 4:
        return {
            "upper_left_x": 0.0,
            "upper_left_y": 0.0,
            "upper_right_x": 1.0,
            "upper_right_y": 0.0,
            "lower_left_x": 0.0,
            "lower_left_y": 1.0,
            "lower_right_x": 1.0,
            "lower_right_y": 1.0,
        }
    x0, y0, x1, y1 = [float(v) for v in crop_box]
    return {
        "upper_left_x": x0,
        "upper_left_y": y0,
        "upper_right_x": x1,
        "upper_right_y": y0,
        "lower_left_x": x0,
        "lower_left_y": y1,
        "lower_right_x": x1,
        "lower_right_y": y1,
    }


# Square frame (9:16): blurred full-bleed gameplay, the 6:5 gameplay frame 1080x900 at y=470,
# hook banner in y 150-430 above it and captions from y=1400 below it.
SQUARE_FRAME = {"y": 470, "h": 900, "hook_y": 290, "caption_y": 1400}


def layout_boxes(ratio: str, layout: str, main_h: int = None) -> dict:
    """
    Output rects (x, y, w, h) on the canvas, the default caption anchor, the anchors of the
    --caption-position choices (align, y) and the hook banner anchor; same geometry as the CapCut draft.
    """
    W, H = (1080, 1920) if ratio == "9:16" else (1080, 1080)
    tall = ratio == "9:16"
    if layout in ("split", "uncut-stack"):
        cam_h = 672 if tall else 454
        rects = {"cam": [0, 0, W, cam_h], "main": [0, cam_h, W, H - cam_h]}
        cap = {"align": 8, "y": cam_h + 28}
        pos = {"top": (8, cam_h + 28), "center": (5, cam_h + (H - cam_h) // 2), "lower": (2, H - (260 if tall else 60))}
        hook = {"align": 8, "y": cam_h + 28, "bar_top": cam_h}
    elif layout == "hook-grabber":
        top_h = int(main_h or 1056)
        rects = {"main": [0, 0, W, top_h], "filler": [0, top_h, W, H - top_h]}
        cap = {"align": 2, "y": top_h}
        pos = {"top": (8, 160), "center": (5, top_h // 2), "lower": (2, H - 260)}
        hook = {"align": 2, "y": top_h, "bar_top": 160}
    elif layout == "square-frame":
        if not tall:
            raise ToolError("The square-frame layout supports 9:16 only.", "use --ratio 9:16 or another preset")
        fy, fh = SQUARE_FRAME["y"], SQUARE_FRAME["h"]
        rects = {"main": [0, 0, W, H], "frame": [0, fy, W, fh]}
        cap = {"align": 8, "y": SQUARE_FRAME["caption_y"]}
        pos = {"top": (8, fy + 24), "center": (5, fy + fh // 2), "lower": (8, SQUARE_FRAME["caption_y"])}
        hook = {"align": 5, "y": SQUARE_FRAME["hook_y"], "bar_top": None}
    elif layout in ("speaker", "speaker-seam"):
        # captions and the hook share one zone that the shot plan keeps clear of faces (SPEAKER_BANDS)
        z0, z1 = SPEAKER_BANDS["seam" if layout == "speaker-seam" else "lower"]
        rects = {"main": [0, 0, W, H]}
        cap = {"align": 5, "y": (z0 + z1) // 2} if layout == "speaker-seam" else {"align": 2, "y": z1}
        pos = {"top": (8, 260), "center": (5, H // 2), "lower": (2, 1500)}
        hook = {"align": cap["align"], "y": cap["y"], "bar_top": z0}
    else:
        rects = {"main": [0, 0, W, H]}
        cap = {"align": 2, "y": 1500 if tall else 900}
        pos = {"top": (8, 260 if tall else 120), "center": (5, H // 2), "lower": (2, 1500 if tall else 900)}
        hook = {"align": 2, "y": cap["y"], "bar_top": 160 if tall else 60}
    return {"canvas": [W, H], "rects": rects, "caption": cap, "positions": pos, "hook": hook}


def caption_anchor(lay: dict, position: str = "auto", anim_up: int = 0) -> tuple:
    """
    (align, y) for --caption-position top|center|lower|auto. With a cam panel, a top-anchored caption
    starts at least 20 px + the style's upward animation offset below the cam panel, so it never
    covers the face.
    """
    if position in (lay.get("positions") or {}):
        align, y = lay["positions"][position]
    else:
        align, y = lay["caption"]["align"], lay["caption"]["y"]
    cam = (lay.get("rects") or {}).get("cam")
    if cam and align == 8:
        y = max(y, cam[1] + cam[3] + 20 + int(anim_up))
    return align, int(y)


def capcut_y(align: int, y: float, H: int, h: float = 105.0) -> float:
    """ASS anchor (align, y px) -> CapCut text centre y (+up, normalised to half the canvas height)."""
    centre = y + h / 2 if align == 8 else (y if align == 5 else y - h / 2)
    return round((H / 2 - centre) / (H / 2), 4)


def frame_crop(vw: int, vh: int, region, cam_px, aspect: float) -> tuple:
    """
    Largest crop with the aspect centred in region; shifted sideways so it never cuts through the
    webcam box (it either leaves the cam out or, when that is impossible, takes it in whole).
    Returns even (w, h, x, y).
    """
    cw, ch, x0, y0 = _fill_crop(region or (0, 0, vw, vh), vw, vh, aspect)
    if cam_px:
        cx, cy, cwid, chei = cam_px
        cx1, cy1 = cx + cwid, cy + chei
        overlaps = x0 < cx1 and cx < x0 + cw and y0 < cy1 and cy < y0 + ch
        inside = x0 <= cx and cx1 <= x0 + cw and y0 <= cy and cy1 <= y0 + ch
        if overlaps and not inside:
            right = (cx1 + 1) & ~1  # move right of a left-side cam
            left = (cx - cw) & ~1  # move left of a right-side cam
            if cx < vw / 2 and right + cw <= vw:
                x0 = right
            elif cx >= vw / 2 and left >= 0:
                x0 = left
            elif cw >= cwid:  # take the whole cam in
                x0 = max(0, min(vw - cw, cx if cx < vw / 2 else cx1 - cw)) & ~1
    return cw, ch, x0, y0


def render_square_frame_panel(
    video_path: Path, out_dir: Path, game_px, cam_px, start_s=None, dur_s=None, meta: dict = None
) -> str:
    """1080x1920 square-frame panel: blurred full-bleed copy (no darkening) + the 6:5 frame at y=470."""
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = meta or probe_video(video_path)
    vw, vh = meta["width"], meta["height"]
    fw, fh, fy = 1080, SQUARE_FRAME["h"], SQUARE_FRAME["y"]
    cw, ch, x0, y0 = frame_crop(vw, vh, game_px, cam_px, fw / fh)
    tag = file_tag(video_path, (cw, ch, x0, y0), start_s, dur_s, "sqf1")
    out = out_dir / f"{Path(video_path).stem}_squareframe_1080x1920_{tag}.mp4"
    if not out.is_file():
        fc = (
            "[0:v]split=2[a][b];"
            "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
            "scale=270:480,gblur=sigma=8,scale=1080:1920:flags=bicubic[bg];"
            f"[b]crop={cw}:{ch}:{x0}:{y0},scale={fw}:{fh}:flags=lanczos[fg];"
            f"[bg][fg]overlay=0:{fy},setsar=1[v]"
        )
        _encode_panel(video_path, out, fc, _ss_args(start_s, dur_s), True, "square-frame panel render")
    return fwd(out)
