"""ASS subtitle writer for the MP4 renderer: every caption style and hook banner style."""

import re
from pathlib import Path

from streamclipper.captions.styles import (
    DEFAULT_HOOK_STYLE,
    EMOJI_RE,
    FONT_FILES,
    HOOK_STYLES,
    NEON_PALETTE,
    CaptionStyle,
    get_style,
    style_name,
)
from streamclipper.env import ttf_full_name

# ASS colours are &HBBGGRR&: active word #FFE600, hype words #FF3B30.
ASS_COLOURS = {k: f"&H{hx[5:7]}{hx[3:5]}{hx[1:3]}&" for k, (_, hx) in NEON_PALETTE.items()}


_MARKUP_RE = re.compile(r"\{(yellow|green|red|cyan|orange|pink|purple|white|black):([^{}]+)\}|\*([^*]+)\*")


_POP_TAG = r"\fscx78\fscy78\t(0,90,\fscx110\fscy110)\t(90,160,\fscx100\fscy100)"

_LEGACY_MODES = {"hormozi-pop": "bold-pop", "karaoke-word": "karaoke-highlight", "two-line-stack": "two-line-stack"}


def _has_devanagari(text: str) -> bool:
    return any(0x0900 <= ord(ch) <= 0x097F for ch in str(text or ""))


def _ass_escape(s: str) -> str:
    return str(s).replace("\\", "/").replace("{", "(").replace("}", ")")


def _ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _col(name: str) -> str:
    return ASS_COLOURS.get(name, ASS_COLOURS["white"])


def _style_col(name: str, alpha: str = "00") -> str:
    return f"&H{alpha}{_col(name)[2:-1]}"


def _tokens(text: str, max_chars: int = 16, keep_emoji: bool = True) -> list:
    """Markup ({yellow:WORD}, *WORD*, \\n) -> at most 2 lines of (word, colour) with <= max_chars per line."""
    lines = []
    for raw in str(text).split("\n"):
        toks, pos = [], 0
        for m in _MARKUP_RE.finditer(raw):
            toks += [(w, "white") for w in raw[pos : m.start()].split()]
            toks += [(w, (m.group(1) or "yellow").lower()) for w in (m.group(2) or m.group(3)).split()]
            pos = m.end()
        toks += [(w, "white") for w in raw[pos:].split()]
        if not keep_emoji:
            toks = [(EMOJI_RE.sub("", w), c) for w, c in toks]
            toks = [t for t in toks if t[0]]
        cur, n = [], 0
        for t in toks:
            add = len(t[0]) + (1 if cur else 0)
            if cur and n + add > max_chars:
                lines.append(cur)
                cur, n, add = [], 0, len(t[0])
            cur.append(t)
            n += add
        if cur:
            lines.append(cur)
    if len(lines) > 2:  # rebalance into two lines at the word boundary nearest the middle
        flat = [t for ln in lines for t in ln]
        total = sum(len(w) + 1 for w, _ in flat)
        acc, cut = 0, 1
        for i, (w, _) in enumerate(flat[:-1]):
            acc += len(w) + 1
            if acc >= total / 2:
                cut = i + 1
                break
        lines = [flat[:cut], flat[cut:]]
    return lines


def _join(lines: list, fmt) -> str:
    """fmt(word, colour, index) -> ASS text of one token; lines joined with \\N."""
    out, k = [], 0
    for ln in lines:
        parts = []
        for w, c in ln:
            parts.append(fmt(w, c, k))
            k += 1
        out.append(" ".join(parts))
    return r"\N".join(out)


def _ass_text(text: str, max_chars: int = 16) -> str:
    """CapCut markup ({yellow:WORD}, *WORD*, \\n) -> ASS text, at most 2 lines of <= max_chars."""

    def word(w, col, _k):
        w = _ass_escape(w)
        return w if col == "white" else f"{{\\c{ASS_COLOURS[col]}}}{w}{{\\c{ASS_COLOURS['white']}}}"

    return _join(_tokens(text, max_chars), word)


