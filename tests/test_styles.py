import json
import re

import pytest

from streamclipper.captions.ass import build_ass
from streamclipper.captions.capcut import build_word_synced_captions, parse_multicolor_styles
from streamclipper.captions.styles import (
    HOOK_STYLES,
    STYLE_ALIASES,
    STYLES,
    apply_case,
    get_style,
    mask_profanity,
    style_name,
)
from streamclipper.layout import layout_boxes
from streamclipper.paths import TEMPLATE_DIR
from streamclipper.util import utf16_len

FONTS = TEMPLATE_DIR / "fonts"
TEXT = "okay so the cops are here no way bro that was fire let's go"


def _words(text=TEXT, gap=0.05):
    out, t = [], 0.5
    for w in text.split():
        out.append({"w": w, "s": round(t, 3), "e": round(t + 0.3, 3)})
        t += 0.3 + gap
    return out


def _caps(style, **kw):
    return build_word_synced_captions(_words(), 0.0, 30.0, 0.0, style=style, **kw)


def _plan(style, caps, hook_style="bold-box", hook="NO WAY BRO"):
    hook_cap = {"text": hook, "tl_start": 0.0, "tl_end": 2.0, "layer": 1}
    return {
        "caption_style": style,
        "caption_opts": {"position": "auto"},
        "hook_style": hook_style,
        "hook_text": hook,
        "captions": [hook_cap] + caps,
        "segments": [{"src_s": 0.0, "src_e": 30.0, "tl_start": 0.0, "kind": "story"}],
    }


def _dialogue_texts(doc):
    return [ln.split(",", 9)[9] for ln in doc.splitlines() if ln.startswith("Dialogue:")]


def test_full_layout_captions_leave_a_low_face_clear(tmp_path):
    from streamclipper.gate import caption_boxes, captions_cover_face
    from streamclipper.layout import FULL_ANCHORS, FULL_BANDS, apply_caption_anchor, face_shots, speaker_caption_zone

    low = [0.3, 0.62, 0.7, 0.8]  # face-centred crop of a keyed streamer: the face sits low in the frame
    shots = face_shots([[low]] * 6 + [[]] * 2)
    assert speaker_caption_zone(shots, FULL_BANDS) == "upper"
    assert speaker_caption_zone(face_shots([[[0.3, 0.1, 0.7, 0.25]]] * 8), FULL_BANDS) == "lower"
    assert speaker_caption_zone(face_shots([[]] * 8), FULL_BANDS) == "lower"
    face_px = shots[0]["faces_out"]
    caps = _caps("bold-pop")
    before = build_ass(_plan("bold-pop", caps), layout_boxes("9:16", "full"), tmp_path / "a.ass", FONTS)
    assert captions_cover_face(face_px, caption_boxes({"caption": before, "canvas": [1080, 1920]}))
    lay = apply_caption_anchor(layout_boxes("9:16", "full"), FULL_ANCHORS["upper"])
    assert lay["hook"]["y"] == lay["caption"]["y"] == FULL_ANCHORS["upper"][1]  # the hook moves with them
    after = build_ass(_plan("bold-pop", caps), lay, tmp_path / "b.ass", FONTS)
    boxes = caption_boxes({"caption": after, "canvas": [1080, 1920]})
    assert len(boxes) > 1  # the hook banner and the word captions
    assert not captions_cover_face(face_px, boxes)
    # a sidecar written before caption spans: one band from top_min down to the anchor, for the whole clip
    old = {k: v for k, v in before.items() if k != "spans"}
    assert captions_cover_face(
        face_px, caption_boxes({"caption": old, "canvas": [1080, 1920], "expected_duration": 30})
    )


def test_registry_has_15_styles_and_3_hook_styles():
    assert len(STYLES) == 15
    assert set(HOOK_STYLES) == {"bold-box", "outline-glow", "top-bar"}
    for alias, target in STYLE_ALIASES.items():
        assert style_name(alias) == target
    assert style_name("nope") == ""
    assert get_style("hormozi-pop").name == "bold-pop"


