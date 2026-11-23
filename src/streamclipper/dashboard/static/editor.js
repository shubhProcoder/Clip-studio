/* StreamClipper dashboard: the reframe editor page (#/edit/<job>/<key>). Uses the helpers of app.js.
   An edit is a list of contiguous segments; each has a layout, crop window(s) and optional pan keyframes.
   The preview canvas mirrors the render maths of reframe.py (window_points / pan_value), so what you see
   is what renders. Saving sends the old single-layout form when there is one segment without keyframes. */
"use strict";

const ED_SHORTCUTS = [
  ["Space", "Editor: play or pause"],
  ["J / K / L", "Editor: back 5 s / pause / forward 5 s (L while playing = 2x)"],
  ["S", "Editor: split at the playhead"],
  ["[ / ]", "Editor: set the start / end at the playhead"],
  ["Delete", "Editor: delete the keyframe at the playhead"],
  ["Ctrl+Z / Ctrl+Shift+Z", "Editor: undo / redo"],
];
const LAYOUT_SHORT = { full: "Full", "caption-punch": "Caption punch", split: "Split", "square-frame": "Square", speaker: "Speaker" };

async function pageEdit(root, jobId, key) {
  const base = `/api/jobs/${enc(jobId)}/clips/${enc(key)}`;
  let ctx = await api("GET", `${base}/edit`);
  const W = ctx.width, H = ctx.height;
  const [CW, CH] = ctx.canvas;
  const [lo, hi] = ctx.bounds;
  const L = ctx.limits;
  const FRAME = 1 / L.fps;
  let alive = true;
  let snapOn = true;
  let form = null;

  // ---- model ------------------------------------------------------------------------------------------------

  function lockAspect(box, a) {
    let [x, y, w, hh] = box.map(Number);
    const cx = x + w / 2, cy = y + hh / 2;
    w = Math.max(2, w);
    hh = w / a;
    const fit = Math.min(1, W / w, H / hh);
    w *= fit;
    hh *= fit;
    const grow = Math.max(1, ctx.min_box / w, ctx.min_box / hh);
    w = Math.min(W, w * grow);
    w = Math.floor(w / 2) * 2;
    hh = Math.round(w / a / 2) * 2;
    if (hh > H) {
      hh = H - (H % 2);
      w = Math.floor((hh * a) / 2) * 2;
    }
    return clampBox([cx - w / 2, cy - hh / 2, w, hh]);
  }
  function clampBox(b) {
    const w = Math.min(Math.round(b[2] / 2) * 2, W - (W % 2)), hh = Math.min(Math.round(b[3] / 2) * 2, H - (H % 2));
    return [Math.floor(clamp(Math.round(b[0]), 0, W - w) / 2) * 2, Math.floor(clamp(Math.round(b[1]), 0, H - hh) / 2) * 2, w, hh];
  }
  const aspectsOf = (layout) => ctx.aspects[layout] || {};
  function autoWindows(layout) {
    const out = { cam: null, game: null };
    const auto = ctx.layouts[layout] || {};
    for (const [which, a] of Object.entries(aspectsOf(layout))) {
      const b = auto[which] || ctx.layouts.full.game || [0, 0, W, H];
      out[which] = lockAspect(b, a);
    }
    return out;
  }
  function newSeg(start, end, layout, wins) {
    return { start, end, layout, windows: wins || autoWindows(layout), keys: [], ease: "smooth", boundary: "cut" };
  }
  function normalize(f) {
    f = clone(f);
    if (!Array.isArray(f.segments) || !f.segments.length) {
      const wins = { cam: f.cam || null, game: f.game || null };
      f.segments = [newSeg(f.start, f.end, f.layout, wins)];
    }
    for (const s of f.segments) {
      s.keys = s.keys || [];
      s.ease = s.ease || "smooth";
      s.boundary = s.boundary || "cut";
      s.windows = s.windows || {};
      const auto = autoWindows(s.layout);
      for (const which of ["cam", "game"]) {
        const a = aspectsOf(s.layout)[which];
        s.windows[which] = a ? (s.windows[which] ? s.windows[which].slice() : auto[which]) : null;
      }
    }
    f.caption_anchor = f.caption_anchor || null;
    f.zoom = f.zoom || "off";
    f.enhance_speech = f.enhance_speech || "off";
    f.remove_fillers = !!f.remove_fillers;
    f.brand_kit = !!f.brand_kit;
    return f;
  }
  const segs = () => form.segments;
  function segAt(t) {
    const s = segs();
    for (let i = 0; i < s.length; i++) if (t < s[i].end - 1e-6) return i;
    return s.length - 1;
  }
  const easeP = (p, e) => {
    p = clamp(p, 0, 1);
    return e === "smooth" ? p * p * (3 - 2 * p) : p;
  };
  function panValue(pts, t, e) {
    if (t <= pts[0][0]) return pts[0][1];
    for (let k = 1; k < pts.length; k++) {
      if (t < pts[k][0]) return pts[k - 1][1] + (pts[k][1] - pts[k - 1][1]) * easeP((t - pts[k - 1][0]) / Math.max(1e-3, pts[k][0] - pts[k - 1][0]), e);
    }
    return pts[pts.length - 1][1];
  }
  function windowPoints(list, i, which) {
    const s = list[i];
    let pts = [[s.start, s.windows[which].slice(0, 2)]];
    for (const k of s.keys) {
      if (k.t - s.start < 1e-3) pts[0] = [s.start, k[which].slice(0, 2)];
      else pts.push([k.t, k[which].slice(0, 2)]);
    }
    const nxt = list[i + 1];
    if (nxt && s.boundary === "smooth" && nxt.windows[which]) {
      const t0 = Math.max(s.start, s.end - L.smooth_s);
      const here = [0, 1].map((d) => panValue(pts.map((p) => [p[0], p[1][d]]), t0, s.ease));
      const first = windowPoints(list.slice(0, i + 2), i + 1, which)[0][1];
      pts = pts.filter((p) => p[0] < t0 - 1e-6).concat([[t0, here], [s.end, first]]);
    }
    return pts;
  }
  function winAt(i, which, t) {
    const s = segs()[i];
    if (!s.windows[which]) return null;
    const pts = windowPoints(segs(), i, which);
    const x = panValue(pts.map((p) => [p[0], p[1][0]]), t, s.ease);
    const y = panValue(pts.map((p) => [p[0], p[1][1]]), t, s.ease);
    return [x, y, s.windows[which][2], s.windows[which][3]];
  }
  function smoothAllowed(a, b) {
    if (a.layout !== b.layout) return false;
    for (const which of ["cam", "game"]) {
      const x = a.windows[which], y = b.windows[which];
      if (!x !== !y || (x && (x[2] !== y[2] || x[3] !== y[3]))) return false;
    }
    return true;
  }
  function fixBoundaries() {
    const s = segs();
    s.forEach((g, i) => {
      if (i === s.length - 1 || (g.boundary === "smooth" && !smoothAllowed(g, s[i + 1]))) g.boundary = "cut";
    });
  }
  function fitSegs() {
    let s = segs().filter((g) => g.end > form.start + 1e-3 && g.start < form.end - 1e-3);
    if (!s.length) s = [segs()[0]];
    s[0].start = form.start;
    s[s.length - 1].end = form.end;
    for (let i = s.length - 1; i >= 0 && s.length > 1; i--) {
      if (s[i].end - s[i].start < L.min_seg - 1e-6) {
        if (i > 0) {
          s[i - 1].end = s[i].end;
          s.splice(i, 1);
        } else {
          s[1].start = s[0].start;
          s.splice(0, 1);
        }
      }
    }
    for (const g of s) g.keys = g.keys.filter((k) => k.t > g.start + 1e-3 && k.t <= g.end + 1e-3);
    form.segments = s;
    fixBoundaries();
  }
  // what the server gets: the old one-layout form for one segment without keyframes, else the segments
  function payload() {
    const f = clone(form);
    const s = f.segments;
    const a = aspectsOf(s[0].layout);
    f.layout = s[0].layout;
    if (s.length === 1 && !s[0].keys.length) {
      f.cam = a.cam ? s[0].windows.cam : null;
      f.game = a.game && s[0].layout !== "speaker" ? s[0].windows.game : null;
      delete f.segments;
    } else {
      f.cam = s[0].windows.cam;
      f.game = s[0].windows.game;
    }
    return f;
  }

  // ---- history (undo / redo) --------------------------------------------------------------------------------

  const undoStack = [], redoStack = [];
  function push() {
    undoStack.push(JSON.stringify(form));
    if (undoStack.length > 100) undoStack.shift();
    redoStack.length = 0;
    updHistoryBtns();
  }
  function undo() {
    if (!undoStack.length) return;
    redoStack.push(JSON.stringify(form));
    form = JSON.parse(undoStack.pop());
    syncAll();
  }
  function redo() {
    if (!redoStack.length) return;
    undoStack.push(JSON.stringify(form));
    form = JSON.parse(redoStack.pop());
    syncAll();
  }

  // ---- source video with the crop windows -------------------------------------------------------------------

  const video = h("video", { src: ctx.video_url, preload: "auto", playsInline: true, id: "ed-video", muted: false });
  const wrap = h("div", { class: "crop-wrap", style: { aspectRatio: `${W} / ${H}` } }, video);
  const boxes = { cam: makeBox("cam"), game: makeBox("game") };
  wrap.append(boxes.cam, boxes.game);
  const now = () => video.currentTime || 0;
  const keyAt = (i, t) => segs()[i].keys.findIndex((k) => Math.abs(k.t - t) < 1.5 * FRAME);

  function makeBox(which) {
    const b = h("div", { class: "box " + which, dataset: { which } }, h("span", { class: "tag", text: which === "cam" ? "Facecam" : "Gameplay" }), h("div", { class: "handle", title: "Resize" }));
    let drag = null;
    b.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      video.pause();
      const t = now(), i = segAt(t), s = segs()[i];
      // the box under the playhead: the segment start, a keyframe, or (with keyframes) a new keyframe here
      let target = "window", k = keyAt(i, t);
      if (k >= 0) {
        push();
        target = k;
      } else if (s.keys.length && t > s.start + FRAME && t <= s.end) {
        if (s.keys.length >= L.max_keys) {
          toast(`A segment can have at most ${L.max_keys} keyframes.`, "error", "delete one first");
          return;
        }
        push();
        const nk = { t: Math.round(t * 1000) / 1000 };
        for (const w of ["cam", "game"]) if (s.windows[w]) nk[w] = clampBox(winAt(i, w, t));
        s.keys.push(nk);
        s.keys.sort((a, c) => a.t - c.t);
        target = s.keys.indexOf(nk);
      } else push();
      b.setPointerCapture(ev.pointerId);
      const r = target === "window" ? s.windows[which] : s.keys[target][which];
      drag = { i, target, size: ev.target.classList.contains("handle"), x0: ev.clientX, y0: ev.clientY, r: r.slice() };
    });
    b.addEventListener("pointermove", (ev) => {
      if (!drag) return;
      const s = segs()[drag.i];
      const sc = W / wrap.clientWidth;
      const dx = (ev.clientX - drag.x0) * sc, dy = (ev.clientY - drag.y0) * sc;
      let [x, y, w, hh] = drag.r;
      const a = aspectsOf(s.layout)[which];
      if (drag.size) {
        const minW = Math.max(ctx.min_box, ctx.min_box * a);
        w = clamp(w + Math.max(dx, dy * a), minW, Math.min(W - x, (H - y) * a));
        hh = w / a;
        // a size change applies to the window and every keyframe of the segment (keyframes only pan)
        s.windows[which] = [s.windows[which][0], s.windows[which][1], w, hh];
        for (const k of s.keys) k[which] = [k[which][0], k[which][1], w, hh];
        if (drag.target !== "window") s.keys[drag.target][which] = [x, y, w, hh];
        else s.windows[which] = [x, y, w, hh];
      } else {
        x = clamp(x + dx, 0, W - w);
        y = clamp(y + dy, 0, H - hh);
        if (drag.target === "window") s.windows[which] = [x, y, w, hh];
        else s.keys[drag.target][which] = [x, y, w, hh];
      }
      redraw();
    });
    const end = () => {
      if (!drag) return;
      const s = segs()[drag.i];
      const a = aspectsOf(s.layout)[which];
      const fix = (r) => lockAspect(r, a);
      s.windows[which] = fix(s.windows[which]);
      const [, , w, hh] = s.windows[which];
      for (const k of s.keys) k[which] = clampBox([k[which][0], k[which][1], w, hh]);
      drag = null;
      fixBoundaries();
      syncSegUI();
      redraw();
    };
    b.addEventListener("pointerup", end);
    b.addEventListener("pointercancel", end);
    return b;
  }

  function placeBoxes() {
    const t = now(), i = segAt(t);
    for (const which of ["cam", "game"]) {
      const r = winAt(i, which, clamp(t, segs()[i].start, segs()[i].end));
      const b = boxes[which];
      b.hidden = !r;
      if (!r) continue;
      Object.assign(b.style, { left: `${(r[0] / W) * 100}%`, top: `${(r[1] / H) * 100}%`, width: `${(r[2] / W) * 100}%`, height: `${(r[3] / H) * 100}%` });
      b.classList.toggle("keyed", segs()[i].keys.length > 0);
    }
  }

  // ---- live output preview (canvas drawn from the source video) ---------------------------------------------

  const PW = ctx.ratio === "9:16" ? 300 : 380, PH = Math.round((PW * CH) / CW);
  const cv = h("canvas", { width: CW / 2, height: CH / 2, id: "ed-preview" });
  const ovCap = h("div", { class: "ov-cap drag", id: "ed-cap-ov", title: "Drag to move the captions" });
  const ovHook = h("div", { class: "ov-hook" });
  const preview = h("div", { class: "preview", style: { width: `${PW}px`, height: `${PH}px` } }, cv, ovHook, ovCap);
  const faceWarn = h("span", { class: "chip warn-chip", id: "ed-face-warn", hidden: true }, icon("alert-triangle", 14), "The caption covers a face");
  const capAuto = h("button", { class: "btn sm", type: "button", id: "ed-cap-auto", text: "Auto position", title: "Let the layout place the captions" });
  capAuto.addEventListener("click", () => {
    push();
    form.caption_anchor = null;
    syncAll();
  });

  function drawPreview() {
    const c = cv.getContext("2d");
    const sw = cv.width, sh = cv.height, k = sw / CW;
    c.fillStyle = "#000";
    c.fillRect(0, 0, sw, sh);
    if (video.readyState >= 2) {
      const draw = (box, dx, dy, dw, dh) => {
        let [sx, sy, bw, bh] = box;
        const da = dw / dh, sa = bw / bh;
        if (sa > da) {
          const nw = bh * da;
          sx += (bw - nw) / 2;
          bw = nw;
        } else if (sa < da) {
          const nh = bw / da;
          sy += (bh - nh) / 2;
          bh = nh;
        }
        c.drawImage(video, sx, sy, bw, bh, dx, dy, dw, dh);
      };
      const t = now(), i = segAt(t), s = segs()[i];
      const tt = clamp(t, s.start, s.end);
      if (s.layout === "square-frame") {
        c.filter = "blur(6px)";
        draw([0, 0, W, H], 0, 0, sw, sh);
        c.filter = "none";
      }
      for (const [which, rx, ry, rw, rh] of ctx.panel_rects[s.layout]) {
        const r = winAt(i, which, tt);
        if (r) draw(r, rx * k, ry * k, rw * k, rh * k);
      }
    }
    drawOverlay();
  }

  function capBoxFor(anchor) {
    const [a, y] = anchor;
    const hh = 150;
    return a === 8 ? [y, y + hh] : a === 5 ? [y - hh / 2, y + hh / 2] : [y - hh, y];
  }
  function defaultAnchor() {
    const tall = ctx.ratio === "9:16";
    const lay = segs()[0].layout;
    const camH = tall ? 672 : 454;
    const pos = form.caption_position;
    if (lay === "split") return { top: [8, camH + 28], center: [5, camH + (CH - camH) / 2], lower: [2, CH - (tall ? 260 : 60)] }[pos] || [8, camH + 28];
    if (lay === "square-frame") return { top: [8, 494], center: [5, 920], lower: [8, 1400] }[pos] || [8, 1400];
    return { top: [8, tall ? 260 : 120], center: [5, CH / 2], lower: [2, tall ? 1500 : 900] }[pos] || [2, tall ? (lay === "speaker" && segs().length === 1 ? 1660 : 1500) : 900];
  }
  const anchorNow = () => form.caption_anchor || defaultAnchor();
  function safeZones() {
    const tall = ctx.ratio === "9:16";
    const z = tall ? [[8, 260], [2, 830], [2, 1500]] : [[8, 120], [5, 540], [2, 900]];
    if (segs().some((s) => s.layout === "split")) z.push([8, (tall ? 672 : 454) + 28]);
    return z;
  }
  // ---- hook banner zone and the caption below it: mirrors captions/ass.py build_ass --------------------------
  const KS = CH === 1080 ? 0.76 : 1;
  const estTop = (a, y, n, size) => {
    const hh = n * size * 1.2;
    return a === 8 ? y : a === 5 ? y - hh / 2 : y - hh;
  };
  function lineCount(text, max = 16) {
    // ass._tokens: markup stripped, words wrapped at `max` characters, at most 2 lines
    const plain = String(text || "").replace(/\{\w+:([^}]*)\}/g, "$1").replace(/\*([^*]+)\*/g, "$1");
    let n = 0;
    for (const raw of plain.split("\n")) {
      let cur = 0, any = false;
      for (const w of raw.split(/\s+/).filter(Boolean)) {
        const add = w.length + (any ? 1 : 0);
        if (any && cur + add > max) {
          n++;
          cur = w.length;
        } else {
          cur += add;
          any = true;
        }
      }
      if (any) n++;
    }
    return clamp(n, 1, 2);
  }
  function hookZone() {
    // the render uses the reframe panel (layout "full") for 2+ segments or keyframes, else the segment's layout;
    // a caption anchor (dragged, or the reframe panel's auto one) moves the hook with it (apply_caption_anchor)
    const s = segs(), zones = ctx.hook_zones || {};
    const rf = s.length > 1 || s.some((x) => x.keys.length);
    const z = zones[rf ? "full" : s[0].layout] || zones.full;
    if (!z) return null;
    const anchor = form.caption_anchor || (rf ? (zones[s[0].layout] || {}).auto_anchor : null);
    const hk = anchor ? { ...z.hook, align: anchor[0], y: anchor[1] } : z.hook;
    const n = lineCount(form.hook_text);
    const size = Math.round((form.hook_style === "top-bar" ? 80 : 96) * KS);
    const hh = n * size * 1.2;
    if (form.hook_style === "top-bar") {
      const barH = Math.trunc(hh + 52);
      const top = z.frame ? z.frame[1] - barH : Math.trunc(hk.bar_top ?? 160);
      return { top, bottom: top + barH, align: hk.align, bar: true };
    }
    const pad = form.hook_style === "bold-box" ? 18 : 14;
    const top = estTop(hk.align, hk.y, n, size) - pad;
    return { top, bottom: top + hh + 2 * pad, align: hk.align, bar: false };
  }
  let capGroup = null; // {s, text} of the caption on screen (drawOverlay)
  const hookOn = (t) => !!form.hook_text && t >= form.start - 0.05 && t < form.start + 2;
  function captionAt() {
    // a caption that starts while the hook shows and sits in its zone moves just below the hook banner
    const [a, y] = anchorNow();
    if (!capGroup || !form.hook_text || capGroup.s >= form.start + 2 - 0.01) return [a, y];
    const hz = hookZone();
    if (!hz || !(hz.bar || hz.align === a)) return [a, y];
    const info = ctx.style_info[form.caption_style] || ctx.style_info["bold-pop"] || {};
    const size = Math.round((info.size || 84) * KS), up = info.anim_up || 0;
    const n = lineCount(capGroup.text), capH = n * size * 1.2;
    if (estTop(a, y, n, size) - up >= hz.bottom + 20) return [a, y];
    const nt = hz.bottom + 24 + up;
    return [a, Math.trunc(a === 8 ? nt : a === 5 ? nt + capH / 2 : nt + capH)];
  }
  function placeCaption() {
    const [a, y] = captionAt();
    ovCap.style.top = `${(y / CH) * 100}%`;
    ovCap.style.transform = a === 8 ? "translateY(0)" : a === 5 ? "translateY(-50%)" : "translateY(-100%)";
    capAuto.disabled = !form.caption_anchor;
    faceWarn.hidden = !captionCoversFace();
  }
  function captionCoversFace() {
    const [y0, y1] = capBoxFor(anchorNow());
    const s = segs();
    for (let i = 0; i < s.length; i++) {
      if (s[i].layout === "split") {
        const camH = ctx.panel_rects.split[0][4];
        if (y0 < camH) return true;
      }
    }
    for (const f of ctx.faces || []) {
      if (f.t < form.start || f.t > form.end) continue;
      const i = segAt(f.t);
      for (const [which, , ry, , rh] of ctx.panel_rects[s[i].layout]) {
        const r = winAt(i, which, f.t);
        if (!r) continue;
        const [fx0, fy0, fx1, fy1] = f.box;
        if (fx1 <= r[0] || fx0 >= r[0] + r[2] || fy1 <= r[1] || fy0 >= r[1] + r[3]) continue;
        const cy0 = ry + ((Math.max(fy0, r[1]) - r[1]) / r[3]) * rh, cy1 = ry + ((Math.min(fy1, r[1] + r[3]) - r[1]) / r[3]) * rh;
        if (cy0 < y1 && cy1 > y0) return true;
      }
    }
    return false;
  }
  {
    let drag = null;
    ovCap.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      push();
      ovCap.setPointerCapture(ev.pointerId);
      drag = { y0: ev.clientY, a: anchorNow().slice() };
    });
    ovCap.addEventListener("pointermove", (ev) => {
      if (!drag) return;
      const sc = CH / preview.clientHeight;
      const [lo2, hi2] = L.anchor_y;
      form.caption_anchor = [drag.a[0], Math.round(clamp(drag.a[1] + (ev.clientY - drag.y0) * sc, lo2, hi2))];
      form.caption_position = "auto";
      placeCaption();
    });
    const up = () => {
      if (!drag) return;
      drag = null;
      // snap to the nearest safe zone (by the caption box centre)
      const mid = (z) => {
        const [a, b] = capBoxFor(z);
        return (a + b) / 2;
      };
      const cur = mid(anchorNow());
      let best = safeZones()[0];
      for (const z of safeZones()) if (Math.abs(mid(z) - cur) < Math.abs(mid(best) - cur)) best = z;
      form.caption_anchor = best.slice();
      posSel.value = "auto";
      placeCaption();
    };
    ovCap.addEventListener("pointerup", up);
    ovCap.addEventListener("pointercancel", up);
  }

  function drawOverlay() {
    const t = now();
    const info = ctx.style_info[form.caption_style] || ctx.style_info["bold-pop"];
    const hl = form.highlight_color ? ctx.palette[form.highlight_color] : info.active;
    const inside = ctx.words.map((w, i) => ({ ...w, i })).filter((w) => w.e > form.start && w.s < form.end);
    const n = Math.max(1, info.words);
    let group = null;
    for (let k = 0; k < inside.length; k += n) {
      const g = inside.slice(k, k + n);
      const next = inside[k + n];
      if (t >= g[0].s - 0.05 && t < (next ? next.s : g[g.length - 1].e + 0.4)) group = g;
    }
    // with no word on screen, show a sample so the caption can be placed
    const sample = !group && inside.length ? inside.slice(0, n) : null;
    const cased = (s) => (info.case === "upper" ? s.toUpperCase() : s);
    ovCap.classList.toggle("sample", !!sample);
    ovCap.replaceChildren(
      ...(group || sample || []).map((w, j) => {
        const active = t >= w.s && t < w.e;
        const span = h("span", { class: "w" + (active ? " active" : "") + (info.box ? " boxed" : ""), text: cased(form.words[w.i]) });
        span.style.color = active && !info.box ? hl : info.primary;
        if (info.box) span.style.setProperty("--hl", hl);
        return j ? [" ", span] : span;
      }).flat()
    );
    ovCap.dataset.style = form.caption_style;
    const shown = group || sample;
    capGroup = shown ? { s: shown[0].s, text: shown.map((w) => form.words[w.i]).join(" ") } : null;
    const showHook = hookOn(t);
    ovHook.className = "ov-hook " + form.hook_style;
    const hz = showHook ? hookZone() : null;
    if (hz) Object.assign(ovHook.style, { top: `${(hz.top / CH) * 100}%`, height: `${((hz.bottom - hz.top) / CH) * 100}%` });
    ovHook.replaceChildren(hz ? h("span", { text: form.hook_text.toUpperCase() }) : "");
    placeCaption();
  }

  // ---- timeline ---------------------------------------------------------------------------------------------

  const tcv = h("canvas", { class: "tl-canvas" });
  const segRow = h("div", { class: "tl-segs", id: "ed-segs" });
  const wordRow = h("div", { class: "tl-words", id: "ed-wordchips" });
  const head = h("div", { class: "tl-head" });
  const hIn = h("div", { class: "th in", title: "Drag to change the start", role: "slider", "aria-label": "Start" }, h("span", { text: "In" }));
  const hOut = h("div", { class: "th out", title: "Drag to change the end", role: "slider", "aria-label": "End" }, h("span", { text: "Out" }));
  const tl = h("div", { class: "timeline tl", id: "ed-timeline" }, tcv, segRow, wordRow, head, hIn, hOut);
  const xOf = (t) => ((t - lo) / (hi - lo)) * 100;
  const tOf = (clientX) => {
    const r = tl.getBoundingClientRect();
    return lo + clamp((clientX - r.left) / r.width, 0, 1) * (hi - lo);
  };
  const thumbs = new Image();
  let thumbsOk = false;
  thumbs.addEventListener("load", () => {
    thumbsOk = true;
    drawTimeline();
  });
  thumbs.addEventListener("error", () => (thumbsOk = false));
  thumbs.src = ctx.thumbs.url;

  function snap(t, which) {
    if (!snapOn) return t;
    for (const w of ctx.words) if (t > w.s && t < w.e) return which === "in" ? w.s : which === "out" ? w.e : t - w.s < w.e - t ? w.s : w.e;
    let best = t, bd = 0.35;
    for (const w of ctx.words) {
      for (const b of [w.s, w.e]) {
        const dd = Math.abs(b - t);
        if (dd < bd) {
          bd = dd;
          best = b;
        }
      }
    }
    return best;
  }
  function setIn(t) {
    t = clamp(snap(t, "in"), Math.max(lo, form.end - ctx.max_dur), form.end - ctx.min_dur);
    form.start = Math.round(t * 1000) / 1000;
    fitSegs();
  }
  function setOut(t) {
    t = clamp(snap(t, "out"), form.start + ctx.min_dur, Math.min(hi, form.start + ctx.max_dur));
    form.end = Math.round(t * 1000) / 1000;
    fitSegs();
  }

  function drawTimeline() {
    const r = tcv.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const TW = Math.max(1, Math.round(r.width * dpr)), TH = Math.max(1, Math.round(r.height * dpr));
    if (tcv.width !== TW || tcv.height !== TH) {
      tcv.width = TW;
      tcv.height = TH;
    }
    const c = tcv.getContext("2d");
    c.clearRect(0, 0, TW, TH);
    const px = (t) => ((t - lo) / (hi - lo)) * TW;
    const stripH = thumbsOk ? Math.round(TH * 0.5) : 0;
    if (thumbsOk) {
      const tw = ctx.thumbs.w, th = ctx.thumbs.h;
      for (let i = 0; i < ctx.thumbs.n; i++) {
        const t = ctx.thumbs.lo + i * ctx.thumbs.step;
        const x0 = px(t), x1 = px(Math.min(hi, t + ctx.thumbs.step));
        const slot = x1 - x0;
        if (slot < 1) continue;
        // keep the frame's shape: crop the tile to the slot instead of squeezing it
        const want = (slot / stripH) * th;
        const sw = Math.min(tw, want), sx = i * tw + (tw - sw) / 2;
        c.drawImage(thumbs, sx, 0, sw, th, x0, 0, slot + 0.5, stripH);
      }
    }
    const peaks = ctx.wave.peaks, step = ctx.wave.step;
    const bw = Math.max(1, px(lo + step) - px(lo) - 1);
    const waveH = TH - stripH, mid = stripH + waveH / 2;
    for (let i = 0; i < peaks.length; i++) {
      const t = ctx.wave.lo + i * step;
      const bh = Math.max(2 * dpr, peaks[i] * waveH * 0.8);
      c.fillStyle = t >= form.start && t < form.end ? "#a78bfa" : "#52525b";
      c.fillRect(px(t), mid - bh / 2, bw, bh);
    }
    c.fillStyle = "rgba(250, 204, 21, .55)";
    for (const w of ctx.words) c.fillRect(px(w.s), TH - 6 * dpr, 1 * dpr, 6 * dpr);
    c.fillStyle = "rgba(0, 0, 0, .55)";
    c.fillRect(0, 0, px(form.start), TH);
    c.fillRect(px(form.end), 0, TW - px(form.end), TH);
    head.style.left = `${xOf(now())}%`;
    hIn.style.left = `${xOf(form.start)}%`;
    hOut.style.left = `${xOf(form.end)}%`;
    inIn.value = form.start.toFixed(2);
    outIn.value = form.end.toFixed(2);
    lenTxt.textContent = `Length ${(form.end - form.start).toFixed(1)} s`;
    timeTxt.textContent = `${fmtT(now() - form.start)} / ${fmtT(form.end - form.start)}`;
    wordInputs.forEach((inp, i) => inp.classList.toggle("out", !(ctx.words[i].e > form.start && ctx.words[i].s < form.end)));
    markKeys();
  }
  const fmtT = (t) => {
    t = Math.max(0, t);
    return `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, "0")}`;
  };

  // segment blocks, join toggles and keyframe diamonds (rebuilt on every model change)
  function syncSegUI() {
    const s = segs();
    const act = segAt(now());
    const kids = [];
    s.forEach((g, i) => {
      const blk = h(
        "button",
        {
          type: "button",
          class: `seg-block lay-${g.layout}` + (i === act ? " on" : ""),
          style: { left: `${xOf(g.start)}%`, width: `${xOf(g.end) - xOf(g.start)}%` },
          title: `Segment ${i + 1}: ${ctx.layout_labels[g.layout]} (${(g.end - g.start).toFixed(1)} s)`,
          dataset: { i: String(i) },
          "aria-label": `Segment ${i + 1}, ${LAYOUT_SHORT[g.layout]}`,
        },
        h("span", { text: `${i + 1} ${LAYOUT_SHORT[g.layout]}` })
      );
      blk.addEventListener("click", () => {
        video.pause();
        video.currentTime = g.start + 0.001;
        redraw();
      });
      kids.push(blk);
      if (i < s.length - 1) {
        const ok = smoothAllowed(g, s[i + 1]);
        const b = h("button", {
          type: "button",
          class: "bnd" + (g.boundary === "smooth" ? " smooth" : ""),
          style: { left: `${xOf(g.end)}%` },
          disabled: !ok,
          title: ok ? (g.boundary === "smooth" ? "Smooth join: click for a cut" : "Cut: click for a smooth join") : "Smooth needs the same layout and crop size on both sides",
          "aria-label": `Join after segment ${i + 1}: ${g.boundary}`,
          dataset: { i: String(i) },
          text: g.boundary === "smooth" ? "~" : "|",
        });
        b.addEventListener("click", () => {
          push();
          g.boundary = g.boundary === "smooth" ? "cut" : "smooth";
          syncAll();
        });
        kids.push(b);
      }
    });
    const g = s[act];
    g.keys.forEach((k, j) => {
      const d = h("div", { class: "kf", style: { left: `${xOf(k.t)}%` }, title: `Keyframe at ${k.t.toFixed(2)} s (drag to move)`, dataset: { j: String(j) }, role: "button", tabIndex: 0, "aria-label": `Keyframe ${j + 1}` });
      let on = false, moved = false;
      d.addEventListener("pointerdown", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        d.setPointerCapture(ev.pointerId);
        on = true;
        moved = false;
        video.pause();
        video.currentTime = k.t;
      });
      d.addEventListener("pointermove", (ev) => {
        if (!on) return;
        if (!moved) push();
        moved = true;
        const prev = j ? g.keys[j - 1].t + 0.05 : g.start + FRAME;
        const next = j < g.keys.length - 1 ? g.keys[j + 1].t - 0.05 : g.end;
        k.t = Math.round(clamp(tOf(ev.clientX), prev, next) * 1000) / 1000;
        d.style.left = `${xOf(k.t)}%`;
        video.currentTime = k.t;
      });
      const up = () => {
        if (!on) return;
        on = false;
        syncAll();
      };
      d.addEventListener("pointerup", up);
      d.addEventListener("pointercancel", up);
      kids.push(d);
    });
    segRow.replaceChildren(...kids);
    layoutSel.value = g.layout;
    easeSel.value = g.ease;
    segInfo.textContent = `Segment ${act + 1} of ${s.length}`;
    bMerge.disabled = s.length < 2;
    markKeys();
  }
  function markKeys() {
    const i = segAt(now());
    const k = keyAt(i, now());
    segRow.querySelectorAll(".kf").forEach((d) => d.classList.toggle("on", Number(d.dataset.j) === k));
    bDelKey.disabled = k < 0;
    segRow.querySelectorAll(".seg-block").forEach((b) => b.classList.toggle("on", Number(b.dataset.i) === i));
    if (layoutSel.value !== segs()[i].layout) {
      layoutSel.value = segs()[i].layout;
      easeSel.value = segs()[i].ease;
      segInfo.textContent = `Segment ${i + 1} of ${segs().length}`;
    }
  }
  function syncWordChips() {
    wordRow.replaceChildren(
      ...ctx.words.map((w, i) => {
        const chip = h("button", {
          type: "button",
          class: "wchip" + (w.e > form.start && w.s < form.end ? "" : " out") + (form.words[i] !== w.w ? " changed" : ""),
          style: { left: `${xOf(w.s)}%`, width: `${Math.max(0.3, xOf(w.e) - xOf(w.s))}%` },
          title: `${form.words[i]} (${w.s.toFixed(2)} s)`,
          text: form.words[i],
          tabIndex: -1,
        });
        chip.addEventListener("click", () => {
          video.currentTime = w.s;
          wordInputs[i].focus();
        });
        return chip;
      })
    );
  }

  for (const [handle, setter] of [[hIn, setIn], [hOut, setOut]]) {
    let on = false;
    handle.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      handle.setPointerCapture(ev.pointerId);
      on = true;
      push();
      video.pause();
    });
    handle.addEventListener("pointermove", (ev) => {
      if (!on) return;
      setter(tOf(ev.clientX));
      video.currentTime = handle === hIn ? form.start : form.end;
      redraw();
    });
    const up = () => {
      if (!on) return;
      on = false;
      syncAll();
    };
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  }
  {
    let scrub = false;
    tl.addEventListener("pointerdown", (ev) => {
      if (ev.target.closest(".th, .kf, .seg-block, .bnd, .wchip")) return;
      scrub = true;
      tl.setPointerCapture(ev.pointerId);
      video.currentTime = tOf(ev.clientX);
      redraw();
    });
    tl.addEventListener("pointermove", (ev) => {
      if (!scrub) return;
      video.currentTime = tOf(ev.clientX);
      redraw();
    });
    const end = () => (scrub = false);
    tl.addEventListener("pointerup", end);
    tl.addEventListener("pointercancel", end);
  }

  const inIn = h("input", { type: "number", step: "0.1", id: "ed-in", "aria-label": "Start (s)" });
  const outIn = h("input", { type: "number", step: "0.1", id: "ed-out", "aria-label": "End (s)" });
  inIn.addEventListener("change", () => {
    push();
    setIn(Number(inIn.value));
    video.currentTime = form.start;
    syncAll();
  });
  outIn.addEventListener("change", () => {
    push();
    setOut(Number(outIn.value));
    video.currentTime = form.end;
    syncAll();
  });
  const lenTxt = h("span", { class: "muted small" });
  const timeTxt = h("span", { class: "ed-time", id: "ed-time" });
  const snapBox = h("input", { type: "checkbox", checked: true, id: "ed-snap" });
  snapBox.addEventListener("change", () => (snapOn = snapBox.checked));

  // ---- playback ---------------------------------------------------------------------------------------------

  let playingSel = false;
  const playBtn = h("button", { class: "btn sm", type: "button", id: "ed-play", title: "Play / pause (Space)", "aria-label": "Play" }, icon("play", 16), h("span", { class: "lbl", text: "Play" }));
  function playPause() {
    if (!video.paused) {
      video.pause();
      return;
    }
    playingSel = true;
    if (now() < form.start - 0.01 || now() >= form.end - 0.05) video.currentTime = form.start;
    video.playbackRate = 1;
    video.play().catch(() => {});
  }
  playBtn.addEventListener("click", playPause);
  video.addEventListener("play", () => {
    playBtn.setAttribute("aria-label", "Pause");
    playBtn.replaceChildren(icon("pause", 16), h("span", { class: "lbl", text: "Pause" }));
  });
  video.addEventListener("pause", () => {
    playBtn.setAttribute("aria-label", "Play");
    playBtn.replaceChildren(icon("play", 16), h("span", { class: "lbl", text: "Play" }));
    playingSel = false;
    video.playbackRate = 1;
  });
  let raf = 0;
  function redraw() {
    placeBoxes();
    drawPreview();
    drawTimeline();
  }
  const tick = () => {
    if (playingSel && now() >= form.end) video.pause();
    redraw();
    if (!video.paused && alive) raf = requestAnimationFrame(tick);
  };
  video.addEventListener("play", () => (raf = requestAnimationFrame(tick)));
  video.addEventListener("seeked", redraw);
  video.addEventListener("loadeddata", () => {
    video.currentTime = form.start;
    redraw();
  });
  const onResize = () => redraw();
  window.addEventListener("resize", onResize);

  // ---- segment and keyframe actions -------------------------------------------------------------------------

  function split() {
    let t = now();
    if (snapOn) t = snap(t, "split");
    const i = segAt(t), s = segs()[i];
    if (t <= form.start || t >= form.end) return toast("Move the playhead inside the clip first.", "error");
    if (t - s.start < L.min_seg || s.end - t < L.min_seg) return toast(`Too close to the edge of a segment.`, "error", `keep at least ${L.min_seg} s on each side`);
    if (segs().length >= L.max_segs) return toast(`A clip can have at most ${L.max_segs} segments.`, "error", "merge some first");
    push();
    t = Math.round(t * 1000) / 1000;
    const right = clone(s);
    right.start = t;
    for (const w of ["cam", "game"]) if (s.windows[w]) right.windows[w] = clampBox(winAt(i, w, t));
    right.keys = s.keys.filter((k) => k.t > t + 1e-3);
    s.keys = s.keys.filter((k) => k.t < t - 1e-3);
    right.boundary = s.boundary;
    s.boundary = "cut";
    s.end = t;
    segs().splice(i + 1, 0, right);
    fixBoundaries();
    video.currentTime = t + 0.001;
    syncAll();
    toast(`Split at ${t.toFixed(2)} s: segment ${i + 2} can now get its own layout and crop.`, "ok");
  }
  function merge() {
    const s = segs();
    if (s.length < 2) return;
    push();
    const i = segAt(now());
    if (i > 0) {
      s[i - 1].end = s[i].end;
      s[i - 1].boundary = s[i].boundary;
      s.splice(i, 1);
    } else {
      s[1].start = s[0].start;
      s[1].keys = s[1].keys.filter((k) => k.t > s[1].start);
      s.splice(0, 1);
    }
    fixBoundaries();
    syncAll();
  }
  function addKey() {
    const t = now(), i = segAt(t), s = segs()[i];
    if (t <= s.start + FRAME || t > s.end) return toast("Move the playhead inside the segment, after its first frame.", "error", "the segment start is the window itself");
    if (keyAt(i, t) >= 0) return;
    if (s.keys.length >= L.max_keys) return toast(`A segment can have at most ${L.max_keys} keyframes.`, "error", "delete one first");
    push();
    const k = { t: Math.round(t * 1000) / 1000 };
    for (const w of ["cam", "game"]) if (s.windows[w]) k[w] = clampBox(winAt(i, w, t));
    s.keys.push(k);
    s.keys.sort((a, b) => a.t - b.t);
    syncAll();
    toast("Keyframe added. Drag the box to move the crop at this time.", "ok");
  }
  function delKey() {
    const i = segAt(now()), j = keyAt(i, now());
    if (j < 0) return;
    push();
    segs()[i].keys.splice(j, 1);
    syncAll();
  }
  function resetAuto() {
    push();
    const s = segs()[segAt(now())];
    s.windows = autoWindows(s.layout);
    s.keys = [];
    fixBoundaries();
    syncAll();
  }

  const segInfo = h("span", { class: "muted small", id: "ed-seginfo" });
  const layoutOpts = Object.keys(ctx.layout_labels).map((k) => [k, ctx.layout_labels[k], ctx.ratio !== "9:16" && (k === "square-frame" || k === "caption-punch")]);
  const layoutSel = sel(layoutOpts, "full", { id: "ed-layout", "aria-label": "Layout of this segment" });
  layoutSel.addEventListener("change", () => {
    push();
    const s = segs()[segAt(now())];
    const same = JSON.stringify(aspectsOf(s.layout)) === JSON.stringify(aspectsOf(layoutSel.value));
    s.layout = layoutSel.value;
    if (!same) {
      s.windows = autoWindows(s.layout);
      s.keys = [];
    }
    fixBoundaries();
    syncAll();
  });
  const easeSel = sel([["smooth", "Smooth easing"], ["linear", "Linear"]], "smooth", { id: "ed-ease", "aria-label": "Keyframe easing" });
  easeSel.addEventListener("change", () => {
    push();
    segs()[segAt(now())].ease = easeSel.value;
    syncAll();
  });
  const tb = (id, ic, label, title, fn, cls) => {
    const b = h("button", { class: "btn sm " + (cls || ""), type: "button", id, title, "aria-label": label }, icon(ic, 16), h("span", { class: "lbl", text: label }));
    b.addEventListener("click", fn);
    return b;
  };
  const bSplit = tb("ed-split", "split", "Split", "Split at the playhead (S)", split);
  const bMerge = tb("ed-merge", "scissors", "Merge", "Join this segment with the one before it", merge);
  const bAddKey = tb("ed-addkey", "plus", "Keyframe", "Add a keyframe at the playhead", addKey);
  const bDelKey = tb("ed-delkey", "trash-2", "Delete key", "Delete the keyframe at the playhead (Delete)", delKey);
  const bAuto = tb("ed-auto", "wand-2", "Auto-reframe", "Follow the face with keyframes (needs OpenCV)", () => autoReframe());
  const bReset = tb("ed-reset", "rotate-ccw", "Reset to auto-detected", "Put this segment's crop back to the detected one", resetAuto);
  const bUndo = tb("ed-undo", "undo-2", "Undo", "Undo (Ctrl+Z)", undo, "ghost");
  const bRedo = tb("ed-redo", "redo-2", "Redo", "Redo (Ctrl+Shift+Z)", redo, "ghost");
  // secondary actions live in the "More" menu; the hidden buttons keep their enabled state (syncSegUI)
  const bMore = menuBtn(
    () => [
      { label: "Merge with the segment before", icon: "scissors", disabled: bMerge.disabled, onClick: merge },
      { label: "Delete keyframe (Delete)", icon: "trash-2", disabled: bDelKey.disabled, onClick: delKey },
      "-",
      { label: "Reset crop to auto-detected", icon: "rotate-ccw", title: "Put this segment's crop back to the detected one", onClick: resetAuto },
    ],
    "More editing actions"
  );
  bMore.id = "ed-more";
  function updHistoryBtns() {
    bUndo.disabled = !undoStack.length;
    bRedo.disabled = !redoStack.length;
  }

  async function autoReframe() {
    bAuto.disabled = true;
    setStatus("Finding faces in the clip ...");
    try {
      try {
        await api("POST", `${base}/reframe`, { edit: payload() });
      } catch (e) {
        if (e.status !== 409) throw e;
        setStatus(e.message); // a run started elsewhere (another tab): wait for its result
      }
      let r = { running: true };
      for (let n = 0; n < 600 && r.running; n++) {
        await new Promise((ok) => setTimeout(ok, 800));
        if (!alive) return;
        r = await api("GET", `${base}/reframe`);
      }
      if (r.error) throw Object.assign(new Error(r.error), { hint: r.hint });
      if (!r.segments) throw new Error("Auto-reframe did not finish.");
      push();
      form.segments = r.segments;
      form = normalize(form);
      ctx.faces = r.faces || [];
      fixBoundaries();
      syncAll();
      setStatus(r.note || "Auto-reframe done.");
      toast(r.note || "Auto-reframe done.", "ok");
    } catch (e) {
      setStatus(e.message + (e.hint ? ` Fix: ${e.hint}.` : ""), true);
    } finally {
      bAuto.disabled = false;
    }
  }

  // ---- shortcuts --------------------------------------------------------------------------------------------

  const onKey = (e) => {
    if (modals.size) return;
    const t = e.target;
    const typing = t && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName));
    const k = e.key;
    if ((e.ctrlKey || e.metaKey) && !e.altKey) {
      if (typing) return; // inputs keep their own text undo
      if (k === "z" || k === "Z") {
        e.preventDefault();
        if (e.shiftKey) redo();
        else undo();
      } else if (k === "y" || k === "Y") {
        e.preventDefault();
        redo();
      }
      return;
    }
    if (typing || e.altKey) return;
    if (k === " ") {
      e.preventDefault();
      playPause();
    } else if (k === "j" || k === "J") {
      video.currentTime = Math.max(lo, now() - 5);
      redraw();
    } else if (k === "k" || k === "K") video.pause();
    else if (k === "l" || k === "L") {
      if (video.paused) {
        video.currentTime = Math.min(hi, now() + 5);
        redraw();
      } else video.playbackRate = video.playbackRate >= 2 ? 1 : 2;
    } else if (k === "s" || k === "S") {
      e.preventDefault();
      split();
    } else if (k === "[" || k === "]") {
      push();
      if (k === "[") setIn(now());
      else setOut(now());
      syncAll();
    } else if (k === "Delete" || k === "Backspace") {
      e.preventDefault();
      delKey();
    }
  };
  document.addEventListener("keydown", onKey);
  SHORTCUTS.push(...ED_SHORTCUTS);
  pageCleanup.push(() => {
    alive = false;
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("resize", onResize);
    for (const row of ED_SHORTCUTS) {
      const i = SHORTCUTS.indexOf(row);
      if (i >= 0) SHORTCUTS.splice(i, 1);
    }
    cancelAnimationFrame(raf);
    video.pause();
    video.removeAttribute("src");
    video.load();
  });

  // ---- captions, style, post text ---------------------------------------------------------------------------

  const posSel = sel(ctx.positions, "auto", { id: "ed-pos" });
  const hlSel = sel(ctx.highlights.map((x) => [x, x || "The style's own"]), "", { id: "ed-hl" });
  const hookIn = h("input", { type: "text", maxLength: 80, id: "ed-hooktext" });
  const titleIn = h("input", { type: "text", maxLength: 100, id: "ed-title" });
  const descIn = h("textarea", { maxLength: 500, id: "ed-desc" });
  const tagsIn = h("input", { type: "text", id: "ed-tags", placeholder: "#gaming #clips" });
  // effects (5.3): punch-in zoom, speech enhancement, filler removal, brand kit
  const EC = ctx.effect_choices || { zoom: ["off"], enhance_speech: ["off"] };
  const zoomLabels = { off: "Off", light: "Light (1.08x)", medium: "Medium (1.12x)", strong: "Strong (1.2x)" };
  const enhLabels = { off: "Off", light: "Light (keeps more game sound)", strong: "Strong" };
  const zoomSel = sel(EC.zoom.map((z) => [z, zoomLabels[z] || z]), "off", { id: "ed-zoom" });
  const enhSel = sel(EC.enhance_speech.map((z) => [z, enhLabels[z] || z]), "off", { id: "ed-enhance" });
  const fxToggle = (id, label, disabled) => {
    const c = h("input", { type: "checkbox", id, role: "switch", disabled: !!disabled });
    return [c, h("label", { class: "switch" }, c, h("span", { class: "switch-ui", "aria-hidden": "true" }), label)];
  };
  const [fillChk, fillLbl] = fxToggle("ed-fillers", "Remove filler words (um, uh)");
  const [brandChk, brandLbl] = fxToggle("ed-brand", "Brand kit", !ctx.brand_kit_ready);
  for (const [elx, k] of [[zoomSel, "zoom"], [enhSel, "enhance_speech"]]) {
    elx.addEventListener("change", () => {
      push();
      form[k] = elx.value;
    });
  }
  for (const [elx, k] of [[fillChk, "remove_fillers"], [brandChk, "brand_kit"]]) {
    elx.addEventListener("change", () => {
      push();
      form[k] = elx.checked;
    });
  }
  const fxCard = h(
    "div",
    { class: "card stack", id: "ed-effects" },
    h("h2", { text: "Effects" }),
    h("div", { class: "grid2" }, field("Punch-in zoom", zoomSel, "On the payoff and hype words; faces stay in view"), field("Speech enhancement", enhSel, "Less background noise")),
    h("div", { class: "row" }, fillLbl, brandLbl),
    h("p", { class: "hint", text: ctx.brand_kit_ready ? "The brand kit comes from Settings. An intro card delays the hook, so keep it short. The preview does not show effects; Render does." : "Set up a brand kit in Settings to use it here. The preview does not show effects; Render does." })
  );
  // text fields: one undo step per focus, taken on the first change
  const once = (elx) => {
    let armed = true;
    elx.addEventListener("focus", () => (armed = true));
    return () => {
      if (armed) push();
      armed = false;
    };
  };
  for (const [elx, k] of [[posSel, "caption_position"], [hlSel, "highlight_color"], [hookIn, "hook_text"], [titleIn, "title"], [descIn, "description"]]) {
    const mark = once(elx);
    elx.addEventListener(elx.tagName === "SELECT" ? "change" : "input", () => {
      mark();
      form[k] = elx.value;
      if (k === "caption_position") form.caption_anchor = null;
      drawOverlay();
    });
  }
  {
    const mark = once(tagsIn);
    tagsIn.addEventListener("input", () => {
      mark();
      form.hashtags = tagsIn.value.split(/\s+/).filter(Boolean);
    });
  }
  const wordInputs = ctx.words.map((w, i) => {
    const inp = h("input", { type: "text", maxLength: 40, title: `${w.s.toFixed(2)}-${w.e.toFixed(2)} s`, dataset: { i: String(i) }, "aria-label": `Caption word ${i + 1}` });
    inp.size = Math.max(3, Math.min(14, w.w.length + 1));
    const mark = once(inp);
    inp.addEventListener("input", () => {
      mark();
      form.words[i] = inp.value;
      inp.classList.toggle("changed", inp.value !== w.w);
      drawOverlay();
      syncWordChips();
    });
    inp.addEventListener("focus", () => {
      video.currentTime = w.s;
    });
    return inp;
  });
  const capPick = h("div", { id: "ed-cap" });
  const hookPick = h("div", { id: "ed-hook" });
  function buildPickers() {
    capPick.replaceChildren(
      picker("caption", form.caption_style, (name) => {
        push();
        form.caption_style = name;
        drawOverlay();
      })
    );
    hookPick.replaceChildren(
      picker("hook", form.hook_style, (name) => {
        push();
        form.hook_style = name;
        drawOverlay();
      })
    );
  }

  function syncInputs() {
    posSel.value = form.caption_position;
    hlSel.value = form.highlight_color || "";
    hookIn.value = form.hook_text || "";
    titleIn.value = form.title || "";
    descIn.value = form.description || "";
    tagsIn.value = (form.hashtags || []).join(" ");
    zoomSel.value = form.zoom || "off";
    enhSel.value = form.enhance_speech || "off";
    fillChk.checked = !!form.remove_fillers;
    brandChk.checked = !!form.brand_kit && !!ctx.brand_kit_ready;
    wordInputs.forEach((inp, i) => {
      inp.value = form.words[i];
      inp.classList.toggle("changed", form.words[i] !== ctx.words[i].w);
    });
    buildPickers();
  }
  function syncAll() {
    syncInputs();
    syncSegUI();
    syncWordChips();
    updHistoryBtns();
    redraw();
  }

  // ---- save / render / draft --------------------------------------------------------------------------------

  const status = h("p", { class: "small", role: "status", id: "ed-status" });
  function setStatus(text, bad) {
    status.className = "small" + (bad ? " err" : "");
    status.textContent = text;
  }
  const act = async (action, btn) => {
    setStatus("");
    btn.disabled = true;
    try {
      const r = await api("POST", `${base}/edit`, { action, edit: action === "undo" ? null : payload() });
      if (action === "undo") {
        push();
        form = normalize(r.current);
        syncAll();
        setStatus("Back to the last rendered version.");
      } else if (action === "save") setStatus("Saved. Title, description and hashtags are in clips.json.");
      else {
        setStatus(action === "render" ? `Rendering ${r.name} in the background ...` : `Writing the CapCut draft ${r.name} ...`);
        await reloadVersions();
      }
    } catch (e) {
      setStatus(e.message + (e.hint ? ` Fix: ${e.hint}.` : ""), true);
    } finally {
      btn.disabled = false;
    }
  };
  const bRender = h("button", { class: "btn primary", type: "button", id: "ed-render" }, icon("sparkles", 16), "Render");
  const bDraft = h("button", { class: "btn", type: "button", id: "ed-draft", title: "Write a new CapCut draft of this edit" }, icon("film", 16), "Save as CapCut draft");
  const bSave = h("button", { class: "btn", type: "button", id: "ed-save" }, icon("check", 16), "Save");
  const bDiscard = h("button", { class: "btn ghost", type: "button", id: "ed-discard", title: "Drop the saved changes and go back to the last rendered version" }, "Discard changes");
  bRender.addEventListener("click", () => act("render", bRender));
  bDraft.addEventListener("click", () => act("draft", bDraft));
  bSave.addEventListener("click", () => act("save", bSave));
  bDiscard.addEventListener("click", () => act("undo", bDiscard));

  // ---- versions and Before/After compare --------------------------------------------------------------------

  const versions = h("div", { class: "card stack", id: "ed-versions" });
  let cmpPick = null;
  function renderVersions() {
    const hist = ctx.history.slice().reverse();
    const done = hist.filter((x) => x.kind === "render" && x.status === "done" && x.mp4_url);
    const pickV = done.find((x) => x.v === cmpPick) || done[0];
    const kids = [h("h2", { text: "Versions" })];
    if (!hist.length) kids.push(h("p", { class: "muted", text: "No edits rendered yet. Render makes a new MP4 (named _2, _3, ...) next to the original." }));
    for (const x of hist) {
      const canCmp = x.kind === "render" && x.status === "done" && x.mp4_url;
      kids.push(
        h(
          "div",
          { class: "variant" + (pickV && x.v === pickV.v ? " on" : ""), dataset: { v: String(x.v) } },
          h("strong", { text: `v${x.v}` }),
          h("span", { text: x.name }),
          h("span", { class: "muted", text: x.kind === "render" ? "MP4" : "CapCut draft" }),
          pill(x.status),
          x.gate ? pill(x.gate.pass ? "PASS" : "FAIL", x.gate.pass ? "audit pass" : "audit fail") : null,
          canCmp
            ? h("button", {
                class: "btn sm",
                type: "button",
                text: "Compare",
                dataset: { cmp: String(x.v) },
                onclick: () => {
                  cmpPick = x.v;
                  renderVersions();
                },
              })
            : null
        )
      );
    }
    if (pickV) kids.push(compareView(pickV));
    versions.replaceChildren(...kids);
  }
  function compareView(v) {
    const mk = (src, id) => (src ? h("video", { preload: "metadata", src, id, playsInline: true, muted: true }) : h("span", { class: "muted", text: "No MP4 of the original clip." }));
    const a = mk(ctx.mp4_url, "ed-before"), b = mk(v.mp4_url, "ed-after");
    const vids = [a, b].filter((x) => x.tagName === "VIDEO");
    const play = h("button", { class: "btn sm", type: "button", id: "ed-cmp-play" }, icon("play", 16), "Play both");
    const seek = h("input", { type: "range", min: "0", max: "1000", value: "0", id: "ed-cmp-seek", "aria-label": "Seek both videos" });
    play.addEventListener("click", () => {
      const go = vids.some((x) => x.paused);
      vids.forEach((x) => (go ? x.play().catch(() => {}) : x.pause()));
      play.replaceChildren(icon(go ? "pause" : "play", 16), go ? "Pause both" : "Play both");
    });
    seek.addEventListener("input", () => {
      vids.forEach((x) => {
        if (x.duration) x.currentTime = (Number(seek.value) / 1000) * x.duration;
      });
    });
    if (vids.length === 2) {
      // the After video follows the Before one (play, pause, seek and drift)
      a.addEventListener("timeupdate", () => {
        if (a.duration) seek.value = String(Math.round((a.currentTime / a.duration) * 1000));
        if (Math.abs(b.currentTime - a.currentTime) > 0.15 && !b.seeking) b.currentTime = Math.min(a.currentTime, b.duration || a.currentTime);
      });
      a.addEventListener("pause", () => b.pause());
    }
    return h(
      "div",
      { class: "stack", id: "ed-compare" },
      h("div", { class: "row tight" }, play, seek),
      h(
        "div",
        { class: "compare" },
        h("div", { class: "stack" }, h("span", { class: "label", text: "Before (original)" }), h("div", { class: "frame" }, a)),
        h("div", { class: "stack" }, h("span", { class: "label", text: `After (${v.name})` }), h("div", { class: "frame" }, b))
      ),
      h("details", {}, h("summary", { text: "Export audit of the original" }), gateTable(ctx.gate)),
      h("details", { open: true }, h("summary", { text: `Export audit of ${v.name}` }), gateTable(v.gate))
    );
  }
  async function reloadVersions() {
    try {
      ctx = Object.assign(ctx, { history: (await api("GET", `${base}/edit`)).history });
    } catch (e) {
      return;
    }
    if (alive) renderVersions();
  }
  every(2500, () => {
    if (ctx.history.some((x) => ["queued", "running"].includes(x.status))) reloadVersions();
  });

  // ---- page ---------------------------------------------------------------------------------------------------

  const toolbar = h(
    "div",
    { class: "card ed-toolbar", id: "ed-toolbar" },
    h("div", { class: "tb-group" }, playBtn, timeTxt),
    h("div", { class: "tb-group" }, bSplit, layoutSel, segInfo),
    h("div", { class: "tb-group" }, bAddKey, easeSel, bAuto),
    h("div", { class: "tb-group" }, bUndo, bRedo, bMore)
  );
  root.replaceChildren(
    h(
      "div",
      { class: "page-head" },
      h("div", {}, h("h1", { text: `Edit ${key}` }), h("p", { class: "muted", text: "Choose what each part of the clip shows. Split at the playhead to give a part its own layout and crop; keyframes pan the crop." })),
      h("div", { class: "row tight" }, bDiscard, bSave, bDraft, bRender, h("a", { class: "btn ghost", href: `#/project/${enc(jobId)}` }, icon("chevron-left", 16), "Back to clips"))
    ),
    toolbar,
    h(
      "div",
      { class: "ed-main" },
      h(
        "div",
        { class: "card stack ed-source" },
        h("div", { class: "row between" }, h("h2", { text: "Source", style: { margin: 0 } }), h("span", { class: "hint", text: "Drag a box to move it; drag its corner to resize. With keyframes, dragging at the playhead moves the crop at that time." })),
        wrap,
        ctx.original_source ? null : h("p", { class: "hint", text: "Only the clip's window is available (the original stream file is not on this PC)." }),
        tl,
        h("div", { class: "row tight" }, field("In (s)", inIn), field("Out (s)", outIn), lenTxt, h("span", { class: "spacer" }), h("label", { class: "check" }, snapBox, "Snap to words"))
      ),
      h(
        "div",
        { class: "card stack ed-out" },
        h("div", { class: "row between" }, h("h2", { text: "Output", style: { margin: 0 } }), h("span", { class: "chip" }, ctx.ratio)),
        preview,
        h("div", { class: "row tight" }, capAuto, faceWarn),
        h("p", { class: "hint", text: "Live preview of the render. Drag the captions up or down; they snap to a safe zone." })
      )
    ),
    h(
      "div",
      { class: "editor" },
      h(
        "div",
        { class: "stack" },
        h("div", { class: "card stack" }, h("h2", { text: "Caption words" }), h("p", { class: "hint", text: "Fix a misspelled word in place (or click its chip on the timeline). Timings stay the same." }), h("div", { class: "words", id: "ed-words" }, wordInputs)),
        versions
      ),
      h(
        "div",
        { class: "stack" },
        h("div", { class: "card stack" }, h("h2", { text: "Caption style" }), capPick, h("div", { class: "grid2" }, field("Caption position", posSel), field("Highlight colour", hlSel))),
        h("div", { class: "card stack" }, h("h2", { text: "Hook" }), field("Hook headline", hookIn), hookPick),
        fxCard,
        h("div", { class: "card stack" }, h("h2", { text: "Post text" }), field("Title", titleIn), field("Description", descIn), field("Hashtags", tagsIn)),
        h("div", { class: "card stack" }, status)
      )
    )
  );
  form = normalize(ctx.pending || ctx.current);
  syncAll();
  renderVersions();
}
