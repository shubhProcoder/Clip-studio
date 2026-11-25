import pytest

from streamclipper.fillers import MAX_CUTS, cut_fillers, filler_spans, protect_zones, remove_fillers


def _words(spec):
    """'text@start-end ...' -> words."""
    out = []
    for tok in spec.split():
        w, t = tok.rsplit("@", 1)
        a, b = t.split("-")
        out.append({"w": w, "s": float(a), "e": float(b)})
    return out


def _inside_any_word(t, words):
    return any(w["s"] + 1e-9 < t < w["e"] - 1e-9 for w in words)


def test_um_and_uh_are_cut_between_the_words():
    ws = _words("so@10.0-10.2 um@10.4-10.6 we@10.8-11.0 went@11.0-11.3 uh,@11.5-11.7 home@11.9-12.2")
    spans = filler_spans(ws)
    assert [s[2] for s in spans] == ["um", "uh,"]
    a, b, _ = spans[0]
    assert a == pytest.approx(10.28) and b == pytest.approx(10.72)  # the filler + 0.12 s of the pause each side


def test_like_as_a_verb_stays_like_comma_goes():
    assert filler_spans(_words("i@1.0-1.2 like@1.2-1.5 it@1.5-1.7")) == []
    assert filler_spans(_words("it@1.0-1.2 looks@1.2-1.5 like@1.7-1.9 rain@2.1-2.4")) == []
    spans = filler_spans(_words("it@1.0-1.2 was@1.2-1.4 like,@1.5-1.7 crazy@1.9-2.3"))
    assert [s[2] for s in spans] == ["like,"]


def test_you_know_as_a_filler_only():
    assert filler_spans(_words("do@1.0-1.2 you@1.2-1.4 know@1.4-1.6 him@1.7-1.9")) == []
    assert filler_spans(_words("you@1.0-1.2 know@1.2-1.4 what@1.4-1.6 happened@1.6-2.0")) == []
    spans = filler_spans(_words("and@1.0-1.2 you@1.3-1.5 know,@1.5-1.7 then@2.1-2.4"))
    assert [s[2] for s in spans] == ["you know,"]


def test_no_cut_starts_or_ends_inside_a_word_even_when_words_touch():
    ws = _words("we@5.0-5.3 um@5.3-5.6 like,@5.6-5.9 go@5.9-6.2 erm@6.2-6.5 now@6.5-6.9")
    spans = filler_spans(ws)
    assert spans
    kept = [w for w in ws if w["w"] not in ("um", "like,", "erm")]
    for a, b, _ in spans:
        assert not _inside_any_word(a, kept) and not _inside_any_word(b, kept)
        assert not any(w["s"] < b and w["e"] > a for w in kept)  # no kept word overlaps a cut


def test_protected_zones_and_dramatic_pause():
    ws = _words("so@20.0-20.2 um@20.4-20.6 look@20.8-21.0 uh@30.0-30.2 wait@31.2-31.5")
    # the payoff zone (peak 21.0 -> 18.0..22.5) keeps its filler; 'uh' before a 1 s pause is a beat
    segs = [{"cut_start": 5.0, "cut_end": 40.0, "kind": "story", "words": ws}]
    assert filler_spans(ws, protect_zones(segs, peak=21.0)) == []


def test_first_two_seconds_are_protected():
    ws = _words("um@10.2-10.4 so@10.6-10.8 um@13.0-13.2 yes@13.4-13.7")
    segs = [{"cut_start": 10.0, "cut_end": 20.0, "kind": "story", "words": ws}]
    spans = filler_spans(ws, protect_zones(segs))
    assert [round(s[0], 2) for s in spans] == [12.88]


def test_at_most_eight_cuts():
    toks = " ".join(f"go@{i:.1f}-{i + 0.2:.1f} um@{i + 0.4:.1f}-{i + 0.6:.1f}" for i in range(0, 40))
    assert len(filler_spans(_words(toks))) == MAX_CUTS


def test_cut_fillers_splits_pieces_drops_tiny_parts_and_keeps_times():
    ws = _words("a@0.5-0.8 um@1.0-1.2 b@1.4-1.7 c@2.0-2.3 uh@2.32-2.4 d@2.6-3.0")
    segs = [
        {"cut_start": 9.0, "cut_end": 10.4, "kind": "tease", "words": []},
        {"cut_start": 0.0, "cut_end": 3.2, "kind": "story", "words": ws},
    ]
    out = cut_fillers(segs, [(0.82, 1.38, "um"), (2.3, 2.58, "uh")])
    assert out[0]["kind"] == "tease" and out[0]["cut_start"] == 9.0  # the tease is not touched
    story = out[1:]
    assert [(round(p["cut_start"], 2), round(p["cut_end"], 2)) for p in story] == [
        (0.0, 0.82),
        (1.38, 2.3),
        (2.58, 3.2),
    ]
    assert "after_filler" not in story[0] and story[1]["after_filler"] and story[2]["after_filler"]
    # words keep their source times; the fillers are gone
    assert [w["w"] for p in story for w in p["words"]] == ["a", "b", "c", "d"]
    assert story[1]["words"][0]["s"] == 1.4
    tiny = cut_fillers([{"cut_start": 0.0, "cut_end": 1.0, "kind": "story", "words": []}], [(0.2, 0.9, "um")])
    assert tiny == []  # both parts are shorter than 0.3 s


def test_remove_fillers_end_to_end():
    ws = _words("hey@0.2-0.5 guys@0.5-0.8 so@3.0-3.2 um@3.4-3.6 this@3.8-4.0 is@4.0-4.1 nuts@4.2-4.6")
    segs = [{"cut_start": 0.0, "cut_end": 5.0, "kind": "story", "words": ws}]
    new, removed = remove_fillers(segs, ws, peak=None)
    assert [w["w"] for w in removed] == ["um"]
    assert len(new) == 2 and new[0]["cut_end"] <= 3.4 and new[1]["cut_start"] >= 3.6
    same, none = remove_fillers(segs, _words("no@1.0-1.2 fillers@1.3-1.8"), None)
    assert same is segs and none == []


def test_cuts_are_snapped_inwards_onto_the_frame_grid():
    from streamclipper.fillers import snap_spans

    ws = _words("play,@13.04-13.14 um@13.14-13.40 then@13.40-13.70")
    ((a, b, _),) = snap_spans(filler_spans(ws), ws, origin=0.0)
    assert a >= 13.14 and b <= 13.40 and abs(a * 30 - round(a * 30)) < 0.01 and abs(b * 30 - round(b * 30)) < 0.01
    assert snap_spans([(1.01, 1.06, "um")], ws, origin=0.0) == []  # too short once on the grid