@pytest.mark.parametrize("name", list(STYLES))
def test_every_style_writes_valid_ass(tmp_path, name):
    caps = _caps(name)
    assert caps
    out = tmp_path / "c.ass"
    for hs in HOOK_STYLES:
        info = build_ass(_plan(name, caps, hs), layout_boxes("9:16", "split"), out, FONTS)
        doc = out.read_text(encoding="utf-8")
        for section in ("[Script Info]", "[V4+ Styles]", "[Events]"):
            assert section in doc
        texts = _dialogue_texts(doc)
        assert texts and info["events"] == len(texts)
        for t in texts:
            assert t.count(r"\N") <= 1, t  # at most 2 lines
            assert t.count("{") == t.count("}")
        assert info["caption_style"] == name and info["hook_style"] == hs
        assert info["top_min"] >= 672  # never above the cam panel bottom


def test_caption_case_and_words_per_caption():
    def plain(c):
        return re.sub(r"\{[a-z]+:([^{}]+)\}", r"\1", c["text"])

    up = _caps("bold-pop", case="upper")
    assert all(plain(c) == plain(c).upper() for c in up)
    title = _caps("bold-pop", case="title")
    assert "Cops" in " ".join(plain(c) for c in title)
    assert "Let's" in " ".join(plain(c) for c in title)
    raw = _caps("bold-pop", case="as-is")
    assert "cops" in " ".join(plain(c) for c in raw)
    for n in (1, 2, 3, 4):
        caps = _caps("karaoke-highlight", words_per=n)
        assert max(len(plain(c).split()) for c in caps) <= n
    assert max(len(plain(c).split()) for c in _caps("word-by-word")) == 1
    assert apply_case("भाई bro", "upper") == "भाई BRO"


def test_profanity_mask_keeps_first_letter():
    assert mask_profanity("fucking") == "f******"
    assert mask_profanity("Shit!") == "S***!"
    assert mask_profanity("shoot") == "shoot"
    caps = build_word_synced_captions(_words("what the fuck bro"), 0.0, 9.0, 0.0, style="bold-pop", profanity=True)
    joined = " ".join(c["text"] for c in caps)
    assert "FUCK" not in joined and "F***" in joined
    off = build_word_synced_captions(_words("what the fuck bro"), 0.0, 9.0, 0.0, style="bold-pop")
    assert "FUCK" in " ".join(c["text"] for c in off)


def test_emoji_added_for_keywords_and_dropped_without_font(tmp_path):
    caps = _caps("emoji-pop")
    joined = " ".join(c["text"] for c in caps)
    assert "🚓" in joined and "😱" in joined and "🔥" in joined and "🚀" in joined
    assert "🔥" not in " ".join(c["text"] for c in _caps("bold-pop"))
    assert "🔥" in " ".join(c["text"] for c in _caps("bold-pop", emoji=True))
    out = tmp_path / "c.ass"
    lay = layout_boxes("9:16", "split")
    info = build_ass(_plan("emoji-pop", caps), lay, out, FONTS)
    assert info["emoji_font"] and "Noto Emoji" in out.read_text(encoding="utf-8")
    empty = tmp_path / "nofonts"
    empty.mkdir()
    info = build_ass(_plan("emoji-pop", caps), lay, out, empty)
    doc = out.read_text(encoding="utf-8")
    assert not info["emoji_font"]
    assert not any(e in doc for e in ("🔥", "😱", "🚓", "🚀"))


def test_typewriter_reveals_whole_words():
    caps = _caps("typewriter")
    grp = [c for c in caps if c.get("full")]
    assert grp and all(c["reveal"] == len(c["text"].split()) for c in grp)
    assert all(c["full"].startswith(c["text"].split()[0]) for c in grp)


@pytest.mark.parametrize("name", list(STYLES))
def test_capcut_styles_span_utf16_text(name):
    base = {"range": [0, 0], "size": 11.0}
    caps = _caps(name) + build_word_synced_captions(
        [{"w": "भाई", "s": 0.1, "e": 0.5}, {"w": "fire", "s": 0.6, "e": 0.9}],
        0.0,
        2.0,
        0.0,
        style=name,
        emoji=True,
    )
    for c in caps:
        dev = any(0x0900 <= ord(ch) <= 0x097F for ch in c["text"])
        text, styles, _ = parse_multicolor_styles(
            c["text"],
            base,
            "x.ttf",
            "",
            11.0,
            c.get("stroke_width", 0.07),
            keep_devanagari=dev,
            keep_emoji=bool(c.get("emoji")),
            colour_map=c.get("colour_map"),
        )
        assert styles[0]["range"][0] == 0 and styles[-1]["range"][1] == utf16_len(text), (name, text)
        for a, b in zip(styles, styles[1:], strict=False):
            assert a["range"][1] == b["range"][0]
        for st in styles:
            assert all(sh["diffuse"] <= 0.15 for sh in st["shadows"])
        json.dumps(styles)
    joined = " ".join(c["text"] for c in caps)
    assert "भाई" in joined and "🔥" in joined


