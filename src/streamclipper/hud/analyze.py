"""HUD analysis for discover(): game profile, keyframe pass over the stream, dense pass per clip window.

keyframe_stage() runs once per stream (saved as the 'hud' stage in state.json, so a resume skips it) and
returns a Hud object; Hud.window(win) gives the events and the face signal of one candidate window.
"""

import hashlib
import json
import time
from pathlib import Path

from streamclipper.audio import _mmss
from streamclipper.hud import frames as F
from streamclipper.hud.detect import detect_events
from streamclipper.hud.ocr import get_recognizer, ocr_available
from streamclipper.hud.profiles import list_profiles, load_profile, match_title
from streamclipper.state import write_json_atomic
from streamclipper.util import fwd

HUD_MODES = ("auto", "off", "gta5", "gta6", "generic")
AUTO_PROFILES = ("gta6", "gta5")  # title keywords are checked in this order (gta6 before gta5)
AUTO_MIN_READS = 2  # keyframes with an expected gta5 word read before auto picks gta5 without a title match
REFINE = (3.0, 1.0)  # dense pass around a keyframe hit: [t - 3, t + 1] for the true onset
WINDOW_PAD = 2.0
FACE_HALF = 2.0  # face signal over payoff +- 2 s
_NOTICE = []


def _notice(msg: str):
    if msg not in _NOTICE:
        _NOTICE.append(msg)
        print(msg)


def _combined(profiles: list) -> dict:
    """One profile holding the events of several (ids prefixed 'n:'), so one frame pass serves all."""
    return {
        "name": "+".join(p["name"] for p in profiles),
        "events": [dict(e, id=f"{i}:{e['id']}") for i, p in enumerate(profiles) for e in p["events"]],
    }


def _split(events: list, i: int) -> list:
    pre = f"{i}:"
    return [dict(e, event=e["event"][len(pre) :]) for e in events if e["event"].startswith(pre)]