def _est_top(align: int, y: float, n_lines: int, size: float) -> float:
    h = n_lines * size * 1.2
    return y if align == 8 else (y - h / 2 if align == 5 else y - h)


def plan_style(plan: dict) -> CaptionStyle:
    """The caption style of a clip plan (older plans only have caption_mode)."""
    name = style_name(plan.get("caption_style") or "") or _LEGACY_MODES.get(plan.get("caption_mode") or "", "")
    return get_style(name)


class _Fonts:
    def __init__(self, fonts_dir: Path, style: CaptionStyle, plan: dict, caps: list, hooks: list):
        def full(key):
            return ttf_full_name(Path(fonts_dir) / FONT_FILES.get(key, FONT_FILES["barlow"])) or ""

        opts = plan.get("caption_opts") or {}
        key = opts.get("font") or style.font
        if not (plan.get("caption_style") or opts.get("font")) and plan.get("font"):
            key = plan["font"]  # plans written before caption styles
        any_dev = any(_has_devanagari(c.get("text")) for c in caps)
        self.cap = full("baloo" if any_dev else key) or full("barlow") or "Arial"
        hook_dev = any(_has_devanagari(c.get("text")) for c in hooks)
        self.hook = full("baloo" if hook_dev else "anton") or "Arial"
        self.emoji = full("emoji") if (Path(fonts_dir) / FONT_FILES["emoji"]).is_file() else ""


