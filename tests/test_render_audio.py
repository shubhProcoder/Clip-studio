import shutil
import subprocess

import pytest

from streamclipper.render import (
    COMP_STAGES,
    MAX_LIMITING_DB,
    aac_ceiling,
    comp_filter,
    limiting_db,
    next_stage,
    sfx_volume_cap,
    sustained_limiting,
)


def test_limiting_is_peak_above_the_limiter_ceiling():
    assert limiting_db(None) == 0.0
    assert limiting_db(-5.0) == 0.0
    assert abs(limiting_db(2.5) - 5.5) < 1e-9


def test_gain_staging_steps_up_only_when_limiting_exceeds_6_db():
    assert next_stage(0, MAX_LIMITING_DB) is None  # exactly 6 dB: fine
    assert next_stage(0, 7.0) == 1  # 7 dB of limiting -> compressor stage 1
    assert next_stage(1, 9.0) == 2
    assert next_stage(len(COMP_STAGES) - 1, 20.0) is None  # last stage: keep it
    assert comp_filter(0) == ""
    thr = []
    for k in range(1, len(COMP_STAGES)):
        comp = comp_filter(k)
        assert "ratio=" in comp and "attack=5" in comp and "release=120" in comp
        thr.append(float(comp.split("threshold=")[1].split(":")[0]))
    assert thr == sorted(thr, reverse=True)  # each stage compresses more


def test_sfx_stays_6_db_under_the_voice():
    # voice lane -20 LUFS, SFX file -10 LUFS: volume must give <= -26 LUFS -> <= 10^(-16/20)
    v = sfx_volume_cap(0.3, -20.0, -10.0)
    assert v <= 10 ** (-16 / 20) + 1e-4
    assert sfx_volume_cap(0.3, -10.0, -30.0) == 0.3  # quiet SFX keep their volume
    assert sfx_volume_cap(0.25, None, -10.0) == 0.25


def test_sustained_limiting_ignores_quiet_frames_and_transients():
    pre = [-60.0, -20.0, -8.0, -5.0, -12.0]
    post = [-60.0, -20.0, -9.0, -11.5, -12.0]
    assert sustained_limiting(pre, post) == 6.5
    assert sustained_limiting([-50.0], [-80.0]) == 0.0  # silence does not count
    assert sustained_limiting([], []) == 0.0


def test_aac_overshoot_lowers_the_limiter_ceiling():
    assert aac_ceiling(-3.0, -2.0) == -3.0
    assert aac_ceiling(-3.0, None) == -3.0
    assert aac_ceiling(-3.0, 0.2) == round(-3.0 - 1.4 - 0.3, 2)
    # repeated drops stop at the floor (the render loop then ends: the ceiling no longer changes)
    assert aac_ceiling(-5.5, 0.5) == -6.0 and aac_ceiling(-6.0, 0.5) == -6.0


def test_sustained_limiting_aligns_logs_that_differ_by_a_frame():
    pre = [-20.0] * 10 + [-6.0] * 10 + [-20.0] * 10
    post = [-120.0] + [-20.0] * 10 + [-9.0] * 10 + [-20.0] * 10 + [-150.0]  # one frame late, odd edges
    assert sustained_limiting(pre, post) == 3.0


def test_speech_enhancement_only_when_enabled():
    from streamclipper.render import RNNOISE_MODEL, _audio_graph, enhance_mix

    assert enhance_mix("off") is None and enhance_mix(None) is None
    assert enhance_mix("light") == 0.6 and enhance_mix("strong") == 0.9
    assert RNNOISE_MODEL.is_file() and RNNOISE_MODEL.read_bytes().startswith(b"rnnoise-nu model file")
    lanes = [{"a0": 0, "end": 60, "idx": [0]}]
    segs = [{"a": 0, "n": 60, "fade_ms": 0}]
    plain = _audio_graph(lanes, segs, {0: 0}, True, [], 1)[0]
    enh = _audio_graph(lanes, segs, {0: 0}, True, [], 1, 0.6)[0]
    assert "arnndn" not in "".join(plain)
    assert plain[0] == "[0:a]aformat=sample_rates=48000:channel_layouts=stereo,apad=pad_dur=0.5,asplit=1[as0]"
    text = ";".join(enh)
    # fully wet arnndn + the dry signal delayed by the same 10 ms (arnndn's own mix would comb), then re-synced
    assert "arnndn=m=../_models/bd.rnnn:mix=1" in text and "adelay=delays=480S|480S" in text
    assert "weights='0.6 0.4'" in text and "atrim=start_sample=480" in text and text.endswith(plain[-1].split(";")[-1])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_enhanced_audio_stays_in_sync_and_runs_with_the_bundled_model(tmp_path):
    import numpy as np

    from streamclipper.render import RNNOISE_MODEL, enhance_lines

    (tmp_path / "_models").mkdir()
    shutil.copy2(RNNOISE_MODEL, tmp_path / "_models" / RNNOISE_MODEL.name)
    work = tmp_path / "clip"
    work.mkdir()
    lines, head = enhance_lines("[0:a]aformat=sample_rates=48000:channel_layouts=mono,", 0.6, "t")
    graph = ";".join(lines + [f"{head}anull[out]"])
    # a click every 0.5 s in noise: after enhancement the clicks must still sit at the same samples
    src = "aevalsrc='if(lt(mod(n,24000),48),0.8,0)+0.02*random(0)':s=48000:d=3"
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", src, "-filter_complex", graph]
        + ["-map", "[out]", "-ac", "1", "-f", "f32le", "-"],
        cwd=work,
        capture_output=True,
    )
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    y = np.frombuffer(r.stdout, np.float32)
    assert abs(len(y) - 3 * 48000) <= 480
    peak = int(np.argmax(np.abs(y[20000:30000]))) + 20000
    assert abs(peak - 24000) <= 48, peak  # within 1 ms of the original click
