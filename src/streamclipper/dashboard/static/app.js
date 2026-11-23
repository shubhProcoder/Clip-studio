/* StreamClipper dashboard. Plain JS, no libraries. Every text from the server goes in via textContent.
   Scripts: icons.js (icon()), app.js (helpers, shell, router, pages), editor.js (pageEdit). */
"use strict";

const CSRF = document.querySelector('meta[name="csrf"]').content;
const MODE = document.querySelector('meta[name="mode"]').content;
const LAN = MODE === "lan";
const GUIDE = "https://github.com/shubhProcoder/Clip-studio/blob/main/docs/GUIDE.md";
let OPTS = null;
let timers = [];
let pageCleanup = [];
let routeSeq = 0;
let SEARCH = "";
const searchSubs = new Set();
const modals = new Set();
// keyboard shortcuts shown by "?" (the editor adds its own rows)
const SHORTCUTS = [
  ["/", "Search projects and clips"],
  ["?", "Show this list"],
  ["Esc", "Close a dialog or menu"],
];

// ---- small helpers ------------------------------------------------------------------------------------------

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "style") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (typeof v !== "string" && k in el) el[k] = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of kids.flat(3)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function sel(options, value, props) {
  const s = h(
    "select",
    props,
    options.map((o) => {
      const [v, l, dis] = Array.isArray(o) ? o : [o, o];
      return h("option", { value: v, text: l, disabled: !!dis });
    })
  );
  s.value = value === null || value === undefined ? "" : value;
  return s;
}

function field(label, control, hint) {
  return h("label", { class: "field" }, h("span", { text: label }), control, hint ? h("span", { class: "hint", text: hint }) : null);
}

const enc = encodeURIComponent;
const clone = (o) => JSON.parse(JSON.stringify(o));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const even = (v) => Math.round(v / 2) * 2;
const ACTIVE = ["queued", "running", "stopping"];

function every(ms, fn) {
  const id = setInterval(fn, ms);
  timers.push(id);
  return id;
}

function mmss(t) {
  t = Math.max(0, Math.floor(Number(t) || 0));
  const hh = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const p = (n) => String(n).padStart(2, "0");
  return hh ? `${hh}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
}

function when(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function ago(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 7 * 86400) {
    const d = Math.floor(s / 86400);
    return `${d} day${d > 1 ? "s" : ""} ago`;
  }
  return when(ts);
}

async function api(method, path, body) {
  const headers = {};
  let payload;
  if (method !== "GET") headers["X-CSRF-Token"] = CSRF;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const r = await fetch(path, { method, headers, body: payload, credentials: "same-origin", cache: "no-store" });
  let data = null;
  try {
    data = await r.json();
  } catch (e) {
    data = null;
  }
  if (!r.ok) {
    const err = new Error((data && data.error) || `Request failed (HTTP ${r.status})`);
    err.status = r.status;
    err.hint = data && data.hint;
    throw err;
  }
  return data;
}

function upload(file, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("PUT", "/api/upload?name=" + enc(file.name));
    x.setRequestHeader("X-CSRF-Token", CSRF);
    x.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    x.onload = () => {
      let d = {};
      try {
        d = JSON.parse(x.responseText);
      } catch (e) {
        d = {};
      }
      if (x.status >= 200 && x.status < 300) resolve(d);
      else reject(new Error(d.error || `Upload failed (HTTP ${x.status})`));
    };
    x.onerror = () => reject(new Error("Upload failed: the connection was lost."));
    x.send(file);
  });
}

function toast(msg, kind, hint) {
  const ic = { ok: "check-circle", error: "alert-triangle" }[kind] || "info";
  const t = h(
    "div",
    { class: "toast " + (kind || ""), role: kind === "error" ? "alert" : "status" },
    h("span", { class: "toast-ic" }, icon(ic, 18)),
    h("div", { class: "toast-msg" }, h("div", { text: msg }), hint ? h("div", { class: "hint", text: "Fix: " + hint }) : null),
    h("button", { class: "toast-x", type: "button", "aria-label": "Close", onclick: () => t.remove() }, icon("x", 14))
  );
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), kind === "error" ? 9000 : 4500);
}

function fail(e) {
  toast(e.message || String(e), "error", e.hint);
}

async function copyText(text) {
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (e) {
    ok = false;
  }
  if (!ok) {
    // LAN over http is not a secure context: textarea + execCommand fallback
    const ta = h("textarea", { style: { position: "fixed", opacity: "0", top: "0" } });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    try {
      ok = document.execCommand("copy");
    } catch (e) {
      ok = false;
    }
    ta.remove();
  }
  return ok;
}

async function copyWithToast(text, what) {
  if (await copyText(text)) toast(`${what} copied.`, "ok");
  else toast("Copy failed. Select the text and press Ctrl+C.", "error");
}

function copyBtn(text, label) {
  return h("button", { class: "btn sm", type: "button", "aria-label": label || "Copy", onclick: () => copyWithToast(text, label || "Text") }, icon("copy", 14), "Copy");
}

function iconBtn(name, label, props) {
  return h("button", { class: "icon-btn sm", type: "button", "aria-label": label, title: label, ...(props || {}) }, icon(name, 16));
}

const STATUS_LABELS = { queued: "Queued", running: "Running", stopping: "Stopping", done: "Done", failed: "Failed", cancelled: "Cancelled", interrupted: "Interrupted", stopped: "Stopped" };

function pill(status, label) {
  return h("span", { class: "pill " + status, text: label || STATUS_LABELS[status] || status });
}

function emptyState(ic, title, text, action) {
  return h("div", { class: "card empty" }, h("div", { class: "empty-ic" }, icon(ic, 28)), h("h3", { text: title }), h("p", { text }), action || null);
}

function errorBox(e) {
  return h(
    "div",
    { class: "card empty" },
    h("div", { class: "empty-ic bad" }, icon("alert-triangle", 28)),
    h("h3", { text: "Something went wrong" }),
    h("p", { class: "err", text: e.message || String(e) }),
    e.hint ? h("p", { class: "hint", text: "Fix: " + e.hint }) : null,
    h("a", { class: "btn", href: "#/home" }, "Go to Home")
  );
}

function spinner(text) {
  return h("div", { class: "loading" }, h("span", { class: "spinner" }), text || "Loading...");
}

function skeleton(kind, n) {
  const one = {
    project: () => h("div", { class: "card job-card skel-card" }, h("div", { class: "thumb skel" }), h("div", { class: "jc-body" }, h("div", { class: "skel line w70" }), h("div", { class: "skel line w40" }))),
    clip: () => h("div", { class: "card clip skel-card" }, h("div", { class: "frame skel" }), h("div", { class: "body" }, h("div", { class: "skel line w40" }), h("div", { class: "skel line w90" }), h("div", { class: "skel line w70" }))),
    line: () => h("div", { class: "skel line w90" }),
    block: () => h("div", { class: "card skel-card" }, h("div", { class: "skel line w40" }), h("div", { class: "skel line w90" }), h("div", { class: "skel line w70" })),
  }[kind];
  return Array.from({ length: n || 1 }, one);
}

function ring(pct, size) {
  const NS = "http://www.w3.org/2000/svg";
  const r = 15.5, c = 2 * Math.PI * r;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 36 36");
  svg.setAttribute("width", String(size || 40));
  svg.setAttribute("height", String(size || 40));
  svg.setAttribute("aria-hidden", "true");
  const circ = (cls, dash) => {
    const e = document.createElementNS(NS, "circle");
    e.setAttribute("cx", "18");
    e.setAttribute("cy", "18");
    e.setAttribute("r", String(r));
    e.setAttribute("class", cls);
    if (dash !== undefined) {
      e.setAttribute("stroke-dasharray", `${c}`);
      e.setAttribute("stroke-dashoffset", `${dash}`);
      e.setAttribute("transform", "rotate(-90 18 18)");
    }
    return e;
  };
  svg.append(circ("ring-bg"), circ("ring-fg", c * (1 - (pct === null ? 25 : pct) / 100)));
  return h(
    "span",
    { class: "ring" + (pct === null ? " indet" : ""), role: "progressbar", "aria-label": "Progress", "aria-valuenow": pct === null ? null : String(pct), "aria-valuemin": "0", "aria-valuemax": "100" },
    svg,
    pct === null ? null : h("span", { class: "ring-txt", text: `${pct}%` })
  );
}

// ---- modal, confirm, menu -----------------------------------------------------------------------------------

function openModal(title, body, actions, opts) {
  const prev = document.activeElement;
  const back = h("div", { class: "modal-back" });
  const m = { onclose: null };
  const dlg = h(
    "div",
    { class: "modal" + (opts && opts.wide ? " wide" : ""), role: "dialog", "aria-modal": "true", "aria-label": title, tabindex: "-1" },
    h("div", { class: "modal-head" }, h("h2", { text: title }), iconBtn("x", "Close", { onclick: () => close(false) })),
    h("div", { class: "modal-body" }, body),
    actions && actions.length ? h("div", { class: "modal-foot" }, actions) : null
  );
  back.append(dlg);
  let done = false;
  function close(v) {
    if (done) return;
    done = true;
    back.remove();
    modals.delete(close);
    document.removeEventListener("keydown", onKey, true);
    if (prev && prev.focus && document.contains(prev)) prev.focus();
    if (m.onclose) m.onclose(v);
  }
  function onKey(e) {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close(false);
    } else if (e.key === "Tab") {
      const f = [...dlg.querySelectorAll("button:not([disabled]), input, select, textarea, a[href], video[controls]")];
      if (!f.length) return;
      const i = f.indexOf(document.activeElement);
      if (e.shiftKey && i <= 0) {
        e.preventDefault();
        f[f.length - 1].focus();
      } else if (!e.shiftKey && i === f.length - 1) {
        e.preventDefault();
        f[0].focus();
      }
    }
  }
  back.addEventListener("mousedown", (e) => e.target === back && close(false));
  document.addEventListener("keydown", onKey, true);
  document.body.append(back);
  modals.add(close);
  setTimeout(() => (dlg.querySelector("[data-autofocus]") || dlg.querySelector(".modal-body button, .modal-body input, .modal-body select, .modal-foot button") || dlg).focus(), 0);
  m.close = close;
  m.el = dlg;
  return m;
}

function confirmModal(title, text, okLabel, danger) {
  return new Promise((resolve) => {
    const ok = h("button", { class: "btn " + (danger === false ? "primary" : "danger-solid"), type: "button", id: "confirm-ok", text: okLabel || "OK", "data-autofocus": true });
    const cancel = h("button", { class: "btn", type: "button", text: "Cancel" });
    const m = openModal(title, h("p", { class: "muted", text }), [cancel, ok]);
    m.onclose = (v) => resolve(v === true);
    ok.addEventListener("click", () => m.close(true));
    cancel.addEventListener("click", () => m.close(false));
  });
}

let openMenuEl = null;

function closeMenu(focusBtn) {
  if (!openMenuEl) return;
  const b = openMenuEl._btn;
  openMenuEl.remove();
  b.setAttribute("aria-expanded", "false");
  openMenuEl = null;
  if (focusBtn && document.contains(b)) b.focus();
}

function menuBtn(items, label) {
  const b = h("button", { class: "icon-btn sm", type: "button", "aria-label": label || "More actions", title: label || "More actions", "aria-haspopup": "menu", "aria-expanded": "false" }, icon("more-horizontal", 16));
  b.addEventListener("click", (ev) => {
    ev.stopPropagation();
    ev.preventDefault();
    if (openMenuEl && openMenuEl._btn === b) return closeMenu();
    closeMenu();
    const list = (typeof items === "function" ? items() : items).filter(Boolean);
    const m = h(
      "div",
      { class: "menu", role: "menu", "aria-label": label || "More actions" },
      list.map((it) =>
        it === "-"
          ? h("div", { class: "menu-sep", role: "separator" })
          : h(
              "button",
              {
                class: "menu-item" + (it.danger ? " danger" : ""),
                type: "button",
                role: "menuitem",
                disabled: !!it.disabled,
                title: it.title || null,
                onclick: (e) => {
                  e.stopPropagation();
                  closeMenu();
                  it.onClick();
                },
              },
              it.icon ? icon(it.icon, 16) : h("span", { class: "ic-pad" }),
              h("span", { text: it.label })
            )
      )
    );
    m._btn = b;
    m.addEventListener("keydown", (e) => {
      const f = [...m.querySelectorAll(".menu-item:not([disabled])")];
      const i = f.indexOf(document.activeElement);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        f[(i + (e.key === "ArrowDown" ? 1 : f.length - 1)) % f.length].focus();
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        closeMenu(true);
      } else if (e.key === "Tab") closeMenu();
    });
    document.body.append(m);
    const r = b.getBoundingClientRect();
    const mw = m.offsetWidth, mh = m.offsetHeight;
    const left = clamp(r.right - mw, 8, window.innerWidth - mw - 8);
    let top = r.bottom + 4;
    if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);
    m.style.left = `${left + window.scrollX}px`;
    m.style.top = `${top + window.scrollY}px`;
    b.setAttribute("aria-expanded", "true");
    m._opened = performance.now();
    openMenuEl = m;
    const first = m.querySelector(".menu-item:not([disabled])");
    if (first) first.focus({ preventScroll: true });
  });
  return b;
}

// ---- shell: theme, sidebar, search, shortcuts ---------------------------------------------------------------

function setupShell() {
  document.querySelectorAll("[data-icon]").forEach((el) => el.replaceChildren(icon(el.dataset.icon, el.classList.contains("nav-ic") ? 20 : 18)));
  const btn = document.getElementById("theme-toggle");
  const label = () => {
    const dark = document.documentElement.getAttribute("data-theme") === "dark";
    btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
    btn.title = btn.getAttribute("aria-label");
  };
  label();
  btn.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("sc_theme", next);
    } catch (e) {
      /* private mode: the theme just is not remembered */
    }
    label();
  });
  const side = document.getElementById("side-collapse");
  const sideLabel = () => {
    const c = document.documentElement.getAttribute("data-side") === "collapsed";
    side.setAttribute("aria-label", c ? "Expand the sidebar" : "Collapse the sidebar");
    side.title = side.getAttribute("aria-label");
    side.setAttribute("aria-expanded", String(!c));
  };
  sideLabel();
  side.addEventListener("click", () => {
    const c = document.documentElement.getAttribute("data-side") !== "collapsed";
    if (c) document.documentElement.setAttribute("data-side", "collapsed");
    else document.documentElement.removeAttribute("data-side");
    try {
      localStorage.setItem("sc_side", c ? "collapsed" : "open");
    } catch (e) {
      /* not remembered */
    }
    sideLabel();
  });
  const search = document.getElementById("search");
  search.addEventListener("input", () => {
    SEARCH = search.value.trim().toLowerCase();
    if (!searchSubs.size && SEARCH) location.hash = "#/projects";
    searchSubs.forEach((f) => f(SEARCH));
  });
  document.getElementById("shortcuts-btn").addEventListener("click", shortcutsSheet);
  document.querySelector(".skip").addEventListener("click", (e) => {
    e.preventDefault();
    document.getElementById("app").focus();
  });
  if (LAN) document.getElementById("mode-badge").hidden = false;
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    const typing = t && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName));
    if (typing) {
      if (e.key === "Escape" && t.id === "search") t.blur();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey || modals.size) return;
    if (e.key === "/") {
      e.preventDefault();
      search.focus();
      search.select();
    } else if (e.key === "?") {
      e.preventDefault();
      shortcutsSheet();
    }
  });
  document.addEventListener("click", () => closeMenu());
  window.addEventListener("resize", () => closeMenu());
  // a scroll right after opening (the browser bringing the button into view, images loading) keeps the menu
  window.addEventListener("scroll", () => openMenuEl && performance.now() - openMenuEl._opened > 400 && closeMenu(), true);
}

function onSearch(fn) {
  searchSubs.add(fn);
  pageCleanup.push(() => searchSubs.delete(fn));
}

function matches(text) {
  return !SEARCH || String(text || "").toLowerCase().includes(SEARCH);
}

function shortcutsSheet() {
  if (modals.size) return;
  openModal(
    "Keyboard shortcuts",
    h("table", { class: "keys" }, h("tbody", {}, SHORTCUTS.map(([k, d]) => h("tr", {}, h("td", {}, ...k.split(" / ").map((x, i) => [i ? " / " : "", h("kbd", { text: x })])), h("td", { text: d }))))),
    [],
    {}
  );
}

// ---- router -------------------------------------------------------------------------------------------------

const LEGACY = { new: "home", jobs: "projects", job: "project" };
const ROUTES = {
  home: (r) => pageHome(r),
  projects: (r) => pageProjects(r),
  project: (r, id) => pageProject(r, id),
  edit: (r, job, key) => pageEdit(r, job, key),
  live: (r) => pageLive(r),
  styles: (r) => pageStyles(r),
  settings: (r) => pageSettings(r),
};

async function route() {
  timers.forEach(clearInterval);
  timers = [];
  pageCleanup.forEach((f) => f());
  pageCleanup = [];
  closeMenu();
  [...modals].forEach((close) => close(false));
  let parts;
  try {
    parts = (location.hash.replace(/^#\/?/, "") || "home").split("/").map((p) => decodeURIComponent(p));
  } catch (e) {
    parts = ["home"];
  }
  if (LEGACY[parts[0]]) {
    // old links keep working; replace() so Back does not bounce through the old address
    location.replace("#/" + [LEGACY[parts[0]], ...parts.slice(1)].map(enc).join("/"));
    return;
  }
  const name = ROUTES[parts[0]] ? parts[0] : "home";
  const navName = name === "project" || name === "edit" ? "projects" : name;
  document.querySelectorAll(".nav a").forEach((a) => {
    const on = a.dataset.route === navName;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  document.body.dataset.page = name;
  // each route renders into its own container: a slow page that finishes after the user moved on
  // writes into a detached element instead of over the new page
  const seq = ++routeSeq;
  const root = h("div", { class: "view" }, spinner());
  const app = document.getElementById("app");
  app.replaceChildren(root);
  window.scrollTo(0, 0);
  if (document.activeElement !== document.getElementById("search")) app.focus({ preventScroll: true });
  try {
    if (!OPTS) OPTS = await api("GET", "/api/options");
    if (seq !== routeSeq) return;
    await ROUTES[name](root, ...parts.slice(1));
  } catch (e) {
    root.replaceChildren(errorBox(e));
  }
}

// ---- style picker (sprites of styles_gallery.jpg) -----------------------------------------------------------

function sprite(t) {
  const g = OPTS.gallery;
  const sp = h("div", { class: "sprite", role: "img", "aria-label": t.name });
  const apply = () => {
    const w = sp.clientWidth || 150;
    const s = w / g.tile_w;
    sp.style.height = `${g.tile_h * s}px`;
    sp.style.backgroundImage = `url("${g.url}")`;
    sp.style.backgroundSize = `${g.width * s}px ${g.height * s}px`;
    sp.style.backgroundPosition = `${-t.x * s}px ${-t.y * s}px`;
  };
  const ro = new ResizeObserver(apply);
  ro.observe(sp);
  pageCleanup.push(() => ro.disconnect());
  return sp;
}

function autoArt() {
  return h("div", { class: "auto-art", "aria-hidden": "true" }, h("strong", { text: "AUTO" }), h("span", { text: "picks per clip" }));
}

function tileOf(kind, name) {
  return OPTS.gallery.tiles.find((x) => x.kind === kind && x.name === name);
}

function picker(kind, value, onPick) {
  const items = kind === "caption" ? OPTS.caption_styles : OPTS.hook_styles;
  const wrap = h("div", { class: "picker", role: "radiogroup", "aria-label": kind === "caption" ? "Caption style" : "Hook style" });
  const tiles = [];
  for (const name of items) {
    const t = tileOf(kind, name);
    const look = kind === "caption" ? OPTS.caption_looks[name] : OPTS.hook_looks[name];
    const btn = h("button", {
      type: "button",
      class: "tile" + (name === value ? " on" : "") + (t ? "" : " auto"),
      role: "radio",
      "aria-checked": String(name === value),
      title: look || "Picked per clip from its category",
      dataset: { name },
    });
    btn.append(t ? sprite(t) : autoArt());
    btn.append(h("span", { class: "name", text: name === "auto" ? "auto (per clip)" : name }));
    btn.addEventListener("click", () => {
      tiles.forEach((x) => {
        x.classList.remove("on");
        x.setAttribute("aria-checked", "false");
      });
      btn.classList.add("on");
      btn.setAttribute("aria-checked", "true");
      onPick(name);
    });
    tiles.push(btn);
    wrap.append(btn);
  }
  return wrap;
}

// ---- shared job bits ----------------------------------------------------------------------------------------

const STAGES = {
  queued: "Waiting in the queue",
  starting: "Starting",
  download: "Downloading",
  analysis: "Analysing audio",
  transcribe: "Transcribing",
  scoring: "Scoring moments",
  render: "Rendering",
  finishing: "Finishing",
  live: "Live",
  stopping: "Stopping",
};
const KINDS = { vod: "Recorded stream", podcast: "Podcast", live: "Live", render: "Re-render", draft: "CapCut draft" };
const SRC_ICONS = { twitch: "twitch", youtube: "youtube", kick: "kick", link: "link", file: "file-video" };
const SRC_NAMES = { twitch: "Twitch", youtube: "YouTube", kick: "Kick", link: "Link", file: "Video file" };

function jobPct(job) {
  const p = job.progress || {};
  return p.k && p.n ? clamp(Math.round((100 * p.k) / p.n), 0, 100) : null;
}

function stageText(job) {
  const p = job.progress || {};
  const ENDED = { done: "Finished", failed: "Failed", cancelled: "Cancelled", stopped: "Stopped", interrupted: "Interrupted: click Resume to continue" };
  const parts = [ENDED[job.status] || STAGES[job.stage] || STAGES[p.stage] || job.stage];
  if (p.detail && job.status === "running") parts.push(p.detail);
  if (job.queue_pos) parts.push(`#${job.queue_pos} in the queue`);
  if (p.error) parts.push(p.error);
  return parts.filter(Boolean).join(" · ");
}

