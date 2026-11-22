from streamclipper.cli import build_parser
from streamclipper.discovery import select
from streamclipper.scoring import DEFAULT_BAR, WINDOW_S, per_window, select_clips


def _c(start, end, score):
    return {"start": start, "end": end, "score": score}


CANDS = [_c(0, 20, 60), _c(15, 35, 80), _c(40, 60, 70), _c(70, 90, 40), _c(100, 120, 55), _c(110, 130, 90)]


def test_every_clip_mode_is_ranked_and_never_overlaps():
    got = select_clips(CANDS, min_score=50, max_clips=0)
    assert [c["score"] for c in got] == [90, 80, 70]
    for i, a in enumerate(got):
        for b in got[i + 1 :]:
            assert not (a["start"] < b["end"] and b["start"] < a["end"])


def test_min_score_and_max_clips():
    assert all(c["score"] >= 75 for c in select_clips(CANDS, min_score=75))
    assert len(select_clips(CANDS, min_score=0, max_clips=2)) == 2
    assert select_clips(CANDS, min_score=95) == []
    # with a low threshold the 40-point clip is kept too
    assert 40 in [c["score"] for c in select_clips(CANDS, min_score=0)]


def _cand(start, score, **sig):
    comp = {"hook": 0.6, "story": 0.6, "emotion": 0.6, "pacing": 0.6}
    signals = {"arc_contrast": 0.8, "reaction": 0.8, "laughter": 0.0, "excitement_db": 18.0, **sig}
    return {
        "start": start,
        "end": start + 20,
        "peak": start + 12,
        "score": score,
        "comp": comp,
        "signals": signals,
        "text": "okay we are going in now and then he just walks right into the trap no way",
        "llm": None,
        "sources": ["voice"],
        "shock_contrast": 10.0,
    }


def test_select_applies_the_bar_and_min_score_zero_lists_everything():
    chant = dict(_cand(300, 90), text="I know what to do " * 10)
    quiet = _cand(400, 75, excitement_db=8.0)  # payoff not above the usual voice level
    scream = dict(_cand(500, 45, excitement_db=26.0), text="")  # a scream with no words, lower floor
    cands = [_cand(0, 80), _cand(100, 40), _cand(200, 70), chant, quiet, scream]
    got = select(cands, {"min_score": 50, "max_clips": 0})
    assert [c["score"] for c in got] == [80, 70, 45] and [c["rank"] for c in got] == [1, 2, 3]
    assert len(select(cands, {"min_score": 0, "max_clips": 0})) == 6
    assert len(select(cands, {"min_score": 50, "max_clips": 1})) == 1  # a cap, not a target


def test_custom_rank_key():
    got = select_clips(CANDS, min_score=0, key=lambda c: c["start"])
    assert got[0]["start"] == 110


def test_per_window_cap_spreads_clips_across_the_stream():
    """A loud stretch cannot take the whole budget: each 15-minute window has its own cap."""
    assert per_window(0, 8) == 0  # every-clip mode is never capped
    assert per_window(6, 1) == 8  # the formula, capped at 8
    assert per_window(6, 6) == 3  # ceil(9/6) + 1
    assert per_window(20, 32) == 2  # a long stream: max(2, ...)
    hot = [_c(i * 30, 20 + i * 30, 90 - i) for i in range(6)]  # six high scores in the first 15 minutes
    spread = [_c(w * 900, w * 900 + 20, 60) for w in range(1, 4)]
    got = select_clips(hot + spread, min_score=0, max_clips=6, cap=per_window(6, 6))
    assert len(got) == 6 and len({int(c["start"] // WINDOW_S) for c in got}) == 4
    # without the cap the loud stretch takes every slot
    assert len({int(c["start"] // WINDOW_S) for c in select_clips(hot + spread, 0, 6)}) == 1


def test_cli_defaults_and_top_alias():
    ap = build_parser()
    dc = ap.parse_args(["discover-clips", "--video", "s.mp4", "--out", "o"])
    assert dc.max_clips == 0 and dc.min_score == DEFAULT_BAR["min_score"] and dc.llm == "none" and dc.chat == ""
    assert ap.parse_args(["discover-clips", "--video", "s.mp4", "--out", "o", "--top", "4"]).max_clips == 4
    cs = ap.parse_args(["clip-stream", "--input", "s.mp4", "--out", "o"])
    assert cs.max_clips == 3
    assert ap.parse_args(["clip-stream", "--input", "s.mp4", "--out", "o", "--top-k", "0"]).max_clips == 0
    rn = ap.parse_args(["render", "--clips", "c.json", "--video", "s.mp4"])
    assert rn.max_clips == 0
    assert ap.parse_args(["render", "--clips", "c.json", "--video", "s.mp4", "--top", "1"]).max_clips == 1
