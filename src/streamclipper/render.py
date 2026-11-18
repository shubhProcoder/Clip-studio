"""MP4 renderer (ffmpeg)."""

import json
import os
import re
import shutil
import struct
from pathlib import Path

from streamclipper.audio import measure_loudness
from streamclipper.brand import HANDLE_SIZE, card_seconds, load_brand_kit, plan_brand
from streamclipper.captions.ass import _ass_escape, _ass_time, build_ass
from streamclipper.captions.coverage import spoken_words_tl
from streamclipper.captions.styles import FONT_FILES
from streamclipper.effects import ENHANCE_MIX, effects_of, perspective_filter, zoom_plan_for
from streamclipper.env import _first_line, ttf_full_name
from streamclipper.layout import apply_caption_anchor, layout_boxes, probe_video
from streamclipper.paths import TEMPLATE_DIR
from streamclipper.util import ToolError, fwd, run_cmd

RENDER_FPS = 30
RNNOISE_MODEL = Path(__file__).resolve().parent / "data" / "models" / "bd.rnnn"  # RNNoise-nu beguiling-drafter


ARNNDN_DELAY = 480  # samples at 48 kHz: arnndn's output lags its input by one RNNoise frame (10 ms, measured)


def enhance_mix(level: str):
    """--enhance-speech: the share of denoised signal (light 0.6, strong 0.9), None when off."""
    return ENHANCE_MIX.get(level or "off")


def enhance_lines(head: str, mix: float, tag: str) -> tuple:
    """
    Speech enhancement on one audio chain: highpass, then RNNoise (arnndn, fully wet) mixed with the dry signal
    delayed by the same 10 ms, and the 10 ms taken off again so the lane stays in sync with the video.
    (arnndn's own mix option blends its delayed output with the undelayed input: a comb filter that sounds
    phasey.) head: the chain so far, ending in ','; returns (statements, the new head ending in ',').
    Runs in <out>/_render/<clip> (the model is copied to <out>/_render/_models).
    """
    d = ARNNDN_DELAY
    lines = [
        f"{head}highpass=f=70,asplit=2[{tag}d][{tag}w]",
        f"[{tag}w]arnndn=m=../_models/{RNNOISE_MODEL.name}:mix=1[{tag}n]",
        f"[{tag}d]adelay=delays={d}S|{d}S[{tag}dd]",
    ]
    new_head = (
        f"[{tag}n][{tag}dd]amix=inputs=2:weights='{mix:g} {1 - mix:g}':normalize=0,"
        f"atrim=start_sample={d},asetpts=PTS-STARTPTS,"
    )
    return lines, new_head


_FFMPEG_MAJOR = []


def ffmpeg_major() -> int:
    if not _FFMPEG_MAJOR:
        m = re.search(r"version\s+n?(\d+)\.", _first_line(["ffmpeg", "-hide_banner", "-version"]))
        _FFMPEG_MAJOR.append(int(m.group(1)) if m else 99)  # git builds (N-xxxxx) are recent
    return _FFMPEG_MAJOR[0]


def _graph_args(graph_file: str) -> list:
    """Filter graph from a file (no Windows command-line limit): -/filter_complex on ffmpeg >= 7."""
    return ["-/filter_complex", graph_file] if ffmpeg_major() >= 7 else ["-filter_complex_script", graph_file]


def _audio_graph(
    lanes: list, segs: list, main_idx: dict, has_audio: bool, sfx: list, sfx_idx0: int, enhance: float = None
) -> tuple:
    """Shared audio graph (both loudnorm passes): per-segment atrim + 12 ms fades, then SFX mixed in.
    enhance: the speech enhancement mix (enhance_mix), applied to each main lane before the cuts."""
    g, labels = [], {}
    for L, lane in enumerate(lanes):
        k = len(lane["idx"])
        outs = "".join(f"[as{i}]" for i in lane["idx"])
        if has_audio:
            head = f"[{main_idx[L]}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            if enhance:
                pre, head = enhance_lines(head, enhance, f"en{L}x")
                g += pre
            g.append(f"{head}apad=pad_dur=0.5,asplit={k}{outs}")
        else:
            span = (lane["end"] - lane["a0"]) / RENDER_FPS + 0.5
            g.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={span:.6f},asplit={k}{outs}")
        for i in lane["idx"]:
            s = segs[i]
            a = (s["a"] - lane["a0"]) / RENDER_FPS
            d = s["n"] / RENDER_FPS
            fade = ""
            if s["fade_ms"] > 0 and d > 0.06:
                f = s["fade_ms"] / 1000.0
                fade = f",afade=t=in:d={f:.3f},afade=t=out:st={d - f:.6f}:d={f:.3f}"
            g.append(f"[as{i}]atrim=start={a:.6f}:end={a + d:.6f},asetpts=PTS-STARTPTS{fade}[ac{i}]")
            labels[i] = f"[ac{i}]"
    mix = []
    for j, e in enumerate(sfx):
        ms = int(round(float(e["tl_start"]) * 1000))
        g.append(
            f"[{sfx_idx0 + j}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"adelay={ms}|{ms},volume={float(e['volume']):.3f}[sx{j}]"
        )
        mix.append(f"[sx{j}]")
    return g, labels, mix


