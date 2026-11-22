"""Export gate for rendered MP4s."""

import json
import re
from pathlib import Path

from streamclipper.audio import _np
from streamclipper.captions.coverage import coverage_row
from streamclipper.render import ebur128_summary, mp4_faststart, render_plan_mp4
from streamclipper.state import write_json_atomic
from streamclipper.thumbs import make_thumbnail
from streamclipper.util import ToolError, fwd, run_cmd
from streamclipper.vision import _iou, check_face_box, detect_faces


def check_face_panel(src: Path, offset: float, dur: float, box) -> tuple:
    """
    (status, detail) from 5 frames of the source webcam box: skin mask r>85, g>40, b>20, r-g>12, r>b.
    FAIL when the skin bounding box touches >= 2 edges of the box or its centroid is outside the
    inner 80% (in most samples); WARN when < 2% of the pixels are skin.
    """
    np = _np()
    x, y, w, h = [int(v) for v in box]
    sw = 160
    sh = max(2, int(round(sw * h / w / 2)) * 2)
    good = bad = none = 0
    worst = ""
    for k in range(5):
        t = offset + max(0.1, dur) * (k + 0.5) / 5
        res = run_cmd(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{t:.3f}",
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-vf",
                f"crop={w}:{h}:{x}:{y},scale={sw}:{sh}",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            "face panel sample",
            binary=True,
        )
        buf = res.stdout or b""
        if len(buf) < sw * sh * 3:
            continue
        px = np.frombuffer(buf[: sw * sh * 3], np.uint8).reshape(sh, sw, 3).astype(np.int16)
        r, g, b = px[..., 0], px[..., 1], px[..., 2]
        mask = (r > 85) & (g > 40) & (b > 20) & (r - g > 12) & (r > b)
        if mask.mean() < 0.02:
            none += 1
            continue
        ys, xs = np.nonzero(mask)
        edges = int(xs.min() <= 0) + int(ys.min() <= 0) + int(xs.max() >= sw - 1) + int(ys.max() >= sh - 1)
        cx, cy = xs.mean() / sw, ys.mean() / sh
        if edges >= 2 or not (0.1 <= cx <= 0.9 and 0.1 <= cy <= 0.9):
            bad += 1
            worst = f"t={t:.1f}s touches {edges} edges, centroid ({cx:.2f}, {cy:.2f})"
        else:
            good += 1
    if good + bad == 0:
        return "WARN", f"no skin found in {none} samples of cam box {x},{y},{w},{h}"
    if bad * 2 >= good + bad:
        return "FAIL", f"face looks cut by the cam box in {bad}/{good + bad} samples ({worst}); set --cam-crop"
    return "PASS", f"skin centred in {good}/{good + bad} samples (cam box {x},{y},{w},{h})"