function progressBlock(job) {
  const running = job.status === "running";
  const pct = jobPct(job);
  const bar = h("div", { class: "progress" + (running && pct === null ? " indet" : "") }, h("span", { style: { width: pct === null ? (job.status === "done" ? "100%" : "35%") : `${pct}%` } }));
  return h("div", { class: "prog" }, h("div", { class: "small muted", text: stageText(job) }), running || pct !== null ? bar : null);
}

async function jobAct(job, path, refresh) {
  try {
    await api("POST", `/api/jobs/${enc(job.id)}/${path}`);
    toast({ cancel: "Cancelled.", stop: "Stopping the live session ...", resume: "Resumed: back in the queue." }[path] || "Done.", "ok");
    refresh();
  } catch (e) {
    fail(e);
  }
}

function jobActionItems(job, refresh) {
  const items = [];
  if (job.kind === "live" && job.status === "running") items.push({ label: "Stop", icon: "square", onClick: () => jobAct(job, "stop", refresh) });
  if (["queued", "running"].includes(job.status)) items.push({ label: "Cancel", icon: "x", danger: true, onClick: () => jobAct(job, "cancel", refresh) });
  if (["failed", "cancelled", "interrupted", "stopped"].includes(job.status) && job.kind !== "draft") items.push({ label: "Resume", icon: "rotate-ccw", onClick: () => jobAct(job, "resume", refresh) });
  return items;
}

function jobActions(job, refresh) {
  return jobActionItems(job, refresh).map((it) => h("button", { class: "btn sm" + (it.danger ? " danger" : ""), type: "button", onclick: (ev) => (ev.stopPropagation(), it.onClick()) }, icon(it.icon, 14), it.label));
}

async function showLog(job) {
  const pre = h("pre", { class: "log", text: "Loading ..." });
  openModal(`Log: ${job.label}`, pre, [h("a", { class: "btn sm", href: `/files/${enc(job.id)}/log.txt`, target: "_blank", rel: "noopener" }, icon("external-link", 14), "Full log")], { wide: true });
  try {
    pre.textContent = (await api("GET", `/api/jobs/${enc(job.id)}`)).log_tail || "(empty)";
  } catch (e) {
    pre.textContent = e.message;
  }
}

function logBlock(job, openSet) {
  const pre = h("pre", { class: "log", text: job.log_tail || "" });
  const d = h("details", { class: "log-drawer", open: openSet.has(job.id) }, h("summary", {}, icon("file-text", 14), "Log"), pre);
  d.addEventListener("toggle", async () => {
    if (d.open) {
      openSet.add(job.id);
      try {
        pre.textContent = (await api("GET", `/api/jobs/${enc(job.id)}`)).log_tail || "(empty)";
      } catch (e) {
        pre.textContent = e.message;
      }
    } else openSet.delete(job.id);
  });
  return d;
}

function jobCard(j, refresh) {
  const active = ACTIVE.includes(j.status);
  const pct = jobPct(j);
  const kind = j.source_kind || "file";
  const thumb = j.thumb_url ? h("img", { src: j.thumb_url, alt: "", loading: "lazy" }) : h("div", { class: "thumb-ph" }, icon(SRC_ICONS[kind] || "file-video", 34));
  const n = j.clip_count || 0;
  const items = () => [
    { label: "Open", icon: "folder", onClick: () => (location.hash = `#/project/${enc(j.id)}`) },
    { label: "View log", icon: "file-text", onClick: () => showLog(j) },
    ...(jobActionItems(j, refresh).length ? ["-", ...jobActionItems(j, refresh)] : []),
  ];
  return h(
    "article",
    { class: "card job-card job", dataset: { id: j.id, status: j.status } },
    h("a", { class: "cover-link", href: `#/project/${enc(j.id)}`, "aria-label": `Open ${j.label}` }),
    h(
      "div",
      { class: "thumb" },
      thumb,
      h("div", { class: "thumb-top" }, pill(j.status), j.kind !== "vod" ? h("span", { class: "chip dark", text: KINDS[j.kind] || j.kind }) : null),
      active ? h("div", { class: "thumb-ring" }, ring(pct, 44)) : null
    ),
    h(
      "div",
      { class: "jc-body" },
      h("div", { class: "jc-title-row" }, h("span", { class: "src-ic src-" + kind, title: SRC_NAMES[kind] || kind }, icon(SRC_ICONS[kind] || "file-video", 16)), h("div", { class: "title jc-title", title: j.label, text: j.label })),
      h("div", { class: "jc-meta" }, h("span", { text: active ? stageText(j) : `${n} clip${n === 1 ? "" : "s"}` }), h("span", { class: "dot-sep", text: "·" }), h("span", { text: ago(j.created), title: when(j.created) })),
      h("div", { class: "jc-menu" }, menuBtn(items, "Project actions"))
    )
  );
}