LOUD_TARGET = -11.0


# The AAC encode (16-18 kHz low-pass) reads 0.3-0.6 LU quieter than the float mix, so the gain
# search aims 0.3 LU above the target.
LOUD_SEARCH = LOUD_TARGET + 0.3
LOUD_REACH = 0.5  # a gain search that ends further than this from its aim did not reach the target


# Sample-peak limiter at -3 dBFS, so the true peak after AAC stays below -1 dBTP.
LIMIT_DB = -3.0
MAX_LIMITING_DB = 6.0  # more sustained gain reduction than this pumps audibly
AAC_TP_MAX = -1.2  # true peak of the encoded AAC audio we accept (gate: -1.0)


def limiter(limit_db: float = LIMIT_DB) -> str:
    return f"alimiter=limit={10 ** (limit_db / 20.0):.4f}:attack=5:release=50:level=disabled"


LIMITER = limiter()
# Gain staging: compressor stages between the loudness gain and the final limiter, as (threshold dBFS,
# ratio); attack 5 ms, release 120 ms. The compressor takes the sustained loud parts (a scream that is
# already clipped in the stream) down smoothly, so the limiter only catches what is left. Stage 0 = none.
COMP_STAGES = ((None, None), (-8.0, 3), (-10.0, 4), (-12.0, 6))


def comp_filter(stage: int) -> str:
    """acompressor (placed after the gain) for a gain-staging stage, '' for stage 0."""
    thr_db, ratio = COMP_STAGES[stage]
    if thr_db is None:
        return ""
    return f"acompressor=threshold={10 ** (thr_db / 20.0):.5f}:ratio={ratio}:attack=5:release=120:makeup=1,"


SFX_UNDER_VOICE_DB = 6.0


def limiting_db(tp_pre, limit_db: float = LIMIT_DB) -> float:
    """Peak gain reduction of the limiter: pre-limiter true peak above its ceiling (transients included)."""
    return 0.0 if tp_pre is None else max(0.0, float(tp_pre) - limit_db)


def sustained_limiting(m_pre: list, m_post: list) -> float:
    """
    Sustained gain reduction of the limiter: the largest drop of the 400 ms momentary loudness between
    the mix before and after the limiter (audible pumping). Single transients barely move it, so a
    clipped scream that the limiter catches for a few ms is not counted as pumping.
    The two ebur128 logs can differ by a frame or two (limiter latency, stream end), so the lists are
    aligned on the offset (-3..3 frames) with the smallest mean difference, and on long lists the first
    and last 3 frames (start-up and end-of-stream artefacts) are left out.
    """

    def pairs(k):
        a, b = (m_pre[k:], m_post) if k >= 0 else (m_pre, m_post[-k:])
        n = min(len(a), len(b))
        edge = 3 if n > 20 else 0
        return [(x, y) for x, y in zip(a[edge : n - edge], b[edge : n - edge], strict=True) if x > -30.0 and y > -70.0]

    best = None
    for k in range(-3, 4):
        pr = pairs(k)
        if pr:
            score = sum(abs(x - y) for x, y in pr) / len(pr)
            if best is None or score < best[0]:
                best = (score, pr)
    if best is None:
        return 0.0
    return round(max(x - y for x, y in best[1]), 2)


def next_stage(stage: int, limiting: float) -> int:
    """The next compressor stage when the limiting exceeds 6 dB, else None (also at the last stage)."""
    if limiting > MAX_LIMITING_DB and stage + 1 < len(COMP_STAGES):
        return stage + 1
    return None


LIMIT_FLOOR_DB = -6.0  # lowest limiter ceiling: each drop makes the next gain search push harder into the limiter


def aac_ceiling(limit_db: float, aac_tp) -> float:
    """
    Limiter ceiling lowered by the AAC overshoot so the encoded true peak lands at <= -1.2 dBTP, never below
    LIMIT_FLOOR_DB. Without the floor a hard-limited clip ran away (live Twitch clip on Colab: -3 -> -8.9 dB,
    the gain search then hit its cap and the clip stayed at -14.3 LUFS with the AAC peak at -6.8 dBTP).
    """
    if aac_tp is None or aac_tp <= AAC_TP_MAX:
        return limit_db
    return max(LIMIT_FLOOR_DB, round(limit_db - (aac_tp - AAC_TP_MAX) - 0.3, 2))


def sfx_volume_cap(volume: float, voice_lufs, sfx_lufs) -> float:
    """SFX volume lowered so the event sits at least 6 dB under the voice lane's loudness."""
    if voice_lufs is None or sfx_lufs is None:
        return float(volume)
    cap = 10 ** ((float(voice_lufs) - SFX_UNDER_VOICE_DB - float(sfx_lufs)) / 20.0)
    return round(min(float(volume), cap), 4)


_SFX_LUFS = {}


def _sfx_lufs(path) -> float:
    key = str(path)
    if key not in _SFX_LUFS:
        _SFX_LUFS[key] = measure_loudness(Path(path))
    return _SFX_LUFS[key]