def gate_mp4(path: Path, timeline_json: Path = None) -> list:
    """Export gate for one MP4: list of (check, PASS/FAIL/WARN/N/A, detail)."""
    path = Path(path).resolve()
    if not path.is_file():
        raise ToolError(f"MP4 not found: {fwd(path)}", "check the --mp4 path")
    rows = []

    def row(check, ok, detail, status=None):
        rows.append((check, status or ("PASS" if ok else "FAIL"), detail))

    res = run_cmd(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        f"ffprobe {path.name}",
    )
    info = json.loads(res.stdout)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = info.get("format", {})
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    row("resolution", (w, h) in ((1080, 1920), (1080, 1080)), f"{w}x{h}")
    row(
        "fps",
        v.get("r_frame_rate") == "30/1" and v.get("avg_frame_rate") in ("30/1", None),
        f"r={v.get('r_frame_rate')} avg={v.get('avg_frame_rate')}",
    )
    row(
        "video_codec",
        v.get("codec_name") == "h264" and v.get("profile") == "High",
        f"{v.get('codec_name')} {v.get('profile')}",
    )
    row("pix_fmt", v.get("pix_fmt") == "yuv420p", str(v.get("pix_fmt")))
    row(
        "audio",
        a.get("codec_name") == "aac" and str(a.get("sample_rate")) == "48000",
        f"{a.get('codec_name')} {a.get('sample_rate')} Hz" if a else "no audio stream",
    )
    br = float(fmt.get("bit_rate") or 0) / 1e6
    row("bitrate", 0 < br <= 12.5, f"{br:.2f} Mbps (max 12.5)")
    ok, detail = mp4_faststart(path)
    row("faststart", ok, detail)

    res = run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "info",
            "-i",
            str(path),
            "-vf",
            "blackdetect=d=0.1:pix_th=0.12",
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        f"loudness/black scan of {path.name}",
    )
    err = res.stderr or ""
    li, tp = ebur128_summary(err)
    row("loudness", li is not None and abs(li + 11.0) <= 1.0, f"I={li} LUFS (target -11 +/- 1)")
    row("true_peak", tp is not None and tp <= -1.0, f"TP={tp} dBTP (max -1.0)")
    blacks = re.findall(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)", err)
    row(
        "blackdetect",
        not blacks and "black_start" not in err,
        "no black frames (d=0.1, pix_th=0.12)"
        if not blacks
        else "black at " + ", ".join(f"{float(s):.2f}-{float(e):.2f}s" for s, e in blacks[:5]),
    )

    tl_path = Path(timeline_json) if timeline_json else path.with_name(f"{path.stem}.timeline.json")
    tl = json.loads(tl_path.read_text(encoding="utf-8")) if tl_path.is_file() else None
    dur = float(fmt.get("duration") or 0)
    if tl is None:
        row("duration", True, f"{dur:.3f}s; no timeline sidecar", "WARN")
        row("captions_clear_of_face", True, "no timeline sidecar", "WARN")
        row("face_panel", True, "no timeline sidecar", "WARN")
        return rows
    exp = float(tl["expected_duration"])
    row("duration", abs(dur - exp) <= 0.1, f"{dur:.3f}s vs timeline {exp:.3f}s (max diff 0.1)")
    st, detail = coverage_row(tl.get("caption") or {})
    row("caption_coverage", st != "FAIL", detail, st)
    st, detail = check_captions_off_faces(path, tl) or ("N/A", "no face detector installed")
    row("captions_off_face", st != "FAIL", detail, st)
    if str(tl.get("layout") or "").startswith("speaker"):
        st, detail = check_speaker_faces(path, dur) or ("N/A", "no face detector installed")
        row("faces_whole", st != "FAIL", detail, st)
    cam = (tl.get("rects") or {}).get("cam")
    if not cam:
        row("captions_clear_of_face", True, f"layout {tl.get('layout')} has no cam panel", "N/A")
        row("face_panel", True, f"layout {tl.get('layout')} has no cam panel", "N/A")
        return rows
    cam_bottom = cam[1] + cam[3]
    top = (tl.get("caption") or {}).get("top_min")
    if top is None:
        row("captions_clear_of_face", True, "no captions", "N/A")
    else:
        row("captions_clear_of_face", top >= cam_bottom, f"caption top y={top} vs cam panel bottom {cam_bottom}")
    cs = tl.get("cam_source") or {}
    box = tl.get("cam_box_px")
    if not box or not cs.get("path") or not Path(cs["path"]).is_file():
        row("face_panel", True, "cam box or source window not available", "WARN")
    else:
        args = (Path(cs["path"]), float(cs.get("offset", 0.0)), float(cs.get("dur", 0.0)), box)
        # face detection when OpenCV is installed; the skin-colour heuristic otherwise (it also counts
        # brown game terrain as skin, so a cut-out cam over gameplay can fail it while the face is fine)
        st, detail = check_face_box(*args) or check_face_panel(*args)
        row("face_panel", st == "PASS", detail, st)
    game, person = tl.get("game_box_px"), tl.get("person_box_px")
    if game and person:
        gx, gy, gw, gh = game
        qx, qy, qw, qh = person
        overlap = gx < qx + qw and qx < gx + gw and gy < qy + qh and qy < gy + gh
        row(
            "streamer_clear",
            not overlap,
            f"gameplay crop {gx},{gy},{gw},{gh} vs streamer {qx},{qy},{qw},{qh}"
            + (" overlap: the streamer shows in the gameplay panel; set --game-crop" if overlap else ""),
        )
    else:
        row("streamer_clear", True, "no streamer box recorded", "N/A")
    main = (tl.get("rects") or {}).get("main")
    if main:
        st, detail = check_panel_black(path, main)
        row("gameplay_black", st == "PASS", detail, st)
        st, detail = check_panel_faces(path, main, dur) or ("N/A", "no face detector installed")
        row("gameplay_no_face", st != "FAIL", detail, st)
    return rows