const TOP_KINDS = ["vod", "podcast", "live"];

// ---- home ---------------------------------------------------------------------------------------------------

function looksLive(src) {
  try {
    const u = new URL(src);
    const host = u.hostname.replace(/^(www|m)\./, "");
    const p = u.pathname.split("/").filter(Boolean);
    if (host === "twitch.tv") return p.length === 1 && !["videos", "directory", "downloads"].includes(p[0]);
    if (host === "kick.com") return p.length === 1;
  } catch (e) {
    return false;
  }
  return false;
}

const ZOOM_OPTS = [
  ["off", "Off"],
  ["light", "Light (1.08x)"],
  ["medium", "Medium (1.12x)"],
  ["strong", "Strong (1.2x)"],
];
const ENHANCE_OPTS = [
  ["off", "Off"],
  ["light", "Light (keeps more game sound)"],
  ["strong", "Strong"],
];

function uploadBrand(file) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("PUT", "/api/brand/upload?name=" + enc(file.name));
    x.setRequestHeader("X-CSRF-Token", CSRF);
    x.onload = () => {
      let d = {};
      try {
        d = JSON.parse(x.responseText);
      } catch (e) {
        d = {};
      }
      if (x.status >= 200 && x.status < 300) resolve(d);
      else reject(Object.assign(new Error(d.error || `Upload failed (HTTP ${x.status})`), { hint: d.hint }));
    };
    x.onerror = () => reject(new Error("Upload failed: the connection was lost."));
    x.send(file);
  });
}

// Settings > Brand kit: files are uploaded into <workspace>/brand/, the server checks and writes brand_kit.json
async function brandCard(box) {
  let b;
  try {
    b = await api("GET", "/api/brand");
  } catch (e) {
    box.replaceChildren(errorBox(e));
    return;
  }
  const k = b.kit || {};
  const ro = b.read_only;
  const none = ["", "None"];
  const imgOpts = () => [none, ...b.files.images.map((n) => [n, n])];
  const fontOpts = () => [["", "The caption style's font"], ...b.files.fonts.map((n) => [n, n])];
  const logo = k.logo || {}, intro = k.intro || {}, outro = k.outro || {}, cols = k.colours || {};
  const logoSel = sel(imgOpts(), logo.image || "", { id: "br-logo" });
  const posSel = sel(["auto", "top-left", "top-right", "bottom-left", "bottom-right"], logo.position || "auto", { id: "br-pos" });
  const widthIn = h("input", { type: "number", min: "5", max: "25", step: "1", value: String(logo.width_pct || 12), id: "br-width" });
  const opIn = h("input", { type: "number", min: "0.2", max: "1", step: "0.05", value: String(logo.opacity || 0.85), id: "br-opacity" });
  const handleIn = h("input", { type: "text", maxLength: 30, value: k.handle || "", placeholder: "@yourname", id: "br-handle" });
  const fontSel = sel(fontOpts(), k.font || "", { id: "br-font" });
  const hlIn = h("input", { type: "text", maxLength: 7, value: cols.highlight || "", placeholder: "#FFE600", id: "br-hl" });
  const boxIn = h("input", { type: "text", maxLength: 7, value: cols.hook_box || "", placeholder: "#FFE600", id: "br-box" });
  const card = (c, id) => ({
    img: sel(imgOpts(), c.image || "", { id: `br-${id}-img` }),
    text: h("input", { type: "text", maxLength: 60, value: c.text || "", id: `br-${id}-text` }),
    secs: h("input", { type: "number", min: "0.5", max: "1.5", step: "0.1", value: String(c.seconds || 1.0), id: `br-${id}-secs` }),
  });
  const ci = card(intro, "intro"), co = card(outro, "outro");
  const fileIn = h("input", { type: "file", accept: ".png,.jpg,.jpeg,.ttf,.otf", hidden: true, id: "br-file" });
  const bUp = h("button", { class: "btn", type: "button", id: "br-upload", onclick: () => fileIn.click() }, icon("upload", 16), "Upload image or font");
  fileIn.addEventListener("change", async () => {
    const f = fileIn.files[0];
    if (!f) return;
    bUp.disabled = true;
    try {
      await uploadBrand(f);
      toast(`Uploaded ${f.name}.`, "ok");
      brandCard(box);
    } catch (e) {
      fail(e);
    } finally {
      bUp.disabled = false;
    }
  });
  const bSave = h("button", { class: "btn primary", type: "button", id: "br-save" }, "Save brand kit");
  const bClear = h("button", { class: "btn ghost", type: "button", id: "br-clear" }, "Remove brand kit");
  const cardBody = (c) => ({ image: c.img.value, text: c.text.value, seconds: Number(c.secs.value) });
  bSave.addEventListener("click", async () => {
    bSave.disabled = true;
    try {
      const body = { logo: logoSel.value ? { image: logoSel.value, position: posSel.value, width_pct: Number(widthIn.value), opacity: Number(opIn.value) } : null, handle: handleIn.value, font: fontSel.value, colours: { highlight: hlIn.value.trim(), hook_box: boxIn.value.trim() }, intro: cardBody(ci), outro: cardBody(co) };
      const r = await api("POST", "/api/brand", body);
      toast(r.message, "ok");
      OPTS = null;
    } catch (e) {
      fail(e);
    } finally {
      bSave.disabled = false;
    }
  });
  bClear.addEventListener("click", async () => {
    if (!(await confirmModal("Remove the brand kit?", "The uploaded files stay in the brand folder.", "Remove"))) return;
    try {
      const r = await api("POST", "/api/brand", { clear: true });
      toast(r.message, "ok");
      OPTS = null;
      brandCard(box);
    } catch (e) {
      fail(e);
    }
  });
  const kids = [
    b.error ? h("p", { class: "err", text: "The saved brand kit does not load: " + b.error }) : null,
    h("div", { class: "grid3" }, field("Logo or watermark", logoSel), field("Logo corner", posSel, "Auto picks a corner clear of captions, faces and the app's buttons"), field("Logo width (% of the video)", widthIn)),
    h("div", { class: "grid3" }, field("Logo opacity (0.2-1)", opIn), field("Handle", handleIn, "Shown small in a free corner"), field("Caption font", fontSel)),
    h("div", { class: "grid3" }, field("Highlight colour", hlIn, "Nearest caption colour is used"), field("Hook box colour", boxIn)),
    h("div", { class: "grid3" }, field("Intro image", ci.img), field("Intro text", ci.text), field("Intro seconds", ci.secs)),
    h("div", { class: "grid3" }, field("Outro image", co.img), field("Outro text", co.text), field("Outro seconds", co.secs)),
    h("p", { class: "hint warn-hint", id: "br-intro-warn" }, icon("info", 14), " An intro card delays the hook, and the first 2 seconds decide if people keep watching. Leave it empty or keep it at most 1.5 s. Logo and cards are in the MP4 only; CapCut drafts get the handle, font and colours."),
    ro ? h("p", { class: "muted", text: "Network mode: the brand kit can be changed only on this PC." }) : h("div", { class: "row tight" }, bSave, bUp, b.kit ? bClear : null, fileIn),
  ];
  box.replaceChildren(...kids);
  if (ro) box.querySelectorAll("input, select").forEach((x) => (x.disabled = true));
}