def ebur128_summary(text: str) -> tuple:
    """(integrated LUFS, true peak dBTP) from the last ebur128 Summary block of an ffmpeg log."""
    summ = text[text.rfind("Summary:") :] if "Summary:" in text else ""
    mi = re.search(r"I:\s+(-?[\d.]+|-inf)\s+LUFS", summ)
    mp = re.search(r"Peak:\s+(-?[\d.]+|-inf)\s+dBFS", summ)
    li = float(mi.group(1)) if mi and mi.group(1) != "-inf" else None
    tp = float(mp.group(1)) if mp and mp.group(1) != "-inf" else None
    return li, tp


def loudness_measure(cmd_inputs: list, graph: str, cwd: Path, frames: bool = False, encode: Path = None):
    """
    Run only the audio graph (ending in ebur128=peak=true[aout]); returns (LUFS, dBTP), plus the 400 ms
    momentary loudness per 100 ms with frames=True. encode: write the audio as AAC 48 kHz to this file
    instead of measuring (the graph then ends in [aout] without ebur128).
    """
    gfile = Path(cwd) / "loudness_pass.txt"
    gfile.write_text(graph, encoding="utf-8")
    res = run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "info",
            *cmd_inputs,
            *_graph_args(gfile.name),
            "-map",
            "[aout]",
            *(["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-y", str(encode)] if encode else ["-f", "null", "-"]),
        ],
        "loudness measurement",
        cwd=cwd,
    )
    if encode:
        return None
    text = res.stderr or ""
    li, tp = ebur128_summary(text)
    if not frames:
        return li, tp
    m = [
        float(x)
        for x in re.findall(r"\bM:\s*(-?[\d.]+)", text[: text.rfind("Summary:")] if "Summary:" in text else text)
    ]
    return li, tp, m