def build_ass(plan: dict, lay: dict, out_path: Path, fonts_dir: Path, brand: dict = None) -> dict:
    """
    Write the ASS subtitle file for a ClipPlan; returns caption facts for the timeline sidecar.
    brand (a brand kit, render.py): {font: full font name, hook_box: '#RRGGBB'} for the caption font and the
    hook box / top bar colour.
    """
    brand = brand or {}
    from streamclipper.layout import caption_anchor

    W, H = lay["canvas"]
    sq = H == 1080
    k = 0.76 if sq else 1.0
    st = plan_style(plan)
    hstyle = plan.get("hook_style") or DEFAULT_HOOK_STYLE
    if hstyle not in HOOK_STYLES:
        hstyle = DEFAULT_HOOK_STYLE
    copts = plan.get("caption_opts") or {}
    caps = [c for c in plan.get("captions") or [] if not c.get("capcut_only")]
    hook = plan.get("hook_text") or ""

    segs = plan.get("segments") or []
    tease_end = 0.0
    if segs and segs[0].get("kind") == "tease":
        tease_end = segs[0]["tl_start"] + segs[0]["src_e"] - segs[0]["src_s"]
    hooks, words = [], []
    for c in caps:
        is_hook = c.get("layer") == 1 or (tease_end > 0 and hook and c["tl_end"] <= tease_end + 0.02)
        (hooks if is_hook else words).append(c)
    hook_end = max([c["tl_end"] for c in hooks], default=0.0)
    fonts = _Fonts(fonts_dir, st, plan, words, hooks)
    if brand.get("font") and not any(_has_devanagari(c.get("text")) for c in words):
        fonts.cap = brand["font"]
    box_hex = brand.get("hook_box") or ""
    hook_box_col = _hex_col(box_hex) if box_hex else _style_col("yellow")
    bar_col = _hex_col(box_hex) if box_hex else _style_col("red")
    cap_size = round(st.size * k)
    hook_size = round((80 if hstyle == "top-bar" else 96) * k)
    X = W // 2

    align, y = caption_anchor(lay, copts.get("position") or "auto", st.anim_up)
    hook_a = (lay.get("hook") or {}).get("align", lay["caption"]["align"])
    hook_y = (lay.get("hook") or {}).get("y", lay["caption"]["y"])
    events, tops = [], []
    spans = []  # [s, e, x0, y0, x1, y1]: where each caption/hook shows (estimated box; the export gate's face check)
    texts = []  # plain words of each span (None for a hook banner): the export gate's caption coverage

    def span(s, e, top, h, lines, size):
        chars = max((sum(len(w) for w, _ in ln) + len(ln) - 1 for ln in lines), default=0)
        w = min(W, chars * size * 0.6 + 60)
        spans.append([round(s, 3), round(e, 3), round((W - w) / 2), round(top), round((W + w) / 2), round(top + h)])

    def ev(layer, s, e, style, text):
        if e - s >= 0.01:
            events.append(f"Dialogue: {layer},{_ass_time(s)},{_ass_time(e)},{style},,0,0,0,,{text}")

    # Hook banner
    hook_bottom = None
    for c in hooks:
        s, e = float(c["tl_start"]), float(c["tl_end"])
        lines = _tokens(c["text"], 16, keep_emoji=False)
        n = max(1, len(lines))
        h = n * hook_size * 1.2
        if hstyle == "top-bar":
            pad = 26
            bar_h = int(h + 2 * pad)
            frame = (lay.get("rects") or {}).get("frame")
            bar_top = (frame[1] - bar_h) if frame else int((lay.get("hook") or {}).get("bar_top", 160))
            ev(3, s, e, "Bar", f"{{\\an7\\pos(0,{bar_top})\\p1}}m 0 0 l {W} 0 l {W} {bar_h} l 0 {bar_h}{{\\p0}}")
            txt = _join(lines, lambda w, col, _k: _word(w, "yellow" if col != "white" else "white"))
            ev(4, s, e, "Hook", f"{{\\an8\\pos({X},{bar_top + pad})\\bord4\\shad0}}{txt}")
            tops.append(bar_top)
            spans.append([round(s, 3), round(e, 3), 0, bar_top, W, bar_top + bar_h])
            texts.append(None)
            hook_bottom = max(hook_bottom or 0, bar_top + bar_h)
            continue
        if hstyle == "bold-box":

            def on_box(w, col, _k):
                w = _ass_escape(w)
                return w if col == "white" else f"{{\\c{ASS_COLOURS['red']}}}{w}{{\\c{ASS_COLOURS['black']}}}"

            ev(4, s, e, "HookBox", f"{{\\an{hook_a}\\pos({X},{hook_y}){_POP_TAG}}}{_join(lines, on_box)}")
            pad = 18
        else:  # outline-glow
            plain = _join(lines, lambda w, col, _k: _ass_escape(w))
            glow = r"\blur10\bord14\3c" + _col("yellow") + r"\1a&HFF&\4a&HFF&"
            ev(3, s, e, "Hook", f"{{\\an{hook_a}\\pos({X},{hook_y}){glow}{_POP_TAG}}}{plain}")
            txt = _join(lines, lambda w, col, _k: _word(w, col))
            ev(4, s, e, "Hook", f"{{\\an{hook_a}\\pos({X},{hook_y}){_POP_TAG}}}{txt}")
            pad = 14
        top = _est_top(hook_a, hook_y, n, hook_size) - pad
        tops.append(top)
        span(s, e, top, h + 2 * pad, lines, hook_size)
        texts.append(None)
        hook_bottom = max(hook_bottom or 0, top + h + 2 * pad)

    # Word captions
    for c in words:
        s, e = float(c["tl_start"]), float(c["tl_end"])
        text = c.get("full") or c["text"]
        lines = _tokens(text, 16, keep_emoji=bool(fonts.emoji))
        if not lines:
            continue
        cap_h = len(lines) * cap_size * 1.2
        cy = y
        if hooks and s < hook_end - 0.01:
            if tease_end > 0:
                continue  # the tease shows only the hook headline
            cap_top = _est_top(align, y, len(lines), cap_size) - st.anim_up
            if hook_bottom is not None and cap_top < hook_bottom + 20 and (hstyle == "top-bar" or hook_a == align):
                # same place as the hook banner: move the caption just below it
                new_top = hook_bottom + 24 + st.anim_up
                cy = int(new_top if align == 8 else (new_top + cap_h / 2 if align == 5 else new_top + cap_h))
        tops.append(_est_top(align, cy, len(lines), cap_size) - st.anim_up)
        span(s, e, tops[-1], cap_h + st.anim_up, lines, cap_size)
        texts.append(" ".join(w for ln in _tokens(c["text"], 999, keep_emoji=False) for w, _ in ln))
        start = c.get("anim", "none") != "none"
        active = copts.get("highlight") or st.active
        _caption_events(ev, st, s, e, align, X, cy, lines, start, c.get("reveal"), fonts, active)

    style_fmt = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    prim = _style_col(st.primary)
    box_col = _style_col(copts.get("highlight") or st.active)
    doc = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {W}",
            f"PlayResY: {H}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "",
            "[V4+ Styles]",
            style_fmt,
            f"Style: Cap,{fonts.cap},{cap_size},{prim},{prim},&H00000000,&H96000000,"
            f"-1,0,0,0,100,100,0,0,1,{st.outline},{st.shadow},8,40,40,40,1",
            f"Style: CapBox,{fonts.cap},{cap_size},{prim},{prim},{box_col},&H00000000,"
            "-1,0,0,0,100,100,0,0,3,12,0,8,40,40,40,1",
            f"Style: Hook,{fonts.hook},{hook_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,"
            "-1,0,0,0,100,100,0,0,1,7,3,8,40,40,40,1",
            f"Style: HookBox,{fonts.hook},{hook_size},{_style_col('black')},{_style_col('black')},"
            f"{hook_box_col},&H00000000,-1,0,0,0,100,100,0,0,3,18,0,8,40,40,40,1",
            f"Style: Bar,{fonts.hook},{hook_size},{bar_col},{bar_col},&H00000000,&H00000000,"
            "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events,
            "",
        ]
    )
    out_path.write_text(doc, encoding="utf-8")
    top_min = round(min(tops)) if tops else None
    return {
        "align": align,
        "y": y,
        "top_min": top_min,
        "events": len(events),
        "cap_font": fonts.cap,
        "hook_font": fonts.hook,
        "caption_style": st.name,
        "hook_style": hstyle,
        "emoji_font": bool(fonts.emoji),
        "spans": spans,
        "texts": texts,
    }