async function pageHome(root) {
  const d = OPTS.defaults || {};
  const st = { mode: "auto", preset: d.preset || "", ratio: "9:16", min_score: 50, max_clips: 3, llm: "none", render: true, drafts: false, upload: "", uploading: false, zoom: "off", enhance_speech: "off", remove_fillers: false, brand_kit: false, creator_id: "default", game: "", static_analysis: true, static_bucket: 2, static_min: 6, static_threshold: 0.4 };
  const narrow = window.matchMedia("(max-width: 640px)");
  const srcIn = h("input", { type: "text", id: "src-url", class: "hero-input", placeholder: narrow.matches ? "Paste a link or drop a video" : "Paste a stream link or drop a video", autocomplete: "off", spellcheck: "false", "aria-label": "Stream link or video" });
  const fileIn = h("input", { type: "file", id: "src-file", accept: "video/*,.mkv,.ts,.flv,.m4v", hidden: true });
  const pick = h("button", { class: "btn ghost", type: "button", id: "pick-file", onclick: () => fileIn.click() }, icon("upload", 16), h("span", { class: "hide-sm", text: "Choose file" }));
  const go = h("button", { class: "btn primary lg", type: "submit", id: "home-go" }, icon("sparkles", 18), "Get clips");
  const upRow = h("div", { class: "up-row", hidden: true });
  const detect = h("p", { class: "hint detect", "aria-live": "polite" });
  const err = h("p", { class: "err", role: "alert" });
  const defaultHint = LAN ? "Twitch, YouTube or Kick links, or upload a video." : "Twitch, YouTube or Kick links, the full path of a video on this PC, or drop a file.";
  const showDetect = () => {
    const v = srcIn.value.trim();
    if (st.upload || st.uploading) return (detect.textContent = "");
    if (!v) detect.textContent = defaultHint;
    else if (/^https?:\/\//i.test(v)) detect.textContent = looksLive(v) && st.mode === "auto" ? "Live channel: clips are made while it streams." : "Recorded stream or video link.";
    else detect.textContent = LAN ? "On the network only links and uploads work." : "Video file on this PC.";
  };
  srcIn.addEventListener("input", () => {
    err.textContent = "";
    showDetect();
  });
  const clearUpload = () => {
    st.upload = "";
    st.uploading = false;
    upRow.hidden = true;
    upRow.replaceChildren();
    srcIn.disabled = false;
    fileIn.value = "";
    showDetect();
  };
  const doUpload = async (f) => {
    if (!f) return;
    err.textContent = "";
    st.upload = "";
    st.uploading = true;
    srcIn.value = "";
    srcIn.disabled = true;
    const bar = h("div", { class: "progress" }, h("span", { style: { width: "0%" } }));
    const msg = h("span", { class: "small", text: `Uploading ${f.name} ...` });
    upRow.hidden = false;
    upRow.replaceChildren(h("span", { class: "up-ic" }, icon("file-video", 18)), h("div", { class: "up-main" }, msg, bar), iconBtn("x", "Remove the video", { onclick: clearUpload }));
    showDetect();
    try {
      const res = await upload(f, (p) => (bar.firstChild.style.width = `${Math.round(p * 100)}%`));
      if (!st.uploading) return;
      st.upload = res.upload;
      st.uploading = false;
      msg.textContent = "Uploaded: " + res.upload;
      bar.classList.add("done");
    } catch (e) {
      clearUpload();
      fail(e);
    }
  };
  fileIn.addEventListener("change", () => doUpload(fileIn.files[0]));

  // advanced drawer
  const presetSel = sel([["", "Auto (streamer split)"], ...OPTS.presets.filter((p) => p !== "hook-grabber-9x16")], st.preset, { id: "adv-preset", onchange: (e) => (st.preset = e.target.value) });
  const presetField = field("Layout preset", presetSel);
  const modeSel = sel(
    [
      ["auto", "Auto (from the link)"],
      ["vod", "Recorded stream (VOD)"],
      ["podcast", "Podcast or interview"],
      ["live", "Live stream"],
    ],
    st.mode,
    { id: "adv-mode" }
  );
  modeSel.addEventListener("change", () => {
    st.mode = modeSel.value;
    presetField.hidden = st.mode === "podcast";
    showDetect();
  });
  const scoreVal = h("strong", { text: String(st.min_score) });
  const score = h("input", { type: "range", min: 0, max: 100, step: 5, value: String(st.min_score), "aria-label": "Minimum score", id: "adv-score" });
  score.addEventListener("input", () => {
    st.min_score = Number(score.value);
    scoreVal.textContent = score.value;
  });
  const maxIn = h("input", { type: "number", min: "0", max: "50", value: String(st.max_clips), id: "adv-max" });
  maxIn.addEventListener("change", () => (st.max_clips = Number(maxIn.value)));
  const llmOpts = [["none", "None (rule-based titles)"]];
  llmOpts.push(OPTS.llms.includes("ollama") ? ["ollama", "Ollama (on this PC)"] : ["ollama", "Ollama (not running)", true]);
  llmOpts.push(OPTS.llms.includes("gemini") ? ["gemini", "Gemini"] : ["gemini", "Gemini (needs GEMINI_API_KEY)", true]);
  const llmSel = sel(llmOpts, "none", { id: "adv-llm", onchange: (e) => (st.llm = e.target.value) });
  const ratioSel = sel(OPTS.ratios, st.ratio, { id: "adv-ratio", onchange: (e) => (st.ratio = e.target.value) });
  const creatorOpts = (OPTS.creators || []).map((c) => [c.id, c.id === "default" ? "Personal" : c.name]);
  const creatorSel = sel(creatorOpts.length ? creatorOpts : [["default", "Personal"]], st.creator_id, { id: "adv-creator", onchange: (e) => (st.creator_id = e.target.value) });
  const gameOpts = [["", "Auto detect"], ...((OPTS.games || []).filter((g) => g).map((g) => [g, g]))];
  const gameSel = sel(gameOpts, st.game, { id: "adv-game", onchange: (e) => (st.game = e.target.value) });
  const toggle = (key, label, id) => {
    const c = h("input", { type: "checkbox", checked: st[key], id, role: "switch" });
    c.addEventListener("change", () => (st[key] = c.checked));
    return h("label", { class: "switch" }, c, h("span", { class: "switch-ui", "aria-hidden": "true" }), label);
  };
  const capName = d.caption_style || "auto", hookName = d.hook_style || "bold-box";
  const zoomSel = sel(ZOOM_OPTS, st.zoom, { id: "adv-zoom", onchange: (e) => (st.zoom = e.target.value) });
  const enhSel = sel(ENHANCE_OPTS, st.enhance_speech, { id: "adv-enhance", onchange: (e) => (st.enhance_speech = e.target.value) });
  const brandTg = toggle("brand_kit", "Brand kit", "adv-brand");
  if (!OPTS.brand_kit) brandTg.querySelector("input").disabled = true;
  const effects = h(
    "div",
    { class: "adv-effects stack", id: "adv-effects" },
    h("h3", { class: "adv-sub", text: "Editing effects" }),
    h("div", { class: "grid3" }, field("Punch-in zoom", zoomSel, "Zooms in on the payoff and hype words; faces stay in view"), field("Speech enhancement", enhSel, "Less background noise and some room echo")),
    h("div", { class: "row" }, toggle("remove_fillers", "Remove filler words (um, uh)", "adv-fillers"), brandTg),
    h("p", { class: "hint", id: "adv-brand-hint" }, OPTS.brand_kit ? "Brand kit: logo, handle, font and cards from Settings. An intro card delays the hook, so keep it short." : "Brand kit: set it up first in ", OPTS.brand_kit ? null : h("a", { href: "#/settings", text: "Settings" }), OPTS.brand_kit ? null : ".")
  );
  const drawer = h(
    "div",
    { class: "adv", id: "adv", hidden: true },
    h(
      "div",
      { class: "grid3" },
      field("Mode", modeSel),
      presetField,
      field("Aspect ratio", ratioSel),
      field("Creator", creatorSel),
      field("Game", gameSel),
      h("div", { class: "field" }, h("span", {}, "Minimum score ", scoreVal), score, h("span", { class: "hint", text: "Higher = fewer, stronger clips" })),
      field("Clips to make", maxIn, "Best first; 0 = every clip above the score"),
      field("AI for titles", llmSel, OPTS.llms.includes("gemini") ? "" : "Set GEMINI_API_KEY before you start the dashboard to use Gemini.")
    ),
    h("div", { class: "row" }, toggle("render", "Render ready-to-post MP4s", "adv-render"), toggle("drafts", "CapCut drafts", "adv-drafts"), toggle("static_analysis", "Static screen analysis", "adv-static")),
    h("p", { class: "hint" }, "Captions ", h("strong", { text: capName }), " · Hook ", h("strong", { text: hookName }), " · ", h("a", { href: "#/styles", text: "Change styles" })),
    effects
  );
  const advBtn = h("button", { class: "btn ghost sm adv-toggle", type: "button", id: "adv-toggle", "aria-expanded": "false", "aria-controls": "adv" }, icon("sliders", 16), "Advanced", icon("chevron-down", 14));
  advBtn.addEventListener("click", () => {
    drawer.hidden = !drawer.hidden;
    advBtn.setAttribute("aria-expanded", String(!drawer.hidden));
    advBtn.classList.toggle("open", !drawer.hidden);
  });

  const drop = h("div", { class: "drop", id: "drop" }, h("span", { class: "drop-ic" }, icon("link", 20)), srcIn, pick, go);
  const form = h("form", { class: "hero-form", id: "home-form", novalidate: true }, drop, fileIn, upRow, h("div", { class: "row between" }, detect, advBtn), err, drawer);
  const hero = h(
    "section",
    { class: "hero" },
    h("div", { class: "hero-text" }, h("span", { class: "eyebrow" }, icon("sparkles", 14), "Clips from your streams, made on your PC"), h("h1", { text: "Turn a stream into ready-to-post clips" }), h("p", { class: "muted", text: "Paste a link or drop a video. StreamClipper finds the best moments, adds captions and makes vertical MP4s." })),
    form
  );
  ["dragenter", "dragover"].forEach((t) =>
    hero.addEventListener(t, (e) => {
      if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
      e.preventDefault();
      hero.classList.add("dragging");
    })
  );
  ["dragleave", "drop"].forEach((t) => hero.addEventListener(t, (e) => (e.target === hero || t === "drop") && hero.classList.remove("dragging")));
  hero.addEventListener("drop", (e) => {
    if (!e.dataTransfer || !e.dataTransfer.files.length) return;
    e.preventDefault();
    doUpload(e.dataTransfer.files[0]);
  });
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    err.textContent = "";
    const src = srcIn.value.trim();
    if (st.uploading) return (err.textContent = "Wait for the upload to finish.");
    if (!st.upload && !src) {
      err.textContent = "Paste a stream link, upload a video or enter a file path.";
      srcIn.focus();
      return;
    }
    const mode = st.mode === "auto" ? (!st.upload && looksLive(src) ? "live" : "vod") : st.mode;
    const body = { mode, preset: st.preset, caption_style: capName, hook_style: hookName, ratio: st.ratio, min_score: st.min_score, max_clips: st.max_clips, llm: st.llm, render: st.render, drafts: st.drafts, zoom: st.zoom, enhance_speech: st.enhance_speech, remove_fillers: st.remove_fillers, brand_kit: st.brand_kit, creator_id: st.creator_id, game: st.game, static_analysis: st.static_analysis, static_bucket: st.static_bucket, static_min: st.static_min, static_threshold: st.static_threshold };
    if (mode === "live") Object.assign(body, { stop_after: 60, max_clips_per_hour: 12 });
    if (st.upload) body.upload = st.upload;
    else body.source = src;
    go.disabled = true;
    try {
      const j = await api("POST", "/api/jobs", body);
      toast(mode === "live" ? "Live clipping started." : "Project added. Clips show up as they are made.", "ok");
      location.hash = mode === "live" ? "#/live" : `#/project/${enc(j.id)}`;
    } catch (e) {
      err.textContent = e.message + (e.hint ? ` (${e.hint})` : "");
    } finally {
      go.disabled = false;
    }
  });
  showDetect();

  const recent = h("div", { class: "projects-grid", id: "recent" }, skeleton("project", 4));
  const recentSec = h("section", { class: "stack" }, h("div", { class: "sec-row" }, h("h2", { text: "Recent projects" }), h("a", { class: "btn ghost sm", href: "#/projects" }, "View all", icon("chevron-right", 14))), recent);
  root.replaceChildren(...[hero, checklistCard(), recentSec].filter(Boolean));
  let last = "";
  const refresh = async () => {
    let jobs;
    try {
      jobs = (await api("GET", "/api/jobs")).jobs.filter((j) => TOP_KINDS.includes(j.kind)).slice(0, 4);
    } catch (e) {
      return;
    }
    const sig = JSON.stringify(jobs);
    if (sig === last) return;
    last = sig;
    if (!jobs.length) recent.replaceChildren(h("div", { class: "card empty slim" }, h("div", { class: "empty-ic" }, icon("folder", 24)), h("p", { class: "muted", text: "No projects yet. Paste a link above to start your first one." })));
    else recent.replaceChildren(...jobs.map((j) => jobCard(j, refresh)));
  };
  await refresh();
  every(3000, refresh);
}

// first-run checklist: the doctor runs in the background; three friendly lines
function checklistCard() {
  let dismissed = false;
  try {
    dismissed = localStorage.getItem("sc_onboarded") === "1";
  } catch (e) {
    dismissed = false;
  }
  if (dismissed) return null;
  const list = h(
    "ul",
    { class: "checklist", id: "checklist", "aria-busy": "true" },
    [0, 1, 2].map(() => h("li", { class: "check-row" }, h("span", { class: "skel dot" }), h("div", { class: "grow" }, h("div", { class: "skel line w40" }), h("div", { class: "skel line w70" }))))
  );
  const dismiss = () => {
    try {
      localStorage.setItem("sc_onboarded", "1");
    } catch (e) {
      /* not remembered */
    }
    card.remove();
  };
  const again = h("button", { class: "btn ghost sm", type: "button", id: "check-again", disabled: true }, icon("refresh-cw", 14), "Check again");
  const card = h(
    "section",
    { class: "card onboard", id: "onboard" },
    h("div", { class: "sec-row" }, h("div", {}, h("h2", { text: "Set-up check" }), h("p", { class: "muted small", text: "Runs in the background. Green means ready; amber items are optional or have a fix." })), iconBtn("x", "Hide the set-up check", { onclick: dismiss })),
    list,
    h("div", { class: "row tight" }, again, h("button", { class: "btn sm", type: "button", text: "Got it", onclick: dismiss }))
  );
  const line = (ok, ic, title, text, link) =>
    h(
      "li",
      { class: "check-row " + (ok ? "ok" : "warn") },
      h("span", { class: "check-ic" }, icon(ok ? "check-circle" : "alert-triangle", 18)),
      h("div", { class: "grow" }, h("div", { class: "check-title" }, icon(ic, 14), h("strong", { text: title })), h("div", { class: "small muted", text })),
      link ? h("a", { class: "btn ghost sm", href: link[1], target: "_blank", rel: "noopener" }, link[0], icon("external-link", 12)) : null
    );
  const fill = (rows) => {
    const find = (pre) => rows.find((r) => String(r[0]).toLowerCase().startsWith(pre));
    const ff = find("ffmpeg"), gpu = find("cuda"), cc = find("capcut desktop");
    const ffOk = ff && ff[1] === "OK", gpuOk = gpu && gpu[1] === "OK", ccOk = cc && cc[1] === "OK";
    list.removeAttribute("aria-busy");
    list.replaceChildren(
      ffOk ? line(true, "film", "ffmpeg is ready", "Used to cut and render the clips.") : line(false, "film", "ffmpeg is missing", "Install ffmpeg, add it to PATH, then restart the dashboard.", ["How to install", `${GUIDE}#quickstart-windows-pc`]),
      gpuOk ? line(true, "cpu", "GPU ready", "Transcription runs on your NVIDIA GPU.") : line(false, "cpu", "No GPU found (optional)", "CPU works, just slower.", ["GPU set-up", `${GUIDE}#with-an-nvidia-gpu`]),
      ccOk ? line(true, "scissors", "CapCut found", "Clips can open in CapCut as drafts.") : line(false, "scissors", "CapCut not found (optional)", "Only needed for CapCut drafts. MP4 clips work without it.", ["Get CapCut", "https://www.capcut.com"])
    );
    again.disabled = false;
  };
  let poll = 0;
  const watch = () => {
    clearInterval(poll);
    poll = every(1500, async () => {
      try {
        const r = await api("GET", "/api/doctor");
        if (!r.running && r.rows) {
          clearInterval(poll);
          fill(r.rows);
        }
      } catch (e) {
        clearInterval(poll);
      }
    });
  };
  const start = async (force) => {
    again.disabled = true;
    try {
      const r = force ? null : await api("GET", "/api/doctor");
      if (r && !r.running && r.rows) return fill(r.rows);
      if (!r || !r.running) await api("POST", "/api/doctor");
      watch();
    } catch (e) {
      list.replaceChildren(h("li", { class: "check-row warn" }, h("span", { class: "check-ic" }, icon("alert-triangle", 18)), h("div", { class: "small", text: e.message })));
    }
  };
  again.addEventListener("click", () => {
    list.setAttribute("aria-busy", "true");
    start(true);
  });
  start(false);
  return card;
}

// ---- projects -----------------------------------------------------------------------------------------------

const GROUPS = {
  all: () => true,
  running: (j) => ACTIVE.includes(j.status),
  done: (j) => ["done", "stopped"].includes(j.status),
  failed: (j) => ["failed", "cancelled", "interrupted"].includes(j.status),
};

async function pageProjects(root) {
  let filter = "all";
  let jobs = null;
  const chips = h("div", { class: "chips", role: "tablist", "aria-label": "Filter projects" });
  const grid = h("div", { class: "projects-grid", id: "jobs-list" }, skeleton("project", 6));
  root.replaceChildren(
    h(
      "div",
      { class: "page-head" },
      h("div", {}, h("h1", { text: "Projects" }), h("p", { class: "muted", text: "Every stream or video you clipped. One project runs at a time; the others wait in the queue." })),
      h("a", { class: "btn primary", href: "#/home" }, icon("plus", 16), "New project")
    ),
    chips,
    grid
  );
  const draw = () => {
    if (!jobs) return;
    const counts = Object.fromEntries(Object.entries(GROUPS).map(([k, f]) => [k, jobs.filter(f).length]));
    chips.replaceChildren(
      ...[
        ["all", "All"],
        ["running", "Running"],
        ["done", "Done"],
        ["failed", "Failed"],
      ].map(([k, l]) => h("button", { class: "chip-btn" + (filter === k ? " on" : ""), type: "button", role: "tab", "aria-selected": String(filter === k), dataset: { k }, onclick: () => ((filter = k), draw()) }, l, h("span", { class: "count", text: String(counts[k]) })))
    );
    if (!jobs.length) {
      grid.replaceChildren(emptyState("folder", "No projects yet", "Paste a stream link or drop a video on Home to make your first clips.", h("a", { class: "btn primary", href: "#/home" }, icon("plus", 16), "New project")));
      return;
    }
    const shown = jobs.filter(GROUPS[filter]).filter((j) => matches(j.label));
    if (!shown.length) grid.replaceChildren(emptyState("search", "Nothing matches", SEARCH ? `No project matches "${SEARCH}".` : "No project in this group.", h("button", { class: "btn", type: "button", text: "Show all", onclick: () => ((filter = "all"), clearSearch(), draw()) })));
    else grid.replaceChildren(...shown.map((j) => jobCard(j, refresh)));
  };
  let last = "";
  const refresh = async () => {
    let all;
    try {
      all = (await api("GET", "/api/jobs")).jobs;
    } catch (e) {
      if (!jobs) grid.replaceChildren(errorBox(e));
      return;
    }
    const top = all.filter((j) => TOP_KINDS.includes(j.kind));
    const sig = JSON.stringify(top);
    if (sig === last) return;
    last = sig;
    jobs = top;
    draw();
  };
  onSearch(draw);
  await refresh();
  every(2000, refresh);
}