def render_plan_mp4(plan: dict, out_mp4: Path) -> Path:
    """
    ClipPlan -> ready-to-post MP4 (1080x1920 or 1080x1080, 30 fps, H.264 High, AAC 48 kHz,
    -11 LUFS, faststart) + <name>.timeline.json sidecar for the export gate.
    Uses the same panel files and cut list as the CapCut draft, so both match.
    """
    out_mp4 = Path(out_mp4).resolve()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    name = plan.get("name") or out_mp4.stem
    pn = plan["panels"]
    layout, ratio = plan["layout"], plan["ratio"]
    main = Path(pn["main_src"])
    overlay = Path(pn["overlay_src"]) if pn.get("overlay_src") else None
    for p in [main] + ([overlay] if overlay else []):
        if not p.is_file():
            raise ToolError(
                f"Panel file missing: {fwd(p)}",
                "re-run clip-stream/build for this clip (panels are cached in <out>/panels)",
            )
    mmeta = probe_video(main)
    lay = apply_caption_anchor(layout_boxes(ratio, layout, main_h=mmeta["height"]), pn.get("caption_anchor"))
    W, H = lay["canvas"]
    if mmeta["width"] != W:
        raise ToolError(
            f"Main panel is {mmeta['width']} px wide, expected {W}.", "re-run the clip so panels are rebuilt"
        )
    looped = bool(pn.get("overlay_loop")) and overlay is not None
    stacked = overlay is not None and not looped
    ometa = probe_video(overlay) if overlay else None

    fps = RENDER_FPS
    segs, tl = [], 0
    for s in plan["segments"]:
        a = int(round(float(s["src_s"]) * fps))
        n = int(round((float(s["src_e"]) - float(s["src_s"])) * fps))
        if n < 1:
            continue
        segs.append(
            {
                "a": a,
                "n": n,
                "kind": s.get("kind", "story"),
                "zoom": float(s.get("zoom") or 1.0),
                "fade_ms": float(s.get("fade_ms") or 0),
                "tl_start": round(tl / fps, 4),
            }
        )
        tl += n
    if not segs:
        raise ToolError(f"Clip plan {name} has no segments.", "check the clip plan")
    total = tl / fps
    # Lanes: runs of segments in source order. The tease (a later moment shown first) gets its own
    # input, so ffmpeg never buffers seconds of frames while waiting for it.
    lanes = []
    for i, s in enumerate(segs):
        if lanes and s["a"] >= lanes[-1]["end"]:
            lanes[-1]["idx"].append(i)
            lanes[-1]["end"] = s["a"] + s["n"]
        else:
            lanes.append({"a0": s["a"], "end": s["a"] + s["n"], "idx": [i]})

    rdir = out_mp4.parent / "_render" / re.sub(r"[^\w.-]", "_", name)
    fdir = out_mp4.parent / "_render" / "_fonts"
    rdir.mkdir(parents=True, exist_ok=True)
    fdir.mkdir(parents=True, exist_ok=True)
    for fn in FONT_FILES.values():
        if (TEMPLATE_DIR / "fonts" / fn).is_file() and not (fdir / fn).is_file():
            shutil.copy2(TEMPLATE_DIR / "fonts" / fn, fdir / fn)
    eff = effects_of(plan)
    kit = load_brand_kit(eff["brand_kit"]) if eff["brand_kit"] else None
    enh = enhance_mix(eff["enhance_speech"]) if mmeta["has_audio"] else None
    if enh:
        mdir = out_mp4.parent / "_render" / "_models"
        mdir.mkdir(parents=True, exist_ok=True)
        if not (mdir / RNNOISE_MODEL.name).is_file():
            shutil.copy2(RNNOISE_MODEL, mdir / RNNOISE_MODEL.name)
    brand_ass = None
    if kit:
        brand_ass = {"hook_box": kit["colours"].get("hook_box") or ""}
        if kit["font"]:
            fp = Path(kit["font"])
            if not (fdir / fp.name).is_file() or (fdir / fp.name).stat().st_size != fp.stat().st_size:
                shutil.copy2(fp, fdir / fp.name)
            brand_ass["font"] = ttf_full_name(fdir / fp.name) or ""
    cap_info = build_ass(plan, lay, rdir / "clip.ass", fdir, brand=brand_ass)
    cap_info["words_tl"] = spoken_words_tl(plan)  # the export gate's caption_coverage row

    def lane_input(L, path):
        lane = lanes[L]
        return [
            "-ss",
            f"{lane['a0'] / fps:.6f}",
            "-t",
            f"{(lane['end'] - lane['a0']) / fps + 0.5:.6f}",
            "-i",
            str(path),
        ]

    inputs, main_idx, ov_idx, n_in = [], {}, {}, 0
    for L in range(len(lanes)):
        inputs += lane_input(L, main)
        main_idx[L] = n_in
        n_in += 1
        if stacked:
            inputs += lane_input(L, overlay)
            ov_idx[L] = n_in
            n_in += 1
    fill_idx = None
    if looped:
        inputs += ["-stream_loop", "-1", "-i", str(overlay)]
        fill_idx = n_in
        n_in += 1
    sfx = [e for e in plan.get("sfx") or [] if Path(e["source"]).is_file() and float(e["tl_start"]) < total]
    sfx_inputs = []
    for e in sfx:
        sfx_inputs += ["-i", str(Path(e["source"]))]

    has_audio = mmeta["has_audio"]

    def mix_tail(labels, mix):
        cat = "".join(labels[i] for i in range(len(segs)))
        g = [f"{cat}concat=n={len(segs)}:v=0:a=1[acat]"]
        if mix:
            g.append(f"[acat]{''.join(mix)}amix=inputs={1 + len(mix)}:normalize=0:duration=first[amx]")
            return g, "[amx]"
        return g, "[acat]"

    # Pass 1: audio only, main lanes + SFX
    p1_inputs, p1_idx = [], {}
    for L in range(len(lanes)):
        p1_inputs += lane_input(L, main)
        p1_idx[L] = L
    voice_lufs = None
    if has_audio and sfx:
        # SFX at least 6 dB under the voice: cap each event by its file loudness vs the voice lane
        ag0, labels0, _ = _audio_graph(lanes, segs, p1_idx, has_audio, [], len(lanes), enh)
        tail0, alab0 = mix_tail(labels0, [])
        voice_lufs, _ = loudness_measure(p1_inputs, ";\n".join(ag0 + tail0 + [f"{alab0}ebur128=peak=true[aout]"]), rdir)
        sfx = [dict(e, volume=sfx_volume_cap(e["volume"], voice_lufs, _sfx_lufs(e["source"]))) for e in sfx]
    ag, labels, mix = _audio_graph(lanes, segs, p1_idx, has_audio, sfx, len(lanes), enh)
    tail, alab = mix_tail(labels, mix)
    # Loudness: measure the mix, then gain (+ compressor stage) + limiter, re-measured until within
    # 0.3 LU of -11 LUFS. (Two-pass loudnorm cannot stay linear at -11 LUFS / -1.5 dBTP on stream audio
    # and its dynamic fallback lands around -13.7 LUFS with peaks near 0 dBTP, so it is not used.)
    # Gain staging: when the limiter would take off more than 6 dB (audible pumping), the next
    # compressor stage evens the peaks out before the limiter and the search runs again.

    def search(comp: str, lim: str, aim: float) -> tuple:
        gain, measured = 0.0, []
        for _ in range(6):
            li, tp = loudness_measure(
                p1_inputs + sfx_inputs,
                ";\n".join(ag + tail + [f"{alab}volume={gain:.2f}dB,{comp}{lim},ebur128=peak=true[aout]"]),
                rdir,
            )
            if li is None or li < -70:
                break
            measured.append({"gain_db": round(gain, 2), "I": li, "TP": tp})
            if abs(li - aim) <= 0.3:
                break
            slope = 1.0  # LU per dB of gain; less than 1 once the limiter works (secant estimate)
            if len(measured) >= 2 and measured[-1]["gain_db"] != measured[-2]["gain_db"]:
                slope = (measured[-1]["I"] - measured[-2]["I"]) / (measured[-1]["gain_db"] - measured[-2]["gain_db"])
                slope = max(0.25, min(1.0, slope))
            gain = round(max(-30.0, min(36.0, gain + (aim - li) / slope)), 2)
        if measured:
            gain = min(measured, key=lambda m: abs(m["I"] - aim))["gain_db"]
        return gain, measured

    def meter(chain: str) -> tuple:
        return loudness_measure(
            p1_inputs + sfx_inputs, ";\n".join(ag + tail + [f"{alab}{chain}ebur128=peak=true[aout]"]), rdir, True
        )

    gain, measured, stage, staging, comp = 0.0, [], 0, None, ""
    limit_db, lim, aim = LIMIT_DB, LIMITER, LOUD_SEARCH
    reached = pinned = None  # last compressor stage that reached the loudness target; the stage kept
    if has_audio:
        for _ in range(len(COMP_STAGES) + 3):
            comp = comp_filter(stage)
            gain, measured = search(comp, lim, aim)
            if not measured:
                break
            best_i = min(measured, key=lambda m: abs(m["I"] - aim))["I"]
            if abs(best_i - aim) > LOUD_REACH and reached is not None and pinned is None:
                # a stronger compressor absorbed the gain (gain cap hit): -11 LUFS is a hard rule, limiting
                # <= 6 dB a soft one, so stay on the last stage that reached the target
                pinned = reached
                stage = pinned
                comp = comp_filter(stage)
                gain, measured = search(comp, lim, aim)
            _, tp_pre, m_pre = meter(f"volume={gain:.2f}dB,{comp}")
            _, _, m_post = meter(f"volume={gain:.2f}dB,{comp}{lim},")
            sustained = sustained_limiting(m_pre, m_post)
            staging = {
                "stage": stage,
                "compressor": comp.rstrip(","),
                "limiter_ceiling_db": limit_db,
                "pre_limiter_tp": tp_pre,
                "peak_limiting_db": round(limiting_db(tp_pre, limit_db), 2),
                "limiting_db": sustained,
                "voice_lufs": voice_lufs,
            }
            if abs(best_i - aim) <= LOUD_REACH:
                reached = stage
            if pinned is not None:
                staging["note"] = f"stronger compression could not reach {LOUD_TARGET:g} LUFS; kept stage {stage}"
            nxt = None if pinned is not None else next_stage(stage, sustained)
            if nxt is not None:
                stage = nxt
                continue
            if sustained > MAX_LIMITING_DB and aim > LOUD_TARGET - 0.5 and pinned is None:
                aim = LOUD_TARGET - 0.6  # last stage still pumps: -11.6 LUFS is inside the -11 +/- 1 target
                continue
            # AAC adds inter-sample overshoot on hard-limited audio: check the encoded peak once
            enc = rdir / "aac_check.m4a"
            loudness_measure(
                p1_inputs + sfx_inputs,
                ";\n".join(ag + tail + [f"{alab}volume={gain:.2f}dB,{comp}{lim},aresample=48000[aout]"]),
                rdir,
                encode=enc,
            )
            _, aac_tp = loudness_measure(["-i", str(enc)], "[0:a]ebur128=peak=true[aout]", rdir)
            staging["aac_tp"] = aac_tp
            new_limit = aac_ceiling(limit_db, aac_tp)
            if new_limit == limit_db:
                break
            limit_db, lim = new_limit, limiter(new_limit)
    ln2 = f"volume={gain:.2f}dB,{comp}{lim},aresample=48000" if measured else "aresample=48000"

    # Pass 2: video + audio
    g = []
    for L, lane in enumerate(lanes):
        k = len(lane["idx"])
        g.append(
            f"[{main_idx[L]}:v]fps={fps},setsar=1,tpad=stop_mode=clone:stop_duration=0.5,split={k}"
            + "".join(f"[mv{i}]" for i in lane["idx"])
        )
        if stacked:
            g.append(
                f"[{ov_idx[L]}:v]fps={fps},setsar=1,tpad=stop_mode=clone:stop_duration=0.5,split={k}"
                + "".join(f"[ov{i}]" for i in lane["idx"])
            )
        for i in lane["idx"]:
            s = segs[i]
            a = s["a"] - lane["a0"]
            zf = ""
            if abs(s["zoom"] - 1.0) > 0.01:
                mw, mh = mmeta["width"], mmeta["height"]
                zf = f",scale={int(mw * s['zoom']) & ~1}:{int(mh * s['zoom']) & ~1},crop={mw}:{mh}"
            g.append(f"[mv{i}]trim=start_frame={a}:end_frame={a + s['n']},setpts=PTS-STARTPTS{zf}[m{i}]")
            if stacked:
                g.append(f"[ov{i}]trim=start_frame={a}:end_frame={a + s['n']},setpts=PTS-STARTPTS[o{i}]")
    ag, labels, mix = _audio_graph(lanes, segs, main_idx, has_audio, sfx, n_in, enh)
    g += ag
    cat_in = "".join(f"[m{i}]" + (f"[o{i}]" if stacked else "") + labels[i] for i in range(len(segs)))
    zp = zoom_plan_for(plan) if eff["zoom"] != "off" else None
    z_main = perspective_filter(zp["main"], fps) if zp else ""
    z_ov = perspective_filter(zp["overlay"], fps) if zp and stacked else ""
    m_lab, o_lab = ("[mcat0]" if z_main else "[mcat]"), ("[ocat0]" if z_ov else "[ocat]")
    g.append(
        f"{cat_in}concat=n={len(segs)}:v={2 if stacked else 1}:a=1"
        + (f"{m_lab}{o_lab}[acat]" if stacked else f"{m_lab}[acat]")
    )
    if z_main:
        g.append(f"[mcat0]{z_main}[mcat]")
    if z_ov:
        g.append(f"[ocat0]{z_ov}[ocat]")
    if mix:
        g.append(f"[acat]{''.join(mix)}amix=inputs={1 + len(mix)}:normalize=0:duration=first[amx]")
        alab = "[amx]"
    else:
        alab = "[acat]"
    intro_n, outro_n = (int(round(s * fps)) for s in card_seconds(kit))
    cards = bool(intro_n or outro_n)
    g.append(f"{alab}{ln2}" + ("[abody]" if cards else "[aout]"))
    if stacked:
        if ometa["width"] != W or ometa["height"] + mmeta["height"] != H:
            raise ToolError("Panel sizes do not add up to the canvas.", "re-run the clip so panels are rebuilt")
        g.append("[ocat][mcat]vstack=inputs=2[stk]")
    elif looped:
        if ometa["width"] != W or ometa["height"] + mmeta["height"] != H:
            raise ToolError("Filler panel size does not fit the canvas.", "re-run the clip so panels are rebuilt")
        # -stream_loop restarts timestamps at every loop; renumber frames so vstack pairs them 1:1.
        g.append(f"[{fill_idx}:v]fps={fps},setsar=1,trim=end_frame={tl},settb=1/{fps},setpts=N[fl]")
        g.append(f"[mcat]settb=1/{fps},setpts=N[mfr]")
        g.append("[mfr][fl]vstack=inputs=2:shortest=1[stk]")
    else:
        g.append("[mcat]null[stk]")
    brand_info, brand_inputs = None, []
    if kit is None:
        g.append("[stk]ass=clip.ass:fontsdir=../_fonts,format=yuv420p[vout]")
    else:
        brand_info, brand_inputs = _brand_graph(
            g, kit, plan, lay, cap_info, total, (intro_n, outro_n), rdir, n_in + len(sfx), stacked
        )
    gfile = rdir / "graph.txt"
    gfile.write_text(";\n".join(g), encoding="utf-8")
    shutil.copy2(gfile, out_mp4.with_name(f"{out_mp4.stem}.graph.txt"))

    tmp = out_mp4.with_name(out_mp4.stem + ".part.mp4")
    print(f"Rendering {fwd(out_mp4)} ({total:.2f}s, {len(segs)} pieces, {layout} {ratio}) ...")
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            *inputs,
            *sfx_inputs,
            *brand_inputs,
            *_graph_args(gfile.name),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-maxrate",
            "12M",
            "-bufsize",
            "24M",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-g",
            "60",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(tmp),
        ],
        f"MP4 render of {name}",
        cwd=rdir,
    )
    os.replace(tmp, out_mp4)

    sw = plan.get("source_window") or {}
    shift = intro_n / fps  # an intro card moves everything after it
    if shift:
        cap_info["spans"] = [[round(s[0] + shift, 3), round(s[1] + shift, 3), *s[2:]] for s in cap_info["spans"]]
        cap_info["words_tl"] = [[w[0], round(w[1] + shift, 3), round(w[2] + shift, 3)] for w in cap_info["words_tl"]]
    sidecar = {
        "tool": "capcut_studio",
        "version": "3.1",
        "type": "timeline",
        "name": name,
        "mp4": fwd(out_mp4),
        "expected_duration": round(total + (intro_n + outro_n) / fps, 3),
        "plan_expected_duration": plan.get("expected_duration"),
        "fps": fps,
        "ratio": ratio,
        "layout": layout,
        "canvas": lay["canvas"],
        "rects": lay["rects"],
        "panels": {"main": fwd(main), "overlay": fwd(overlay) if stacked else None},
        "caption": cap_info,
        "cam_box_px": (plan.get("boxes_px") or {}).get("cam"),
        "game_box_px": (plan.get("boxes_px") or {}).get("game"),
        "person_box_px": (plan.get("boxes_px") or {}).get("person"),
        "cam_source": {
            "path": sw.get("path"),
            "offset": float(sw.get("offset", 0.0)),
            "dur": round(float(sw.get("end", 0)) - float(sw.get("start", 0)), 3),
        },
        "segments": [
            {
                "src_s": round(s["a"] / fps, 4),
                "src_e": round((s["a"] + s["n"]) / fps, 4),
                "tl_start": round(s["tl_start"] + shift, 4),
                "kind": s["kind"],
                "zoom": s["zoom"],
            }
            for s in segs
        ],
        "sfx": [
            {"name": e.get("name"), "tl_start": round(float(e["tl_start"]) + shift, 3), "volume": e["volume"]}
            for e in sfx
        ],
        "loudness_passes": measured,
        "gain_staging": staging,
        "draft": plan.get("draft"),
    }
    if any(v != "off" and v for v in (eff["zoom"], eff["enhance_speech"], eff["brand_kit"])) or eff["remove_fillers"]:
        sidecar["effects"] = {
            "zoom": eff["zoom"],
            "zoom_events": {k: (zp or {}).get(k, []) for k in ("main", "overlay")},
            "zoom_notes": (zp or {}).get("notes", []),
            "enhance_speech": eff["enhance_speech"],
            "remove_fillers": eff["remove_fillers"],
            "fillers_removed": len(plan.get("fillers_removed") or []),
            "brand": brand_info,
        }
    out_mp4.with_name(f"{out_mp4.stem}.timeline.json").write_text(
        json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"MP4 -> {fwd(out_mp4)}")
    return out_mp4