CAPTION_FACE_MIN_H = 0.08  # faces smaller than this share of the canvas height (154 px) are stray detections
CAPTION_FACE_REL_H = 0.5  # ... and so are faces under half the height of the biggest face in the same frame


def caption_boxes(tl: dict) -> list:
    """
    [s, e, x0, y0, x1, y1] (timeline s, canvas px) of every word caption and hook banner, from the sidecar's
    caption spans. Older sidecars without spans: one full-width band around the caption anchor (top_min
    down to an align-2 anchor) for the whole clip.
    """
    cap = tl.get("caption") or {}
    if cap.get("spans") is not None:
        return [[float(v) for v in s] for s in cap["spans"]]
    top = cap.get("top_min")
    if top is None:
        return []
    W = float((tl.get("canvas") or [1080, 1920])[0])
    align, y = int(cap.get("align", 2)), float(cap.get("y", top))
    y0, y1 = (float(top), y) if align == 2 else ((y - 75.0, y + 75.0) if align == 5 else (y, y + 150.0))
    return [[0.0, float(tl.get("expected_duration") or 0.0), 0.0, y0, W, y1]]


def _panel_sources(mp4: Path, tl: dict) -> list:
    """[(panel file, canvas rect, zoomed)]: the panel that shows the streamer's face, in sync with the timeline."""
    pn = tl.get("panels")
    if pn is None:  # sidecars written before v5.0.1: the clip plan next to the MP4 has the panel paths
        plan_p = mp4.with_name(f"{mp4.stem}.clip.json")
        if not plan_p.is_file():
            return []
        pp = json.loads(plan_p.read_text(encoding="utf-8")).get("panels") or {}
        pn = {"main": pp.get("main_src"), "overlay": None if pp.get("overlay_loop") else pp.get("overlay_src")}
    rects = tl.get("rects") or {}
    # with a cam panel the streamer's face is there (a face in the gameplay panel is a game character or a
    # false hit; the gameplay_no_face row covers a leaked streamer); otherwise the main panel holds it
    if pn.get("overlay") and rects.get("cam"):
        out = [(Path(pn["overlay"]), rects["cam"], False)]
    elif pn.get("main") and rects.get("main"):
        out = [(Path(pn["main"]), rects["main"], True)]
    else:
        out = []
    return [s for s in out if s[0].is_file()]


def captions_cover_face(faces: list, boxes: list) -> bool:
    """True when a face box (canvas px) and a caption box overlap by >= 20% of the face height."""
    for fx0, fy0, fx1, fy1 in faces:
        for _s, _e, cx0, cy0, cx1, cy1 in boxes:
            if fx0 < cx1 and cx0 < fx1 and min(fy1, cy1) - max(fy0, cy0) >= 0.2 * (fy1 - fy0):
                return True
    return False