function clearSearch() {
  const s = document.getElementById("search");
  s.value = "";
  SEARCH = "";
}

// ---- project page: clips ------------------------------------------------------------------------------------

function gateTable(gate) {
  if (!gate) return h("p", { class: "hint", text: "Audit not saved for this clip." });
  return h(
    "table",
    {},
    h("thead", {}, h("tr", {}, h("th", { text: "Check" }), h("th", { text: "Result" }), h("th", { text: "Detail" }))),
    h(
      "tbody",
      {},
      gate.rows.map((r) => h("tr", {}, h("td", { text: r[0] }), h("td", {}, pill(r[1], r[1])), h("td", { class: "detail", text: r[2] })))
    )
  );
}

function shareModal(body, what) {
  const days = sel(OPTS.share_days.map((d) => [String(d), `${d} day${d > 1 ? "s" : ""}`]), "7", { id: "share-days" });
  const out = h("div");
  const make = h("button", { class: "btn primary", type: "button", id: "share-create" }, icon("link", 16), "Create link");
  make.addEventListener("click", async () => {
    make.disabled = true;
    try {
      const r = await api("POST", "/api/shares", { ...body, days: Number(days.value) });
      out.replaceChildren(h("div", { class: "share-box stack" }, h("div", { class: "mono share-url", text: r.url }), h("div", { class: "row tight" }, copyBtn(r.url, "Link"), h("span", { class: "hint", text: `${r.label}. Valid for ${r.days} days. View and download only; revoke it in Settings.` }))));
    } catch (e) {
      fail(e);
    } finally {
      make.disabled = false;
    }
  });
  openModal(`Share ${what}`, h("div", { class: "stack" }, h("p", { class: "muted", text: "Works for people on the same Wi-Fi/network. The link is shown once." }), h("div", { class: "row tight end" }, field("Link expires after", days), make), out), []);
}

// ---- publishing (this PC only; the server refuses it on the network) --------------------------------------

const PUB_NAMES = { youtube: "YouTube", instagram: "Instagram", pack: "TikTok pack" };

function fmtWhen(iso) {
  const d = new Date(iso);
  return isNaN(d) ? String(iso || "") : d.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function localDate(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function pubChips(pub) {
  return Object.entries(pub || {}).map(([p, e]) => {
    const name = PUB_NAMES[p] || p;
    let text = `${name} failed: ${e.message || "error"}`, cls = "chip pub-chip warn-chip", ic = "alert-triangle";
    if (e.state === "uploading") [text, cls, ic] = [`${name}: uploading ...`, "chip pub-chip", "upload"];
    else if (e.state === "scheduled") [text, cls, ic] = [`${name}: scheduled ${fmtWhen(e.publish_at)}`, "chip pub-chip ok-chip", "clock"];
    else if (e.state === "uploaded") [text, cls, ic] = [`${name}: uploaded (${e.privacy || "private"})`, "chip pub-chip ok-chip", "send"];
    else if (e.state === "published") [text, cls, ic] = [`${name}: published`, "chip pub-chip ok-chip", "send"];
    else if (e.state === "packed") [text, cls, ic] = [`${name} ready`, "chip pub-chip ok-chip", "folder"];
    const chip = h("span", { class: cls, title: [text, e.hint, e.folder].filter(Boolean).join("\n") }, icon(ic, 12), h("span", { class: "ellipsis", text }));
    return e.url ? h("a", { href: e.url, target: "_blank", rel: "noopener", class: "pub-link", "aria-label": `${text} (opens YouTube)` }, chip) : chip;
  });
}

async function publishModal(job, c, refresh) {
  let s;
  try {
    s = await api("GET", "/api/publish");
  } catch (e) {
    return fail(e);
  }
  const tags = (c.hashtags || []).join(" ");
  const first = s.youtube.connected ? "youtube" : s.instagram.connected ? "instagram" : "pack";
  const platSel = sel([["youtube", "YouTube Shorts"], ["instagram", "Instagram Reels"], ["pack", "TikTok publish pack (you post it)"]], first, { id: "pub-platform" });
  const titleIn = h("input", { type: "text", maxLength: 100, value: c.title || c.hook_headline || "", id: "pub-title" });
  const descIn = h("textarea", { id: "pub-desc", maxLength: 4500 });
  descIn.value = c.description || "";
  const tagsIn = h("input", { type: "text", value: tags, id: "pub-tags", placeholder: "#gaming #clips" });
  const privSel = sel([["private", "Private (only you)"], ["unlisted", "Unlisted (anyone with the link)"]], "private", { id: "pub-privacy" });
  const atIn = h("input", { type: "datetime-local", id: "pub-at" });
  const capIn = h("textarea", { id: "pub-caption", maxLength: 2200, rows: 6 });
  capIn.value = [c.title || c.hook_headline, c.description, tags].filter(Boolean).join("\n\n");
  const note = h("div", { class: "stack" });
  const ytRow = h("div", { class: "grid2" }, field("Privacy", privSel), field("Publish at (optional)", atIn, "Your local time. A scheduled video stays private until then."));
  const textBox = h("div", { class: "stack" }, field("Title", titleIn, "At most 100 characters. #Shorts is added to the description."), field("Description", descIn), field("Hashtags", tagsIn), ytRow);
  const capBox = h("div", { class: "stack" }, field("Caption", capIn, "At most 2,200 characters and 30 hashtags."));
  const go = h("button", { class: "btn primary", type: "button", id: "pub-go" });
  const draw = () => {
    const p = platSel.value;
    const conn = p === "pack" ? null : s[p];
    textBox.hidden = p === "instagram";
    ytRow.hidden = p !== "youtube";
    capBox.hidden = p !== "instagram";
    go.replaceChildren(icon(p === "pack" ? "folder" : "send", 16), p === "pack" ? "Make pack" : "Publish");
    go.disabled = !!(conn && !conn.connected);
    note.replaceChildren(
      ...[
        conn && !conn.connected ? h("p", { class: "hint warn-hint" }, icon("info", 14), h("span", {}, `${PUB_NAMES[p]} is not connected yet. `, h("a", { href: "#/settings", text: "Connect it in Settings" }), ".")) : null,
        conn && conn.connected ? h("p", { class: "hint", text: `Posts to ${conn.account || "your account"}.` }) : null,
        p === "pack" ? h("p", { class: "hint", text: "TikTok's posting API needs an app review, so you get a folder with the MP4, caption, hashtags and a best-time note to post from the TikTok app." }) : null,
      ].filter(Boolean)
    );
  };
  platSel.addEventListener("change", draw);
  const m = openModal(`Publish ${c.title || c.key}`, h("div", { class: "stack pub-form" }, field("Where", platSel), note, textBox, capBox), [go]);
  draw();
  go.addEventListener("click", async () => {
    const p = platSel.value;
    const body = p === "instagram" ? { platform: p, caption: capIn.value } : { platform: p, title: titleIn.value, description: descIn.value, tags: tagsIn.value };
    if (p === "youtube") Object.assign(body, { privacy: privSel.value, publish_at: atIn.value });
    go.disabled = true;
    try {
      await api("POST", `/api/jobs/${enc(job.id)}/clips/${enc(c.key)}/publish`, body);
      toast(p === "pack" ? "Making the publish pack ..." : `Uploading to ${PUB_NAMES[p]} ... The clip card shows when it is done.`, "ok");
      m.close(true);
      refresh(true);
    } catch (e) {
      fail(e);
      go.disabled = false;
    }
  });
}

async function scheduleModal(job, refresh) {
  let s;
  try {
    s = await api("GET", "/api/publish");
  } catch (e) {
    return fail(e);
  }
  const url = `/api/jobs/${enc(job.id)}/publish/schedule`;
  const platSel = sel([["youtube", "YouTube Shorts (YouTube publishes each at its time)"], ["pack", "TikTok publish packs (you post them)"]], s.youtube.connected ? "youtube" : "pack", { id: "sch-platform" });
  const startIn = h("input", { type: "date", value: localDate(new Date(Date.now() + 864e5)), id: "sch-start" });
  const perSel = sel(["1", "2", "3"], "1", { id: "sch-per" });
  const timesIn = h("input", { type: "text", id: "sch-times", placeholder: "Suggested times" });
  const out = h("div", { class: "stack sch-plan", id: "sch-plan" });
  const prev = h("button", { class: "btn", type: "button", id: "sch-preview" }, icon("clock", 16), "Show plan");
  const go = h("button", { class: "btn primary", type: "button", id: "sch-go", disabled: true }, icon("send", 16), "Schedule");
  const body = () => ({ platform: platSel.value, start: startIn.value, per_day: Number(perSel.value), times: timesIn.value });
  const reset = () => ((go.disabled = true), out.replaceChildren());
  [platSel, startIn, perSel, timesIn].forEach((x) => x.addEventListener("input", reset));
  prev.addEventListener("click", async () => {
    prev.disabled = true;
    try {
      const r = await api("POST", url, body());
      const ytOff = r.platform === "youtube" && !s.youtube.connected;
      out.replaceChildren(
        ...[
          r.slots.length
            ? h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "When" }), h("th", { text: "Clip" }))), h("tbody", {}, r.slots.map((x) => h("tr", {}, h("td", { class: "when", text: x.label }), h("td", { class: "detail", text: x.title }))))))
            : h("p", { class: "muted", text: "Nothing to plan: every rendered clip is already published there (rejected clips are skipped)." }),
          r.left ? h("p", { class: "hint", text: `${r.left} clip(s) do not fit into this week. Add more clips per day or more times.` }) : null,
          ytOff ? h("p", { class: "hint warn-hint" }, icon("info", 14), h("span", {}, "YouTube is not connected yet. ", h("a", { href: "#/settings", text: "Connect it in Settings" }), ".")) : null,
        ].filter(Boolean)
      );
      go.disabled = !r.slots.length || ytOff;
      go.replaceChildren(icon("send", 16), r.slots.length ? `Schedule ${r.slots.length} clip${r.slots.length > 1 ? "s" : ""}` : "Schedule");
    } catch (e) {
      fail(e);
    } finally {
      prev.disabled = false;
    }
  });
  const m = openModal(
    "Schedule week",
    h(
      "div",
      { class: "stack" },
      h("p", { class: "muted", text: "Spreads the rendered clips over the next 7 days, best first. YouTube uploads happen now and stay private until their time." }),
      field("Where", platSel),
      h("div", { class: "grid3" }, field("First day", startIn), field("Clips per day", perSel), field("Times (optional)", timesIn, "e.g. 18:00, 20:30")),
      h("p", { class: "hint", text: "Suggested times: weekdays 18:00-21:00, weekends 11:00-13:00 (a common heuristic, not measured on your audience)." }),
      h("div", {}, prev),
      out
    ),
    [go]
  );
  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      const r = await api("POST", url, { ...body(), confirm: true });
      toast(`Scheduling ${r.slots.length} clip${r.slots.length > 1 ? "s" : ""} ... The clip cards show each result.`, "ok");
      m.close(true);
      refresh();
    } catch (e) {
      fail(e);
      go.disabled = false;
    }
  });
}