def _brand_faces(plan: dict, lay: dict, total: float, stacked: bool) -> list:
    """Face boxes (canvas px, padded 4% of the width) on 4 frames across the clip, for the logo/handle corner.
    Without a face detector the whole cam panel counts as a face."""
    from streamclipper.effects import _faces_at
    from streamclipper.faces import get_detector

    rects = lay.get("rects") or {}
    pn = plan["panels"]
    panels = [(pn["main_src"], rects.get("main"))]
    if stacked:
        panels.append((pn["overlay_src"], rects.get("cam")))
    det = get_detector()
    pad = 0.04 * lay["canvas"][0]
    out = []
    for path, rect in panels:
        if not rect:
            continue
        rx, ry, rw, rh = rect
        if det is None:
            if path == pn.get("overlay_src"):
                out.append([rx, ry, rx + rw, ry + rh])
            continue
        times = [tl_to_src(plan, total * (i + 0.5) / 4) for i in range(4)]
        for b in _faces_at(det, Path(path), times):
            out.append([rx + b[0] * rw - pad, ry + b[1] * rh - pad, rx + b[2] * rw + pad, ry + b[3] * rh + pad])
    return out


def _write_ass(path: Path, W: int, H: int, font: str, size: int, events: list) -> None:
    """A small ASS file (one style 'B': white, black outline) for the brand handle and the card texts."""
    path.write_text(
        "\n".join(
            [
                "[Script Info]",
                "ScriptType: v4.00+",
                f"PlayResX: {W}",
                f"PlayResY: {H}",
                "WrapStyle: 0",
                "ScaledBorderAndShadow: yes",
                "YCbCr Matrix: TV.709",
                "",
                "[V4+ Styles]",
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
                "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
                "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
                f"Style: B,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,"
                f"-1,0,0,0,100,100,0,0,1,{max(3, size // 14)},2,5,80,80,40,1",
                "",
                "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
                *events,
                "",
            ]
        ),
        encoding="utf-8",
    )