def check_captions_off_faces(mp4: Path, tl: dict, samples: int = 8):
    """
    (status, detail) or None without OpenCV, for every layout: at up to `samples` moments when a caption or the
    hook shows, faces are detected on the face panel (the picture without captions, mapped onto the canvas,
    zoom included) and compared with the caption boxes. FAIL when a caption covers a face on 2+ frames.
    """
    boxes = caption_boxes(tl)
    if not boxes:
        return "N/A", "no captions"
    sources = _panel_sources(Path(mp4), tl)
    if not sources:
        return "WARN", "panel files not available (moved or deleted); caption/face overlap not checked"
    t0, t1 = min(b[0] for b in boxes), max(b[1] for b in boxes)
    grid = [t0 + (t1 - t0) * (i + 0.5) / (4 * samples) for i in range(4 * samples)]
    shown_at = [t for t in grid if any(b[0] <= t < b[1] for b in boxes)]
    if not shown_at:
        return "N/A", "no captions"
    k = min(samples, len(shown_at))
    times = [round(shown_at[int(i * len(shown_at) / k)], 3) for i in range(k)]
    segs = sorted(tl.get("segments") or [], key=lambda s: s["tl_start"])
    if not segs:
        return "N/A", "no segments in the timeline sidecar"

    def panel_time(t):
        seg = next((s for s in reversed(segs) if s["tl_start"] <= t), segs[0])
        pt = float(seg["src_s"]) + t - float(seg["tl_start"])
        return min(max(pt, float(seg["src_s"])), float(seg["src_e"]) - 0.02), float(seg.get("zoom") or 1.0)

    H = float((tl.get("canvas") or [1080, 1920])[1])
    found = [[] for _ in times]
    for src, (rx, ry, rw, rh), zoomed in sources:
        pts = [panel_time(t) for t in times]
        faces = detect_faces(src, [p for p, _ in pts], rw, rh)
        if faces is None:
            return None
        for i, fl in enumerate(faces):
            z = pts[i][1] if zoomed else 1.0
            for f in fl:
                x0, y0, x1, y1 = (0.5 + (v - 0.5) * z for v in f)
                box = [rx + x0 * rw, ry + y0 * rh, rx + x1 * rw, ry + y1 * rh]
                if box[3] - box[1] >= CAPTION_FACE_MIN_H * H:
                    found[i].append(box)
    for i, fl in enumerate(found):  # a poster or a TV face behind the streamer is not a face to keep clear
        big = max((b[3] - b[1] for b in fl), default=0.0)
        found[i] = [b for b in fl if b[3] - b[1] >= CAPTION_FACE_REL_H * big]
    hit = []
    for t, faces in zip(times, found, strict=True):
        shown = [b for b in boxes if b[0] <= t < b[1]]
        if captions_cover_face(faces, shown):
            hit.append(round(t, 1))
    with_face = sum(1 for f in found if f)
    if len(hit) >= 2:
        return "FAIL", f"a caption covers a face at {hit} s ({len(hit)}/{len(times)} sampled frames)"
    return "PASS", f"captions clear of faces ({with_face}/{len(times)} sampled frames with a face, {len(hit)} covered)"


def check_panel_black(mp4: Path, rect: list) -> tuple:
    """
    (status, detail): blackdetect on the gameplay panel alone (>= 1 s, pix_th=0.12, 70% of the panel black),
    so a black game screen next to a lit facecam fails too; the whole-frame blackdetect misses it.
    """
    x, y, w, h = [int(v) for v in rect]
    res = run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "info",
            "-i",
            str(mp4),
            "-an",
            "-vf",
            f"crop={w}:{h}:{x}:{y},scale=270:-2,blackdetect=d=1.0:pix_th=0.12:pic_th=0.7",
            "-f",
            "null",
            "-",
        ],
        "gameplay panel black scan",
    )
    spans = re.findall(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)", res.stderr or "")
    if spans:
        return "FAIL", "gameplay panel black at " + ", ".join(f"{float(a):.2f}-{float(b):.2f}s" for a, b in spans[:5])
    return "PASS", "gameplay panel never black for >= 1 s (pix_th=0.12, 70% of the panel)"