def test_glitch_ghosts_are_capcut_only(tmp_path):
    caps = _caps("gaming-glitch")
    ghosts = [c for c in caps if c.get("capcut_only")]
    assert ghosts and {g["layer"] for g in ghosts} == {2, 3}
    assert all(g["tl_end"] - g["tl_start"] <= 0.151 for g in ghosts)


def test_auto_style_maps_categories_and_explicit_names_win():
    from types import SimpleNamespace

    from streamclipper.captions.styles import CATEGORY_STYLES, auto_style, clip_style
    from streamclipper.options import resolve_options

    assert set(CATEGORY_STYLES.values()) <= set(STYLES)
    assert auto_style("funny") == "comic-burst" and auto_style("rage") == "impact-shake"
    assert auto_style("reaction") == "beast-bold" and auto_style("hot take") == "karaoke-highlight"
    assert auto_style("fail") == "bold-pop" and auto_style("") == "bold-pop"
    assert auto_style("funny", "talk") == "karaoke-highlight"
    assert clip_style("auto", "wholesome") == "minimal-clean" and clip_style("", "reveal") == "highlight-box"
    assert clip_style("neon-glow", "funny") == "neon-glow" and clip_style("hormozi-pop", "funny") == "bold-pop"

    def opts(style=None, spec=None, preset=None):
        return resolve_options(SimpleNamespace(caption_style=style, preset=preset), spec or {})["caption_style"]

    assert opts() == "auto"  # the default
    assert opts(spec={"caption_style": "typewriter"}) == "typewriter"
    assert opts(preset="square-frame-9x16") == "bold-pop"  # a preset's own style
    assert opts("auto", preset="square-frame-9x16") == "auto"  # CLI beats the preset
    assert opts("comic-burst", spec={"caption_style": "typewriter"}) == "comic-burst"
    assert opts(preset="podcast-9x16") == "auto"


def test_restyle_plan_rebuilds_word_captions_and_keeps_the_hook():
    from streamclipper.clipplan import restyle_plan

    words = _words("one two three four five six")
    caps = build_word_synced_captions(words, 0.0, 3.0, 0.0, style="word-by-word")
    hook = {"text": "HOOK", "tl_start": 0.0, "tl_end": 2.0, "layer": 1}
    plan = {
        "caption_style": "word-by-word",
        "hook_text": "HOOK",
        "hook_style": "bold-box",
        "words": words,
        "segments": [{"src_s": 0.0, "src_e": 3.0, "tl_start": 0.0, "kind": "story"}],
        "captions": [hook] + caps,
    }
    new = restyle_plan(plan, "karaoke-highlight", "top-bar")
    assert new["caption_style"] == "karaoke-highlight" and new["hook_style"] == "top-bar"
    assert hook in new["captions"] and plan["captions"][1:] == caps  # the input plan is unchanged
    lines = [c["text"] for c in new["captions"] if c["text"] != "HOOK"]
    assert len(lines) == len(caps) and any(len(t.split()) == 3 for t in lines)  # 3-word lines now


def test_report_shows_the_style_and_a_restyle_command(tmp_path):
    from streamclipper.report import restyle_command, write_report

    clip = {"rank": 2, "start": 1.0, "end": 9.0, "duration": 8.0, "score": 70, "peak": 5.0}
    rendered = dict(clip, caption_style="comic-burst", mp4="LIVE_02.mp4")
    assert restyle_command(rendered) == 'streamclipper render --spec "LIVE_02.clip.json" --caption-style bold-pop'
    local = restyle_command(dict(clip, caption_style="bold-pop"), "D:/v/stream.mp4")
    assert local.endswith('--video "D:/v/stream.mp4" --max-clips 2 --caption-style karaoke-highlight')
    assert restyle_command(clip, "https://youtu.be/x").startswith("streamclipper clip-stream --url https://youtu.be/x")
    csv_p, html_p = write_report(tmp_path, {"source": "D:/v/stream.mp4", "clips": [rendered]})
    assert "comic-burst" in csv_p.read_text(encoding="utf-8-sig").splitlines()[1]
    page = html_p.read_text(encoding="utf-8")
    assert "Captions: comic-burst" in page and "--caption-style bold-pop" in page