def _brand_graph(
    g: list,
    kit: dict,
    plan: dict,
    lay: dict,
    cap_info: dict,
    total: float,
    cards: tuple,
    rdir: Path,
    idx0: int,
    stacked: bool,
) -> tuple:
    """
    Video tail with a brand kit: captions, the handle (brand.ass), the logo overlay and the intro/outro cards
    (image or a colour card, text from an ASS file, silent audio) concatenated around the clip. Appends to g
    (ending in [vout] / [aout]); returns (sidecar info, extra ffmpeg inputs).
    """
    W, H = lay["canvas"]
    fps = RENDER_FPS
    avoid = [[s[2], s[3], s[4], s[5]] for s in cap_info.get("spans") or []]
    faces = _brand_faces(plan, lay, total, stacked)
    place = plan_brand(kit, W, H, avoid + faces)
    for n in place["notes"]:
        print(f"Brand kit: {n}")
    font = cap_info.get("cap_font") or "Arial"
    inputs, idx = [], idx0
    chain = "[stk]ass=clip.ass:fontsdir=../_fonts"
    if place["handle"]:
        x0, y0, x1, y1 = place["handle"]["box"]
        right = place["handle"]["corner"].endswith("right")
        pos = f"\\an{6 if right else 4}\\pos({x1 if right else x0},{(y0 + y1) // 2})"
        ev = f"Dialogue: 5,{_ass_time(0)},{_ass_time(total)},B,,0,0,0,,{{{pos}}}{_ass_escape(kit['handle'])}"
        _write_ass(rdir / "brand.ass", W, H, font, HANDLE_SIZE, [ev])
        chain += ",ass=brand.ass:fontsdir=../_fonts"
    vlab = "[vbody]" if any(cards) else "[vout]"
    if place["logo"]:
        x0, y0, x1, y1 = place["logo"]["box"]
        inputs += ["-i", str(kit["logo"]["image"])]
        g.append(chain + "[vb0]")
        g.append(
            f"[{idx}:v]scale={x1 - x0}:{y1 - y0},format=rgba,colorchannelmixer=aa={kit['logo']['opacity']:.2f}[lg]"
        )
        g.append(f"[vb0][lg]overlay={x0}:{y0}:format=auto,format=yuv420p{vlab}")
        idx += 1
    else:
        g.append(chain + f",format=yuv420p{vlab}")
    card_info = {}
    for which, n in (("intro", cards[0]), ("outro", cards[1])):
        card = kit.get(which)
        if not n or not card:
            continue
        secs = n / fps
        if card["image"]:
            inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{secs + 0.2:.3f}", "-i", card["image"]]
            src = f"[{idx}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={fps}"
            idx += 1
        else:
            hx = (kit["colours"].get("hook_box") or "#FFE600")[1:]
            src = f"color=c=0x{hx}:s={W}x{H}:r={fps}:d={secs + 0.2:.3f},setsar=1"
        if card["text"]:
            text = _ass_escape(card["text"])
            ev = f"Dialogue: 0,{_ass_time(0)},{_ass_time(secs + 0.2)},B,,0,0,0,,{{\\an5\\pos({W // 2},{H // 2})}}{text}"
            _write_ass(rdir / f"{which}.ass", W, H, font, 92 if H > W else 80, [ev])
            src += f",ass={which}.ass:fontsdir=../_fonts"
        g.append(f"{src},format=yuv420p,trim=end_frame={n},setpts=PTS-STARTPTS[{which}v]")
        g.append(f"anullsrc=r=48000:cl=stereo,atrim=end_sample={n * 48000 // fps},asetpts=PTS-STARTPTS[{which}a]")
        card_info[which] = round(secs, 3)
    if any(cards):
        order = (["intro"] if "intro" in card_info else []) + ["body"] + (["outro"] if "outro" in card_info else [])
        cat = "".join("[vbody][abody]" if k == "body" else f"[{k}v][{k}a]" for k in order)
        g.append(f"{cat}concat=n={len(order)}:v=1:a=1[vout][aout]")
    info = {
        "kit": kit["path"],
        "logo": place["logo"],
        "handle": place["handle"],
        "cards": card_info,
        "notes": place["notes"],
        "font": cap_info.get("cap_font"),
    }
    return info, inputs