// Settings > Publishing: paths of the user's own credential files; the token stays in the user config folder
async function publishCard(box) {
  if (LAN) {
    box.replaceChildren(h("p", { class: "muted", text: "Publishing works only on this PC." }));
    return;
  }
  let s;
  try {
    s = await api("GET", "/api/publish");
  } catch (e) {
    box.replaceChildren(errorBox(e));
    return;
  }
  let queue = { upcoming: [], published: [], exported: [], failed: [], cancelled: [] };
  try {
    queue = await api("GET", "/api/publishing/queue");
  } catch (e) {
    // Publishing queue state is unavailable only while the old dashboard file is migrating.
  }
  const y = s.youtube, g = s.instagram;
  const chip = (ok, text) => h("span", { class: "chip " + (ok ? "ok-chip" : "warn-chip") }, icon(ok ? "check" : "info", 12), text);
  const act = async (path, body, msg) => {
    try {
      const r = await api("POST", path, body || {});
      if (msg) toast(typeof msg === "function" ? msg(r) : msg, "ok");
    } catch (e) {
      fail(e);
    }
    publishCard(box);
  };
  const ytIn = h("input", { type: "text", id: "pub-yt-file", value: y.client_file, placeholder: "D:\\keys\\client_secret_1234.json" });
  const ytGo = h("button", { class: "btn primary", type: "button", id: "pub-yt-connect", disabled: y.connecting }, icon("youtube", 16), y.connected ? "Reconnect YouTube" : "Connect YouTube");
  const ytOff = h("button", { class: "btn ghost", type: "button", id: "pub-yt-off", text: "Disconnect" });
  ytGo.addEventListener("click", async () => {
    ytGo.disabled = true;
    try {
      await api("POST", "/api/publish/youtube/connect", { client_file: ytIn.value });
      toast("Sign in to Google in the browser window that opened.", "ok");
    } catch (e) {
      fail(e);
    }
    publishCard(box);
  });
  ytOff.addEventListener("click", async () => {
    if (await confirmModal("Disconnect YouTube?", "The saved sign-in is deleted from this PC. Uploaded videos stay on YouTube.", "Disconnect")) act("/api/publish/youtube/disconnect", {}, "YouTube disconnected.");
  });
  const igIn = h("input", { type: "text", id: "pub-ig-file", value: g.credentials_file, placeholder: "D:\\keys\\instagram.json" });
  const igGo = h("button", { class: "btn primary", type: "button", id: "pub-ig-connect" }, icon("send", 16), g.connected ? "Check again" : "Connect Instagram");
  const igOff = h("button", { class: "btn ghost", type: "button", id: "pub-ig-off", text: "Disconnect" });
  igGo.addEventListener("click", () => ((igGo.disabled = true), act("/api/publish/instagram/connect", { credentials_file: igIn.value }, (r) => `Instagram connected: ${r.instagram.account}`)));
  igOff.addEventListener("click", () => act("/api/publish/instagram/disconnect", {}, "Instagram disconnected. Your credentials file was not touched."));
  const queueRows = [
    ...queue.upcoming.map((p) => h("div", { class: "row tight" }, h("span", { class: "chip ok-chip", text: `${p.platform}: scheduled` }), h("span", { class: "muted", text: new Date(p.scheduled_at * 1000).toLocaleString() }))),
    ...queue.failed.map((p) => h("div", { class: "row tight" }, h("span", { class: "chip warn-chip", text: `${p.platform}: failed` }), h("span", { class: "muted", text: p.error || "Temporary failure" }), h("button", { class: "btn ghost sm", type: "button", onclick: () => act(`/api/publishing/queue/${encodeURIComponent(p.id)}/retry`, {}, "Queue item retried.") }, "Retry"))),
  ];
  const queueSection = h(
    "div",
    { class: "stack", id: "publishing-queue" },
    h("h3", { class: "adv-sub", text: "Publishing queue" }),
    queueRows.length ? queueRows : h("p", { class: "muted", text: "No upcoming or failed posts." }),
    h("p", { class: "hint", text: `${queue.published.length} published · ${queue.exported.length} export packs · ${queue.cancelled.length} cancelled` })
  );
  box.replaceChildren(
    h(
      "div",
      { class: "pub-acc", id: "pub-youtube" },
      h("div", { class: "pub-acc-head" }, icon("youtube", 18), h("strong", { text: "YouTube Shorts" }), chip(y.connected, y.connected ? `Connected: ${y.account || "your account"}` : y.connecting ? "Waiting for the sign-in ..." : "Not connected")),
      field("OAuth client file (client_secret.json)", ytIn, "Your own Google Cloud OAuth client of type Desktop app. The sign-in opens in this PC's browser."),
      y.error ? h("p", { class: "err", text: y.error }) : null,
      h("div", { class: "row tight" }, ytGo, y.connected ? ytOff : null)
    ),
    h(
      "div",
      { class: "pub-acc", id: "pub-instagram" },
      h("div", { class: "pub-acc-head" }, icon("send", 18), h("strong", { text: "Instagram Reels" }), chip(g.connected, g.connected ? `Connected: @${g.account}` : "Not connected")),
      field("Credentials file (JSON)", igIn, 'A file with "ig_user_id", "access_token" and "graph_version" of a professional account with instagram_content_publish.'),
      h("div", { class: "row tight" }, igGo, g.connected ? igOff : null)
    ),
    h("p", { class: "hint" }, `TikTok: use "TikTok publish pack" in a clip's Publish dialog. Sign-ins are kept in ${s.config_dir} and never shown here. `, h("a", { href: GUIDE + "#publishing", target: "_blank", rel: "noopener", text: "How to set it up" })),
    queueSection
  );
  if (y.connecting) setTimeout(() => document.contains(box) && publishCard(box), 2000);
}

function scoreClass(s) {
  return s >= 80 ? "s-hi" : s >= 60 ? "s-mid" : s >= 40 ? "s-warn" : "s-low";
}

function clipCard(job, c, lan, refresh) {
  const base = `/api/jobs/${enc(job.id)}/clips/${enc(c.key)}`;
  const post = async (action, body, msg) => {
    try {
      const r = await api("POST", `${base}/${action}`, body || {});
      if (msg) toast(typeof msg === "function" ? msg(r) : msg, "ok");
      refresh(true);
      return r;
    } catch (e) {
      fail(e);
      return null;
    }
  };
  const score = clamp(Math.round(Number(c.score) || 0), 0, 100);
  const title = c.title || c.hook_headline || c.key;
  const still = c.thumb_url || c.preview_url;
  const frame = h(
    "div",
    { class: "frame" + (c.mp4_url ? " playable" : "") },
    still ? h("img", { src: still, alt: `Frame of clip ${c.rank}`, loading: "lazy" }) : h("div", { class: "thumb-ph" }, icon("film", 30), h("span", { class: "small", text: c.mp4_url ? "No preview" : "No MP4 (render was off)" }))
  );
  const breakdown = Object.entries(c.breakdown || {});
  const badge = h(
    "button",
    { class: "score-badge " + scoreClass(score), type: "button", "aria-label": `Score ${score} of 100. Show the breakdown.` },
    h("span", { class: "score-num", text: String(score) }),
    h(
      "div",
      { class: "score-pop", role: "tooltip" },
      h("strong", { text: `Score ${score} / 100` }),
      breakdown.length ? h("table", {}, h("tbody", {}, breakdown.map(([k, v]) => h("tr", {}, h("td", { text: k.replace(/_/g, " ") }), h("td", { class: "v", text: v === null ? "-" : String(v) }))))) : h("p", { class: "hint", text: "No breakdown saved." }),
      c.reason ? h("p", { class: "hint", text: c.reason }) : null
    )
  );
  badge.addEventListener("click", (e) => (e.stopPropagation(), badge.classList.toggle("open")));
  badge.addEventListener("blur", () => badge.classList.remove("open"));
  frame.append(
    h("div", { class: "frame-top" }, badge, c.mark ? h("span", { class: "chip mark-" + c.mark }, icon(c.mark === "posted" ? "send" : "ban", 12), c.mark === "posted" ? "Posted" : "Rejected") : null),
    h("span", { class: "dur", text: mmss(c.duration || (c.end || 0) - (c.start || 0)) }),
    c.mp4_url ? h("span", { class: "play-ic", "aria-hidden": "true" }, icon("play", 22)) : null
  );
  if (c.mp4_url) {
    // hover-to-play: a muted preview video exists only while the pointer is over the card
    let vid = null;
    frame.addEventListener("mouseenter", () => {
      if (vid) return;
      vid = h("video", { class: "hover-vid", src: c.mp4_url, muted: true, loop: true, playsInline: true, preload: "none", poster: still || null });
      vid.muted = true;
      frame.append(vid);
      vid.play().catch(() => {});
    });
    frame.addEventListener("mouseleave", () => {
      if (!vid) return;
      vid.pause();
      vid.removeAttribute("src");
      vid.load();
      vid.remove();
      vid = null;
    });
    frame.addEventListener("click", (e) => {
      if (e.target.closest(".score-badge")) return;
      const v = h("video", { class: "player", src: c.mp4_url, controls: true, autoplay: true, playsInline: true, poster: still || null });
      const m = openModal(title, v, [h("a", { class: "btn sm", href: c.mp4_url + "?download=1", download: c.key + ".mp4" }, icon("download", 14), "Download")], { wide: true });
      m.onclose = () => (v.pause(), v.removeAttribute("src"), v.load());
    });
  }
  const caption = [c.title || c.hook_headline, c.description, (c.hashtags || []).join(" ")].filter(Boolean).join("\n\n");
  const shareOk = lan && c.mp4_url;
  const shareTip = !lan ? "Share links need network mode: streamclipper dashboard --lan" : "Render the clip first";
  const capSel = () => sel(OPTS.caption_styles.filter((s) => s !== "auto"), c.caption_style && c.caption_style !== "auto" ? (c.caption_style === "comic-burst" ? "bold-pop" : "comic-burst") : "comic-burst", { id: "rr-cap" });
  const rerender = () => {
    const cs = capSel(), hs = sel([["", "Same hook style"], ...OPTS.hook_styles], "", { id: "rr-hook" });
    const go = h("button", { class: "btn primary", type: "button", id: "rr-go" }, icon("refresh-cw", 16), "Re-render");
    const m = openModal("Re-render with another style", h("div", { class: "stack" }, h("p", { class: "muted", text: "Makes a new version next to this clip. The original stays." }), field("Caption style", cs), field("Hook style", hs)), [go]);
    go.addEventListener("click", async () => {
      m.close(true);
      await post("rerender", { caption_style: cs.value, hook_style: hs.value }, (r) => `Queued: ${r.variant.name}`);
    });
  };
  const more = () => [
    !lan ? { label: "Publish", icon: "upload", disabled: !c.mp4_url, title: c.mp4_url ? null : "Render the clip first", onClick: () => publishModal(job, c, refresh) } : null,
    !lan ? "-" : null,
    { label: c.mark === "posted" ? "Unmark posted" : "Mark as posted", icon: "send", onClick: () => post("mark", { mark: c.mark === "posted" ? null : "posted" }, c.mark === "posted" ? "Mark removed." : "Marked as posted.") },
    { label: c.mark === "rejected" ? "Unmark rejected" : "Mark as rejected", icon: "ban", onClick: () => post("mark", { mark: c.mark === "rejected" ? null : "rejected" }, c.mark === "rejected" ? "Mark removed." : "Marked as rejected.") },
    "-",
    { label: "Re-render with another style", icon: "refresh-cw", onClick: rerender },
    { label: c.gate ? `Export audit (${c.gate.pass ? "PASS" : "FAIL"})` : "Export audit", icon: "shield-check", onClick: () => openModal(`Export audit: ${c.key}`, gateTable(c.gate), [], { wide: true }) },
    { label: c.imported ? "Open in CapCut again" : "Open in CapCut", icon: "external-link", onClick: () => post("capcut", {}, (r) => r.message) },
    "-",
    {
      label: "Delete",
      icon: "trash-2",
      danger: true,
      onClick: async () => {
        if (await confirmModal("Delete this clip?", `${c.key} and its files move to the trash folder of the workspace. Nothing is deleted for good.`, "Move to trash")) post("delete", { confirm: true }, (r) => `Moved ${r.moved.length} file(s) to ${r.trash}.`);
      },
    },
  ];
  const variants = (c.variants || []).map((v) =>
    h(
      "div",
      { class: "variant", dataset: { name: v.name } },
      h("strong", { text: v.name }),
      pill(v.status),
      v.edit ? h("span", { class: "muted", text: `edit v${v.edit}` }) : h("span", { class: "muted", text: [v.caption_style, v.hook_style].filter(Boolean).join(" + ") }),
      v.gate ? pill(v.gate.pass ? "PASS" : "FAIL", v.gate.pass ? "audit pass" : "audit fail") : null,
      h("span", { class: "spacer" }),
      v.mp4_url ? h("a", { class: "icon-btn xs", href: v.mp4_url, target: "_blank", rel: "noopener", title: "Watch", "aria-label": `Watch ${v.name}` }, icon("play", 14)) : null,
      v.mp4_url ? h("a", { class: "icon-btn xs", href: v.mp4_url + "?download=1", download: v.name + ".mp4", title: "Download", "aria-label": `Download ${v.name}` }, icon("download", 14)) : null,
      v.mp4_url
        ? h("button", { class: "icon-btn xs" + (v.mark === "posted" ? " on" : ""), type: "button", title: v.mark === "posted" ? "Posted (click to unmark)" : "Mark as posted", "aria-label": `Posted ${v.name}`, "aria-pressed": String(v.mark === "posted"), onclick: () => api("POST", `/api/jobs/${enc(job.id)}/clips/${enc(v.name)}/mark`, { mark: v.mark === "posted" ? null : "posted" }).then(() => refresh(true), fail) }, icon("send", 14))
        : null
    )
  );
  return h(
    "article",
    { class: "card clip", dataset: { key: c.key, category: c.category || "", mark: c.mark || "" } },
    frame,
    h(
      "div",
      { class: "body" },
      h("div", { class: "meta-row" }, c.category ? h("span", { class: "chip cat", text: c.category }) : null, h("span", { class: "small muted", text: `#${c.rank} · ${mmss(c.start)}-${mmss(c.end)}` })),
      h("div", { class: "clip-title", title, text: title }),
      c.latency_s !== null && c.latency_s !== undefined ? h("div", { class: "small muted", text: `Ready ${Math.round(c.latency_s)} s after the moment` }) : null,
      c.imported ? h("div", { class: "hint", text: `In CapCut as '${c.imported}'.` }) : null,
      c.publish && Object.keys(c.publish).length ? h("div", { class: "pub-row" }, pubChips(c.publish)) : null,
      h(
        "div",
        { class: "clip-actions" },
        c.mp4_url ? h("a", { class: "btn sm primary", href: `#/edit/${enc(job.id)}/${enc(c.key)}`, "aria-label": `Edit ${c.key}` }, icon("scissors", 14), "Edit") : h("button", { class: "btn sm primary", type: "button", disabled: true, title: "Render the clip first" }, icon("scissors", 14), "Edit"),
        c.mp4_url ? h("a", { class: "icon-btn sm", href: c.mp4_url + "?download=1", download: c.key + ".mp4", title: "Download MP4", "aria-label": "Download MP4" }, icon("download", 16)) : iconBtn("download", "Download MP4 (render the clip first)", { disabled: true }),
        iconBtn("copy", "Copy caption", { disabled: !caption, onclick: () => copyWithToast(caption, "Caption") }),
        iconBtn("share-2", shareOk ? "Share" : shareTip, { disabled: !shareOk, onclick: () => shareModal({ kind: "clip", job: job.id, clip: c.key }, "this clip") }),
        iconBtn("external-link", c.draft ? (c.imported ? "Open in CapCut again" : "Open in CapCut") : "Open in CapCut (this job ran without CapCut drafts)", { onclick: () => post("capcut", {}, (r) => r.message) }),
        menuBtn(more, "More clip actions")
      ),
      variants.length ? h("div", { class: "variants" }, h("span", { class: "label", text: "Versions" }), variants) : null
    )
  );
}