def check_panel_faces(mp4: Path, rect: list, dur: float):
    """
    (status, detail) or None without OpenCV: faces in 6 frames of the rendered gameplay panel. FAIL when
    a face shows at the same place (IoU >= 0.3) on 3+ frames: a streamer leaked into the gameplay crop
    stays put for the whole clip, while Haar false hits on game scenery (trees, blankets, doors) move with
    the game camera (2 frames can match when the game camera stands still for a moment).
    """
    x, y, w, h = [int(v) for v in rect]
    times = [max(0.1, dur) * (k + 0.5) / 6 for k in range(6)]
    faces = detect_faces(mp4, times, w, h, crop=(x, y, w, h))
    if faces is None:
        return None
    steady = 0
    for i, fl in enumerate(faces):
        others = [g for j, gl in enumerate(faces) if j != i for g in gl]
        steady += any(_iou(f, g) >= 0.3 for f in fl for g in others)
    loose = sum(1 for fl in faces if fl)
    if steady >= 3:
        return "FAIL", f"a face shows at the same place in the gameplay panel on {steady}/6 frames; set --game-crop"
    return "PASS", f"no steady face in the gameplay panel ({loose}/6 frames with a stray detection)"


def check_speaker_faces(mp4: Path, dur: float, samples: int = 8):
    """
    (status, detail) or None without OpenCV, for the speaker layout: faces in `samples` frames of the
    rendered clip. FAIL when a face touches the left or right frame edge (cut by the crop) on 2+ frames.
    """
    times = [max(0.1, dur) * (k + 0.5) / samples for k in range(samples)]
    faces = detect_faces(mp4, times, 1080, 1920)
    if faces is None:
        return None
    cut = [round(t, 1) for t, fl in zip(times, faces, strict=False) if any(f[0] <= 0.005 or f[2] >= 0.995 for f in fl)]
    found = sum(1 for fl in faces if fl)
    if len(cut) >= 2:
        return "FAIL", f"a face touches the frame edge at {cut} s ({len(cut)}/{samples} frames)"
    return "PASS", f"faces inside the frame ({found}/{samples} frames with a face, {len(cut)} at an edge)"


def print_gate_table(title: str, rows: list) -> bool:
    print(title)
    print(f"  {'CHECK':<24}{'STATUS':<8}DETAIL")
    for check, status, detail in rows:
        print(f"  {check:<24}{status:<8}{detail}")
    fails = [r for r in rows if r[1] == "FAIL"]
    print(f"  => {'FAIL' if fails else 'PASS'} ({len(fails)} failed, {sum(r[1] == 'WARN' for r in rows)} warnings)")
    return not fails


def render_and_gate(plan: dict, out_mp4: Path, black_background: bool = False) -> bool:
    """
    Render, run the export gate and, when it passes, write <name>_thumb.jpg. black_background: the source is
    black on purpose (the style gallery's sample), so blackdetect is N/A instead of FAIL.
    """
    mp4 = render_plan_mp4(plan, out_mp4)
    rows = gate_mp4(mp4)
    if black_background:
        rows = [
            (c, "N/A", "black sample background on purpose") if c == "blackdetect" else (c, s, d) for c, s, d in rows
        ]
    ok = print_gate_table(f"EXPORT GATE: {mp4.name}", rows)
    # <name>.gate.json: the same table for the dashboard's audit view
    write_json_atomic(mp4.with_name(f"{mp4.stem}.gate.json"), {"pass": ok, "rows": [list(r) for r in rows]})
    if ok:
        try:
            make_thumbnail(plan, mp4)
        except (ToolError, OSError, ValueError) as e:
            print(f"Thumbnail skipped for {mp4.name}: {e}")
    return ok


def _gate_summary(results: list):
    """Print one line per rendered MP4; exit 1 when any export gate failed."""
    if not results:
        return
    print("RENDER SUMMARY")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not all(ok for _, ok in results):
        raise SystemExit(1)