def _profile_key(profiles: list, video_tag: str) -> str:
    raw = json.dumps([profiles, F.KEY_W, F.KEY_STEP, F.DENSE_FPS, video_tag], sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def choose_profiles(st: dict, title: str):
    """(profiles to scan, forced) for st['hud'] / st['hud_profile'], or (None, False) when HUD is off."""
    mode = st.get("hud") or "off"
    if st.get("hud_profile"):
        return [load_profile(st["hud_profile"])], True
    if mode == "off":
        return None, False
    if mode != "auto":
        return [load_profile(mode)], True
    shipped = [load_profile(n) for n in AUTO_PROFILES if n in list_profiles()]
    hit = match_title(title, shipped)
    if hit is not None:
        return [hit], False
    return [load_profile("gta5"), load_profile("generic")], False


def refine(video: Path, profile: dict, ev: dict, ocr) -> dict:
    """The keyframe event with its onset and strength from a 3 fps pass around it (unchanged when it finds none)."""
    one = dict(profile, events=[e for e in profile["events"] if e["id"] == ev["event"]])
    a = max(0.0, ev["t"] - REFINE[0])
    found = detect_events(one, F.dense(video, a, ev["t"] + REFINE[1]), ocr)
    if not found:
        return ev
    best = found[0]
    return dict(ev, t=best["t"], strength=max(ev["strength"], best["strength"]), conf=max(ev["conf"], best["conf"]))


def scan(video: Path, profiles: list, ocr, refine_hits: bool = True) -> dict:
    """Keyframe pass with every profile at once, game choice, onset refinement.
    Returns {profile (dict), events, keyframes, reads}."""
    comb = _combined(profiles)
    reads, count = {}, [0]

    def tick(t, fr):
        count[0] += 1

    found = detect_events(comb, F.keyframes(video), ocr, on_frame=tick, stats=reads)
    pick = 0
    if len(profiles) > 1:  # auto without a title match: the first profile whose words were read, else the last
        pick = len(profiles) - 1
        for i in range(len(profiles) - 1):
            if sum(n for k, n in reads.items() if k.startswith(f"{i}:")) >= AUTO_MIN_READS:
                pick = i
                break
    prof = profiles[pick]
    events = _split(found, pick)
    if refine_hits:
        events = [refine(video, prof, e, ocr) for e in events]
    return {"profile": prof, "events": sorted(events, key=lambda e: e["t"]), "keyframes": count[0], "reads": reads}


def face_signal(samples: list) -> tuple:
    """(face_motion 0-1, face_seen 0-1) from [(t, box or None)]: movement of the largest face's centre relative
    to its height, plus its size change, over the samples."""
    boxes = [b for _, b in samples if b]
    seen = len(boxes) / len(samples) if samples else 0.0
    if len(boxes) < 2:
        return 0.0, round(seen, 3)
    hs = sorted(b[3] - b[1] for b in boxes)
    h = hs[len(hs) // 2] or 1e-6
    cx = [(b[0] + b[2]) / 2 for b in boxes]
    cy = [(b[1] + b[3]) / 2 for b in boxes]
    mx, my = sorted(cx)[len(cx) // 2], sorted(cy)[len(cy) // 2]
    disp = max(((x - mx) ** 2 + (y - my) ** 2) ** 0.5 for x, y in zip(cx, cy, strict=True)) / h
    size = (hs[-1] - hs[0]) / h
    return round(min(1.0, 0.5 * min(1.0, disp / 0.5) + 0.5 * min(1.0, size / 0.3)), 3), round(seen, 3)


class Hud:
    """The stream's HUD result plus the per-window dense pass (cached in <work>/hud/windows.json)."""

    def __init__(self, data: dict, video: Path, work_dir: Path, ocr=None, faces=None):
        self.data = data
        self.video = Path(video) if video else None
        self.profile = data.get("profile_doc")
        self.events = data.get("events") or []
        self.ocr = ocr
        self.faces = faces
        self.cache_p = Path(work_dir) / "hud" / "windows.json"
        try:
            self.cache = json.loads(self.cache_p.read_text(encoding="utf-8")) if self.cache_p.is_file() else {}
        except ValueError:
            self.cache = {}
        self.dense_s = 0.0
        self.dense_n = 0

    @property
    def name(self) -> str:
        return self.data.get("profile") or "off"

    def strong(self, min_strength: float) -> list:
        return [e for e in self.events if e["strength"] >= min_strength]

    def _detector(self):
        if self.faces is None:
            from streamclipper.faces import get_detector

            self.faces = get_detector() or False
        return self.faces or None

    def window(self, win: dict) -> dict:
        """{events, face_motion, face_seen} for a candidate window: a 3 fps pass over [start - 2, end + 2]
        when a keyframe event is near, a payoff +-2 s pass for the face signal otherwise."""
        key = f"{win['start']:.2f}-{win['end']:.2f}-{win['peak']:.2f}"
        if key in self.cache:
            return self.cache[key]
        a, b, p = win["start"] - WINDOW_PAD, win["end"] + WINDOW_PAD, win["peak"]
        near = [e for e in self.events if a - 10.0 <= e["t"] <= b + 10.0]
        evs, samples = [dict(e) for e in near if a <= e["t"] <= b], []
        t0 = time.time()
        if self.video is not None:
            det = self._detector()

            def on_frame(t, fr):
                if det is not None and abs(t - p) <= FACE_HALF:
                    faces = det.detect(fr)
                    samples.append((t, faces[0]["box"] if faces else None))

            if near and self.profile:
                for e in detect_events(self.profile, F.dense(self.video, a, b), self.ocr, on_frame=on_frame):
                    if not any(x["event"] == e["event"] and abs(x["t"] - e["t"]) <= 6.0 for x in evs):
                        evs.append(e)
            elif det is not None:
                for t, fr in F.dense(self.video, p - FACE_HALF, p + FACE_HALF):
                    on_frame(t, fr)
            self.dense_n += 1
        self.dense_s += time.time() - t0
        motion, seen = face_signal(samples) if samples else (None, None)
        out = {"events": sorted(evs, key=lambda e: e["t"]), "face_motion": motion, "face_seen": seen}
        self.cache[key] = out
        return out

    def save(self):
        if self.video is not None:
            write_json_atomic(self.cache_p, self.cache)


def keyframe_stage(source: Path, url: str, work_dir: Path, state, st: dict, title: str, dur: float):
    """The stream's Hud (from state.json when the same profile ran before), or None when HUD is off/skipped."""
    mode = st.get("hud") or "off"
    if mode == "off" and not st.get("hud_profile"):
        return None
    if mode == "auto" and not st.get("hud_profile") and not ocr_available():
        _notice("HUD detection is off: it needs the vision extra (pip install -e .[vision]).")
        return None
    profiles, forced = choose_profiles(st, title)
    if not profiles:
        return None
    key = _profile_key(profiles, url or Path(source).name)
    saved = state.stages.get("hud") or {}
    work_dir = Path(work_dir)
    if saved.get("key") == key:
        if saved.get("skipped"):
            return None
        video = Path(saved["video"])
        if video.is_file():
            print(f"Resuming: HUD keyframe pass already done ({len(saved['events'])} events)")
            return Hud(saved, video, work_dir, get_recognizer())
    video = F.analysis_source(source, url, work_dir, force=forced)
    if video is None:
        state.set_stage("hud", {"key": key, "skipped": True})
        return None
    ocr = get_recognizer()
    if any(p["experimental"] for p in profiles):
        _notice(f"HUD profile {profiles[0]['name']} is experimental (GTA V words and regions; see calibrate-hud).")
    t0 = time.time()
    res = scan(video, profiles, ocr)
    secs = time.time() - t0
    prof = res["profile"]
    data = {
        "key": key,
        "profile": prof["name"],
        "label": prof["label"],
        "experimental": prof["experimental"],
        "ocr": ocr is not None,
        "video": fwd(video),
        "keyframes": res["keyframes"],
        "seconds": round(secs, 1),
        "events": res["events"],
        "profile_doc": prof,
    }
    state.set_stage("hud", data)
    print(
        f"HUD ({prof['label']}{'' if ocr else ', colour cues only'}): {_mmss(dur)} stream, "
        f"{res['keyframes']:,} keyframes in {secs:.0f} s, {len(res['events'])} events"
    )
    return Hud(data, video, work_dir, ocr)


def clip_events(events: list) -> list:
    """Events for clips.json: [{t, event, label}] with times rounded to 0.01 s."""
    return [{"t": round(float(e["t"]), 2), "event": e["event"], "label": e["label"]} for e in events]