async function pageProject(root, id) {
  const head = h("div", { class: "page-head" }, h("div", { class: "grow" }, h("div", { class: "skel line w40" }), h("div", { class: "skel line w70" })));
  const info = h("div", { class: "stack" });
  const toolbar = h("div", { class: "toolbar", hidden: true });
  const grid = h("div", { class: "clips", id: "clips" }, skeleton("clip", 4));
  root.replaceChildren(h("a", { class: "back-link", href: "#/projects" }, icon("chevron-left", 14), "Projects"), head, info, toolbar, grid);
  const openLogs = new Set();
  const view = { sort: "score", cat: "", mark: "all" };
  let data = null, job = null, lastHead = "", lastClips = "";

  const drawClips = () => {
    if (!data) return;
    const clips = data.clips;
    if (!clips.length) {
      toolbar.hidden = true;
      const running = ACTIVE.includes(job.status);
      grid.replaceChildren(running ? emptyState("sparkles", "Working on it", "Clips show up here when the job has found and rendered them.") : emptyState("film", "No clips", "No moment passed the quality bar. Try a lower minimum score in Advanced on Home."));
      return;
    }
    const cats = [...new Set(clips.map((c) => c.category).filter(Boolean))].sort();
    const sortSel = sel([["score", "Best score"], ["time", "Time in stream"]], view.sort, { id: "clip-sort", "aria-label": "Sort clips", onchange: (e) => ((view.sort = e.target.value), drawClips()) });
    const catSel = sel([["", "All categories"], ...cats.map((x) => [x, x])], view.cat, { id: "clip-cat", "aria-label": "Category", onchange: (e) => ((view.cat = e.target.value), drawClips()) });
    const markChips = h(
      "div",
      { class: "chips", role: "tablist", "aria-label": "Posted status" },
      [
        ["all", "All"],
        ["none", "Not posted"],
        ["posted", "Posted"],
        ["rejected", "Rejected"],
      ].map(([k, l]) => h("button", { class: "chip-btn" + (view.mark === k ? " on" : ""), type: "button", role: "tab", "aria-selected": String(view.mark === k), dataset: { k }, onclick: () => ((view.mark = k), drawClips()) }, l))
    );
    let shown = clips.filter((c) => (!view.cat || c.category === view.cat) && (view.mark === "all" || (view.mark === "none" ? !c.mark : c.mark === view.mark)) && matches([c.title, c.hook_headline, c.category, c.key, c.description].join(" ")));
    shown = shown.slice().sort(view.sort === "time" ? (a, b) => (a.start || 0) - (b.start || 0) : (a, b) => (Number(b.score) || 0) - (Number(a.score) || 0));
    toolbar.hidden = false;
    toolbar.replaceChildren(h("div", { class: "tb-left" }, h("span", { class: "tb-ic" }, icon("arrow-up-down", 14)), sortSel, catSel), markChips, h("span", { class: "small muted", id: "clip-count", text: `${shown.length} of ${clips.length} clip${clips.length === 1 ? "" : "s"}` }));
    if (!shown.length) grid.replaceChildren(emptyState("search", "No clip matches", "Change the filters or the search.", h("button", { class: "btn", type: "button", text: "Clear filters", onclick: () => (Object.assign(view, { cat: "", mark: "all" }), clearSearch(), drawClips()) })));
    else grid.replaceChildren(...shown.map((c) => clipCard(job, c, data.lan, refresh)));
  };

  const refresh = async (force) => {
    let d, detail, all;
    try {
      [d, detail, all] = await Promise.all([api("GET", `/api/jobs/${enc(id)}/clips`), api("GET", `/api/jobs/${enc(id)}`), api("GET", "/api/jobs").catch(() => ({ jobs: [] }))]);
    } catch (e) {
      if (!lastHead) root.replaceChildren(h("a", { class: "back-link", href: "#/projects" }, icon("chevron-left", 14), "Projects"), errorBox(e));
      return;
    }
    job = d.job;
    job.log_tail = detail.log_tail;
    const tasks = all.jobs.filter((j) => j.parent === id && ACTIVE.includes(j.status));
    const headSig = JSON.stringify([job, detail.log_tail, d.clips.length, tasks]);
    const clipsSig = JSON.stringify(d.clips);
    const clipsChanged = clipsSig !== lastClips;
    if (headSig === lastHead && !clipsChanged && !force) return;
    lastHead = headSig;
    lastClips = clipsSig;
    data = d;
    const kind = job.source_kind || "file";
    const canShare = d.lan && d.clips.some((c) => c.mp4_url);
    head.replaceChildren(
      h(
        "div",
        { class: "grow", style: { minWidth: "0" } },
        h("div", { class: "title-row" }, h("span", { class: "src-ic lg src-" + kind, title: SRC_NAMES[kind] }, icon(SRC_ICONS[kind] || "file-video", 18)), h("h1", { class: "ellipsis", title: job.label, text: job.label })),
        h("p", { class: "muted", text: `${SRC_NAMES[kind] || ""} · ${KINDS[job.kind] || job.kind} · ${when(job.created)} · ${d.clips.length} clip${d.clips.length === 1 ? "" : "s"}` })
      ),
      h(
        "div",
        { class: "row tight" },
        pill(job.status),
        ...jobActions(job, () => refresh(true)),
        !d.lan && d.clips.some((c) => c.mp4_url) ? h("button", { class: "btn sm", type: "button", id: "schedule-week", onclick: () => scheduleModal(job, () => refresh(true)) }, icon("clock", 14), "Schedule week") : null,
        canShare ? h("button", { class: "btn sm", type: "button", onclick: () => shareModal({ kind: "job", job: job.id }, "all clips") }, icon("share-2", 14), "Share all") : null,
        d.clips.length ? h("a", { class: "btn sm", href: `/files/${enc(job.id)}/out/clips.csv?download=1` }, icon("download", 14), "clips.csv") : null
      )
    );
    const showProg = ACTIVE.includes(job.status) || ["failed", "interrupted", "cancelled"].includes(job.status);
    info.replaceChildren(
      ...[
        showProg ? h("div", { class: "card stack prog-card" }, progressBlock(job), logBlock(job, openLogs)) : h("div", { class: "log-line" }, logBlock(job, openLogs)),
        tasks.length ? h("div", { class: "card tasks" }, h("span", { class: "label", text: "Background tasks" }), tasks.map((t) => h("div", { class: "task" }, pill(t.status), h("span", { class: "grow ellipsis", text: `${KINDS[t.kind] || t.kind}: ${t.label}` }), h("span", { class: "small muted", text: stageText(t) }), ...jobActions(t, () => refresh(true))))) : null,
      ].filter(Boolean)
    );
    if (clipsChanged || force) drawClips();
  };
  onSearch(drawClips);
  await refresh(true);
  every(3000, () => refresh(false));
}

// ---- live ---------------------------------------------------------------------------------------------------

function liveForm() {
  const d = OPTS.defaults || {};
  const st = { tab: "url", caption_style: d.caption_style || "auto", hook_style: d.hook_style || "bold-box", ratio: "9:16", min_score: 50, stop_after: 60, max_clips_per_hour: 12, llm: "none", render: true, drafts: false, upload: "" };
  const urlIn = h("input", { type: "url", id: "src-url", placeholder: "https://www.twitch.tv/channel", autocomplete: "off" });
  const pathIn = h("input", { type: "text", id: "src-path", placeholder: "D:\\Videos\\my_stream.mp4", autocomplete: "off" });
  const fileIn = h("input", { type: "file", id: "src-file", accept: "video/*,.mkv,.ts,.flv,.m4v" });
  const upBar = h("div", { class: "progress", hidden: true }, h("span", { style: { width: "0%" } }));
  const upMsg = h("span", { class: "hint" });
  fileIn.addEventListener("change", async () => {
    const f = fileIn.files[0];
    if (!f) return;
    st.upload = "";
    upBar.hidden = false;
    upMsg.textContent = "Uploading " + f.name + " ...";
    try {
      const res = await upload(f, (p) => (upBar.firstChild.style.width = `${Math.round(p * 100)}%`));
      st.upload = res.upload;
      upMsg.textContent = "Uploaded: " + res.upload;
    } catch (e) {
      upMsg.textContent = "";
      upBar.hidden = true;
      fail(e);
    }
  });
  const panes = {
    url: field("Live stream link (Twitch, YouTube Live, Kick)", urlIn),
    upload: h("div", { class: "field" }, h("span", { text: "Video file" }), fileIn, upBar, upMsg),
    path: field("Full path of a video on this PC", pathIn, "Put the path without quotes. Nothing is copied."),
  };
  const tabDefs = [["url", "Link"], ["upload", "Upload a video"]];
  if (!LAN) tabDefs.push(["path", "File on this PC"]);
  const tabs = h("div", { class: "tabs", role: "tablist" });
  const paneBox = h("div");
  const showTab = (k) => {
    st.tab = k;
    [...tabs.children].forEach((b) => {
      b.classList.toggle("on", b.dataset.k === k);
      b.setAttribute("aria-selected", String(b.dataset.k === k));
    });
    paneBox.replaceChildren(panes[k]);
  };
  tabDefs.forEach(([k, l]) => tabs.append(h("button", { type: "button", role: "tab", dataset: { k }, text: l, onclick: () => showTab(k) })));
  showTab("url");
  const scoreVal = h("strong", { text: String(st.min_score) });
  const score = h("input", { type: "range", min: 0, max: 100, step: 5, value: String(st.min_score), "aria-label": "Minimum score" });
  score.addEventListener("input", () => {
    st.min_score = Number(score.value);
    scoreVal.textContent = score.value;
  });
  const num = (key, min, max, label, hint) => {
    const i = h("input", { type: "number", min: String(min), max: String(max), value: String(st[key]) });
    i.addEventListener("change", () => (st[key] = Number(i.value)));
    return field(label, i, hint);
  };
  const toggle = (key, label) => {
    const c = h("input", { type: "checkbox", checked: st[key], role: "switch" });
    c.addEventListener("change", () => (st[key] = c.checked));
    return h("label", { class: "switch" }, c, h("span", { class: "switch-ui", "aria-hidden": "true" }), label);
  };
  const ratioSel = sel(OPTS.ratios, st.ratio, { onchange: (e) => (st.ratio = e.target.value) });
  const err = h("p", { class: "err", role: "alert" });
  const submit = h("button", { class: "btn primary", type: "submit", id: "live-start" }, icon("radio", 16), "Start live clipping");
  const form = h(
    "form",
    { class: "card stack", id: "live-form" },
    h("div", { class: "sec-head" }, h("h2", { text: "Start a live session" }), h("p", { text: "Paste the channel link while it is live. Clips appear above as soon as they are ready." })),
    tabs,
    paneBox,
    h("div", { class: "grid3" }, field("Aspect ratio", ratioSel), h("div", { class: "field" }, h("span", {}, "Minimum score ", scoreVal), score, h("span", { class: "hint", text: "Higher = fewer, stronger clips" })), num("stop_after", 0, 1440, "Stop after (minutes)", "0 = when the stream ends"), num("max_clips_per_hour", 1, 60, "Max clips per hour")),
    h("div", { class: "row" }, toggle("render", "Render ready-to-post MP4s"), toggle("drafts", "CapCut drafts")),
    h("details", { class: "styles-drawer" }, h("summary", { text: "Caption and hook style" }), h("div", { class: "stack" }, picker("caption", st.caption_style, (n) => (st.caption_style = n)), picker("hook", st.hook_style, (n) => (st.hook_style = n)))),
    h("div", { class: "row" }, submit, err)
  );
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    err.textContent = "";
    const body = { ...st, mode: "live" };
    delete body.tab;
    if (st.tab === "url") body.source = urlIn.value.trim();
    else if (st.tab === "path") body.source = pathIn.value.trim();
    else if (!st.upload) {
      err.textContent = "Choose a video and wait for the upload to finish.";
      return;
    }
    if (st.tab !== "upload") delete body.upload;
    submit.disabled = true;
    try {
      await api("POST", "/api/jobs", body);
      toast("Live clipping started.", "ok");
      route();
    } catch (e) {
      err.textContent = e.message + (e.hint ? ` (${e.hint})` : "");
    } finally {
      submit.disabled = false;
    }
  });
  return form;
}