def _hex_col(hex_col: str, alpha: str = "00") -> str:
    """#RRGGBB -> ASS style colour &HAABBGGRR."""
    return f"&H{alpha}{hex_col[5:7]}{hex_col[3:5]}{hex_col[1:3]}".upper()


def _word(w: str, col: str) -> str:
    w = _ass_escape(w)
    return w if col == "white" else f"{{\\c{ASS_COLOURS[col]}}}{w}{{\\c{ASS_COLOURS['white']}}}"


def _caption_events(ev, st: CaptionStyle, s, e, align, X, y, lines, start: bool, reveal, fonts: _Fonts, active: str):
    """Dialogue lines for one word caption in style st (layers 0-2; hooks use 3-4)."""
    emoji_on = r"{\fn" + fonts.emoji + "}" if fonts.emoji else ""
    emoji_off = r"{\fn" + fonts.cap + "}"
    white = ASS_COLOURS["white"] if st.primary == "white" else ASS_COLOURS[st.primary]

    def tok(w, col, kk, hidden_from=None, colours=True):
        txt = _ass_escape(w)
        if EMOJI_RE.fullmatch(w) and fonts.emoji:
            txt = f"{emoji_on}{txt}{emoji_off}"
        if hidden_from is not None and kk == hidden_from:
            txt = r"{\alpha&HFF&}" + txt
        if not colours or col == "white":
            return txt
        return f"{{\\c{ASS_COLOURS[col]}}}{txt}{{\\c{white}}}"

    def normal(kk_hidden=None):
        def f(w, col, kk):
            return tok(w, col, kk, kk_hidden)

        return _join(lines, f)

    pos = f"\\an{align}\\pos({X},{y})"
    pop = _POP_TAG if start else ""
    a = st.anim
    if a == "typewriter":
        ev(1, s, e, "Cap", f"{{{pos}}}{normal(reveal if reveal else None)}")
    elif a == "neon":
        plain = _join(lines, lambda w, col, kk: tok(w, col, kk, colours=False))
        glow = r"\blur6\bord6\shad0\3c" + _col(st.active) + r"\1c" + _col(st.active) + r"\alpha&H60&"
        ev(0, s, e, "Cap", f"{{{pos}{glow}{pop}}}{plain}")
        ev(1, s, e, "Cap", f"{{{pos}{pop}}}{normal()}")
    elif a == "gradient":
        plain = _join(lines, lambda w, col, kk: tok(w, col, kk, colours=False))
        ev(0, s, e, "Cap", f"{{{pos}\\bord14\\shad3\\3c{_col('pink')}{pop}}}{plain}")
        ev(1, s, e, "Cap", f"{{{pos}\\bord7\\shad0\\3c{_col('orange')}{pop}}}{normal()}")
    elif st.box:
        box_c = active if any(c == active for ln in lines for _, c in ln) else None

        def boxed(w, col, kk):
            txt = _ass_escape(w)
            if col == box_c:
                return r"{\1a&HFF&\3a&H00&}" + txt + r"{\alpha&HFF&}"
            return txt

        def text_on_box(w, col, kk):
            txt = _ass_escape(w)
            if col == box_c:
                return f"{{\\c{ASS_COLOURS['black']}\\3c{ASS_COLOURS[col]}}}{txt}{{\\c{white}\\3c&H000000&}}"
            return tok(w, col, kk)

        if box_c:
            ev(0, s, e, "CapBox", f"{{{pos}\\alpha&HFF&}}" + _join(lines, boxed))
        ev(1, s, e, "Cap", f"{{{pos}}}" + _join(lines, text_on_box))
    elif a == "shake" and start and e - s > 0.16:
        for i, dx in enumerate((8, -8, 4)):
            ev(1, s + 0.04 * i, s + 0.04 * (i + 1), "Cap", f"{{\\an{align}\\pos({X + dx},{y})}}{normal()}")
        ev(1, s + 0.12, e, "Cap", f"{{{pos}}}{normal()}")
    elif a == "bounce" and start and e - s > 0.16:
        up = st.anim_up
        ev(1, s, s + 0.1, "Cap", f"{{\\an{align}\\move({X},{y + 30},{X},{y - up},0,100)}}{normal()}")
        ev(1, s + 0.1, e, "Cap", f"{{\\an{align}\\move({X},{y - up},{X},{y},0,60)}}{normal()}")
    elif a == "burst":
        ev(1, s, e, "Cap", f"{{{pos}\\frz-4{pop}}}{normal()}")
    elif a == "glitch":
        if start:
            g_end = min(e, s + 0.15)
            plain = _join(lines, lambda w, col, kk: tok(w, col, kk, colours=False))
            for layer, dx, col in ((0, -4, "cyan"), (1, 4, "pink")):
                ev(
                    layer,
                    s,
                    g_end,
                    "Cap",
                    f"{{\\an{align}\\pos({X + dx},{y})\\c{_col(col)}\\3c{_col(col)}\\alpha&H40&\\bord2\\shad0}}{plain}",
                )
        ev(2, s, e, "Cap", f"{{{pos}{pop}}}{normal()}")
    else:  # pop, stack, none (and shake/bounce on short captions)
        ev(1, s, e, "Cap", f"{{{pos}{pop if a in ('pop', 'stack', 'shake', 'bounce') else ''}}}{normal()}")