def tl_to_src(plan: dict, t: float) -> float:
    """Timeline seconds -> panel (window) seconds through the plan's cut list."""
    segs = plan.get("segments") or []
    for s in segs:
        d = float(s["src_e"]) - float(s["src_s"])
        if float(s["tl_start"]) <= t < float(s["tl_start"]) + d:
            return float(s["src_s"]) + t - float(s["tl_start"])
    return float(segs[-1]["src_e"]) - 0.05 if segs else 0.0


def render_still(plan: dict, t: float, out_jpg: Path, ass_path: Path) -> Path:
    """
    One canvas-size JPG (q=2) of the clip at timeline second t: the same panels and stacking as the
    MP4 render, with ass_path (e.g. a hook-only ASS) burned in. Fonts come from <out>/_render/_fonts.
    """
    out_jpg = Path(out_jpg).resolve()
    ass_path = Path(ass_path).resolve()
    pn = plan["panels"]
    main = Path(pn["main_src"])
    overlay = Path(pn["overlay_src"]) if pn.get("overlay_src") else None
    looped = bool(pn.get("overlay_loop")) and overlay is not None
    src_t = max(0.0, tl_to_src(plan, t))
    inputs = ["-ss", f"{src_t:.3f}", "-i", str(main)]
    g = ["[0:v]setpts=PTS-STARTPTS,setsar=1[m]"]
    if overlay is not None:
        o_t = src_t
        if looped:
            o_t = t % max(0.5, float(pn.get("overlay_dur") or probe_video(overlay)["dur"]))
        inputs += ["-ss", f"{o_t:.3f}", "-i", str(overlay)]
        g.append("[1:v]setpts=PTS-STARTPTS,setsar=1[o]")
        g.append("[m][o]vstack=inputs=2[stk]" if looped else "[o][m]vstack=inputs=2[stk]")
    else:
        g.append("[m]null[stk]")
    fdir = out_jpg.parent / "_render" / "_fonts"
    rel_fonts = os.path.relpath(fdir, ass_path.parent).replace("\\", "/")
    g.append(f"[stk]ass={ass_path.name}:fontsdir={rel_fonts},format=yuvj420p[v]")
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            *inputs,
            "-filter_complex",
            ";".join(g),
            "-map",
            "[v]",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(out_jpg),
        ],
        f"still frame of {plan.get('name') or out_jpg.stem}",
        cwd=ass_path.parent,
    )
    return out_jpg


def mp4_faststart(path: Path) -> tuple:
    """(ok, detail): walk the top-level MP4 atoms; faststart means 'moov' comes before 'mdat'."""
    pos = {}
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        off = 0
        while off + 8 <= size:
            f.seek(off)
            sz, typ = struct.unpack(">I4s", f.read(8))
            if sz == 1:
                sz = struct.unpack(">Q", f.read(8))[0]
            elif sz == 0:
                sz = size - off
            pos.setdefault(typ.decode("latin-1"), off)
            if sz < 8:
                break
            off += sz
    moov, mdat = pos.get("moov"), pos.get("mdat")
    if moov is None or mdat is None:
        return False, "moov/mdat atom not found"
    return moov < mdat, f"moov at {moov}, mdat at {mdat}"