async function pageLive(root) {
  const sessions = h("div", { class: "stack", id: "live-sessions" }, skeleton("block", 1));
  root.replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Live clipping" }), h("p", { class: "muted", text: "Clip a stream while it is still live. Clips appear here as soon as they are ready." }))),
    sessions,
    liveForm()
  );
  let last = "";
  const refresh = async () => {
    let jobs;
    try {
      jobs = (await api("GET", "/api/jobs")).jobs.filter((j) => j.kind === "live");
    } catch (e) {
      return;
    }
    const active = jobs.find((j) => ACTIVE.includes(j.status));
    let clips = [];
    if (active) {
      try {
        clips = (await api("GET", `/api/jobs/${enc(active.id)}/clips`)).clips;
      } catch (e) {
        clips = [];
      }
    }
    const sig = JSON.stringify([jobs, clips]);
    if (sig === last) return;
    last = sig;
    const parts = [];
    if (active) {
      parts.push(
        h(
          "div",
          { class: "card stack live-card", id: "live-active" },
          h("div", { class: "row" }, h("span", { class: "live-dot", "aria-hidden": "true" }), h("h2", { class: "ellipsis grow", text: active.label, style: { margin: 0 } }), pill(active.status), ...jobActions(active, refresh), h("a", { class: "btn sm", href: `#/project/${enc(active.id)}` }, "Open")),
          progressBlock(active),
          clips.length ? h("div", { class: "clips" }, clips.map((c) => clipCard(active, c, LAN, refresh))) : h("p", { class: "hint", text: "No clip yet. A moment needs to pass the minimum score first." })
        )
      );
    } else parts.push(emptyState("radio", "No live session running", "Paste a live stream link below to start."));
    const past = jobs.filter((j) => j !== active).slice(0, 8);
    if (past.length) {
      parts.push(h("div", { class: "card stack" }, h("h3", { text: "Past sessions" }), h("div", { class: "past" }, past.map((j) => h("a", { class: "past-row", href: `#/project/${enc(j.id)}` }, pill(j.status), h("span", { class: "grow ellipsis", text: j.label }), h("span", { class: "muted small", text: `${j.clip_count || 0} clips · ${ago(j.created)}` }))))));
    }
    sessions.replaceChildren(...parts);
  };
  await refresh();
  every(3000, refresh);
}

// ---- styles -------------------------------------------------------------------------------------------------

async function pageStyles(root) {
  const defs = { caption: (OPTS.defaults || {}).caption_style || "auto", hook: (OPTS.defaults || {}).hook_style || "bold-box" };
  let cur = { kind: "caption", name: defs.caption };
  const big = h("div", { class: "style-big" });
  const nameEl = h("h2", { class: "style-name" });
  const lookEl = h("p", { class: "muted" });
  const setBtn = h("button", { class: "btn primary", type: "button", id: "style-default" }, icon("check", 16), "Set as default");
  const isDef = h("span", { class: "chip ok-chip", hidden: true }, icon("check", 12), "Default for new projects");
  const panel = h("aside", { class: "card style-panel", id: "style-panel" }, h("span", { class: "label", id: "style-kind" }), big, nameEl, lookEl, h("div", { class: "row tight" }, setBtn, isDef));
  const kindEl = panel.querySelector("#style-kind");
  const marks = () =>
    root.querySelectorAll(".tile").forEach((t) => {
      const k = t.closest(".picker").dataset.kind;
      const old = t.querySelector(".def-badge");
      if (old) old.remove();
      if (t.dataset.name === defs[k]) t.append(h("span", { class: "def-badge", text: "Default" }));
    });
  const show = () => {
    const t = tileOf(cur.kind, cur.name);
    big.replaceChildren(t ? sprite(t) : autoArt());
    kindEl.textContent = cur.kind === "caption" ? "Caption style" : "Hook style";
    nameEl.textContent = cur.name === "auto" ? "auto (per clip)" : cur.name;
    lookEl.textContent = (cur.kind === "caption" ? OPTS.caption_looks[cur.name] : OPTS.hook_looks[cur.name]) || "Picked per clip from its category.";
    const d = defs[cur.kind] === cur.name;
    setBtn.hidden = d;
    isDef.hidden = !d;
  };
  setBtn.addEventListener("click", async () => {
    setBtn.disabled = true;
    try {
      const r = await api("POST", "/api/settings", { defaults: { [cur.kind === "caption" ? "caption_style" : "hook_style"]: cur.name } });
      OPTS.defaults = r.defaults;
      defs.caption = r.defaults.caption_style;
      defs.hook = r.defaults.hook_style;
      toast(`${cur.name} is now the default ${cur.kind} style.`, "ok");
      marks();
      show();
    } catch (e) {
      fail(e);
    } finally {
      setBtn.disabled = false;
    }
  });
  const capPick = picker("caption", defs.caption, (n) => ((cur = { kind: "caption", name: n }), show()));
  capPick.dataset.kind = "caption";
  const hookPick = picker("hook", defs.hook, (n) => ((cur = { kind: "hook", name: n }), show()));
  hookPick.dataset.kind = "hook";
  root.replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Styles" }), h("p", { class: "muted", text: "How captions and the hook headline look on your clips. Click a style to preview it; set it as the default for new projects." }))),
    h(
      "div",
      { class: "styles-layout" },
      h("div", { class: "stack" }, h("section", { class: "card stack" }, h("div", { class: "sec-head" }, h("h2", { text: "Caption styles" }), h("p", { text: "The spoken words on the clip." })), capPick), h("section", { class: "card stack" }, h("div", { class: "sec-head" }, h("h2", { text: "Hook styles" }), h("p", { text: "The headline banner in the first seconds." })), hookPick)),
      panel
    )
  );
  marks();
  show();
}

// ---- settings -----------------------------------------------------------------------------------------------

async function pageSettings(root) {
  root.replaceChildren(h("div", { class: "page-head" }, h("h1", { text: "Settings" })), ...skeleton("block", 3));
  const s = await api("GET", "/api/settings");
  const wsIn = h("input", { type: "text", value: s.workspace_default, id: "ws-input" });
  const d = s.defaults;
  const pSel = sel([["", "Auto (streamer split)"], ...OPTS.presets], d.preset, { id: "set-preset" });
  const cSel = sel(OPTS.caption_styles, d.caption_style, { id: "set-cap" });
  const hSel = sel(OPTS.hook_styles, d.hook_style, { id: "set-hook" });
  const save = async (body, btn) => {
    btn.disabled = true;
    try {
      const r = await api("POST", "/api/settings", body);
      toast(r.message, "ok");
      OPTS = null;
    } catch (e) {
      fail(e);
    } finally {
      btn.disabled = false;
    }
  };
  const sharesBox = h("div", { class: "stack" }, skeleton("line", 2));
  const brandBox = h("div", { class: "stack", id: "brand-kit" }, skeleton("line", 2));
  const pubBox = h("div", { class: "stack", id: "publishing" }, skeleton("line", 2));
  const doctorBox = h("div");
  const row = (k, v, extra) => h("div", { class: "kv" }, h("span", { class: "k", text: k }), h("span", { class: "v mono", text: v }), extra || null);
  const status = (ok, text) => h("span", { class: "chip " + (ok ? "ok-chip" : "warn-chip") }, icon(ok ? "check" : "info", 12), text);
  const section = (ic, title, desc, ...kids) => h("section", { class: "card stack set-sec" }, h("div", { class: "sec-title" }, h("span", { class: "sec-ic" }, icon(ic, 18)), h("div", {}, h("h2", { text: title }), desc ? h("p", { class: "muted small", text: desc }) : null)), ...kids);
  const bDefaults = h("button", { class: "btn primary", type: "button", id: "save-defaults" }, "Save defaults");
  bDefaults.addEventListener("click", () => save({ defaults: { preset: pSel.value, caption_style: cSel.value, hook_style: hSel.value } }, bDefaults));
  const bWs = h("button", { class: "btn", type: "button", id: "save-ws" }, "Save workspace");
  bWs.addEventListener("click", () => save({ workspace: wsIn.value }, bWs));
  const bDoc = h("button", { class: "btn", type: "button", id: "run-doctor", onclick: () => runDoctor(doctorBox, bDoc) }, icon("shield-check", 16), "Run system check");
  const llmOk = OPTS.llms.includes("ollama");
  root.replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Settings" }), h("p", { class: "muted", text: "Defaults for new projects, folders, CapCut, AI and a system check." }))),
    h(
      "div",
      { class: "settings-grid" },
      section("sliders", "Defaults for new projects", "Used by Get clips on Home.", h("div", { class: "grid3" }, field("Layout preset", pSel), field("Caption style", cSel), field("Hook style", hSel)), h("div", { class: "row tight" }, bDefaults, h("a", { class: "btn ghost", href: "#/styles" }, icon("palette", 16), "Browse styles"))),
      section("folder", "Workspace", "Jobs, uploads and clips are saved here.", row("In use", s.workspace), field("Default workspace folder", wsIn, "Restart the dashboard to use a new folder."), h("div", {}, bWs)),
      section("scissors", "CapCut", "Only needed for CapCut drafts.", row("Draft folder", s.draft_root), row("CapCut", s.capcut === "open" ? "open" : "closed", s.capcut === "open" ? status(false, "Close it before Open in CapCut") : status(true, "Ready"))),
      section(
        "sparkles",
        "AI for titles",
        "Optional. Rule-based titles work without AI.",
        row("Gemini API key", s.gemini, status(s.gemini === "set", s.gemini === "set" ? "Set" : "Not set")),
        row("Ollama", llmOk ? "running" : "not running", status(llmOk, llmOk ? "Available" : "Optional")),
        s.gemini === "set" ? null : h("p", { class: "hint", text: "To use Gemini, set GEMINI_API_KEY before you start the dashboard. The key is never shown here." })
      ),
      section("palette", "Brand kit", "Logo, handle, font, colours and intro/outro cards. Turn it on per project under Advanced on Home.", brandBox),
      section("upload", "Publishing", "Post clips with your own YouTube and Instagram accounts. Uploads are private unless you pick unlisted.", pubBox),
      section("share-2", "Share links", s.mode === "lan" ? "Network mode: links work for people on the same Wi-Fi." : "This PC only.", sharesBox),
      section("shield-check", "System check", "Checks Python, ffmpeg, Whisper, the GPU and CapCut.", h("div", {}, bDoc), doctorBox)
    )
  );
  const loadShares = async () => {
    const r = await api("GET", "/api/shares");
    if (!r.lan) {
      sharesBox.replaceChildren(h("p", { class: "muted", text: "Share links work on your Wi-Fi/network when you start the dashboard with: streamclipper dashboard --lan" }));
      if (!r.shares.length) return;
    } else sharesBox.replaceChildren();
    if (!r.shares.length) {
      sharesBox.append(h("p", { class: "muted", text: "No share links yet. Use the Share button of a clip or a project." }));
      return;
    }
    sharesBox.append(
      h(
        "table",
        { id: "shares" },
        h("thead", {}, h("tr", {}, h("th", { text: "What" }), h("th", { text: "Expires" }), h("th", { text: "State" }), h("th", { text: "" }))),
        h(
          "tbody",
          {},
          r.shares.map((x) =>
            h(
              "tr",
              { dataset: { id: x.id } },
              h("td", { text: x.kind === "clip" ? `Clip ${x.clip}` : `All clips of job ${x.job}` }),
              h("td", { text: when(x.expires) }),
              h("td", {}, pill(x.active ? "done" : "failed", x.active ? "active" : x.revoked ? "revoked" : "expired")),
              h(
                "td",
                {},
                x.active
                  ? h("button", {
                      class: "btn sm danger",
                      type: "button",
                      text: "Revoke",
                      onclick: async () => {
                        if (!(await confirmModal("Revoke this link?", "People who have it can no longer open it.", "Revoke"))) return;
                        try {
                          await api("POST", `/api/shares/${enc(x.id)}/revoke`);
                          toast("Link revoked.", "ok");
                          loadShares();
                        } catch (e) {
                          fail(e);
                        }
                      },
                    })
                  : null
              )
            )
          )
        )
      )
    );
  };
  brandCard(brandBox);
  publishCard(pubBox);
  await loadShares();
}

async function runDoctor(box, btn) {
  box.replaceChildren(spinner("Running the system check ..."));
  if (btn) btn.disabled = true;
  try {
    await api("POST", "/api/doctor");
    for (let i = 0; i < 120; i++) {
      const r = await api("GET", "/api/doctor");
      if (!r.running && r.rows) {
        box.replaceChildren(
          h(
            "div",
            { class: "table-wrap" },
            h(
              "table",
              {},
              h("thead", {}, h("tr", {}, h("th", { text: "Check" }), h("th", { text: "Status" }), h("th", { text: "Detail" }), h("th", { text: "Fix" }))),
              h("tbody", {}, r.rows.map((x) => h("tr", {}, h("td", { text: x[0] }), h("td", {}, pill(x[1] === "OK" ? "done" : x[1] === "MISSING" ? "failed" : "queued", x[1])), h("td", { class: "detail", text: x[2] }), h("td", { class: "detail", text: x[3] || "" }))))
            )
          )
        );
        return;
      }
      await new Promise((res) => setTimeout(res, 1000));
    }
  } catch (e) {
    box.replaceChildren(errorBox(e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---- start --------------------------------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  setupShell();
  window.addEventListener("hashchange", route);
  route();
});
