"""
Rolling live analysis. Per new segment: 10 ms envelopes and Whisper words (2 s overlap, de-duplicated),
in absolute stream time. Then the VOD candidate builder and quality bar (discovery.build_candidates +
discovery.select, i.e. scoring.passes_bar) run on the last <= 10 min, and a moment is emitted once its
reaction has ended. Nothing here re-implements scoring.
"""

import threading

from streamclipper.audio import HOP_S, _np, envelope_arrays, find_peaks, islands_from_env, laughter_bursts, peak_near
from streamclipper.chat import chat_signal, spikes
from streamclipper.discovery import build_candidates, select
from streamclipper.games import detect_game
from streamclipper.scoring import DEFAULT_BAR
from streamclipper.story import sentence_boundaries
from streamclipper.util import norm_token, run_cmd

LIVE_ST = {
    "min_dur": 15.0,
    "max_dur": 60.0,
    "target_dur": 30.0,
    "min_pause": 0.55,
    "db": -48.0,
    "min_score": DEFAULT_BAR["min_score"],
    "max_clips": 0,
    "rank_by": "score",
}
ANALYSIS_S = 600.0
MIN_BUFFER_S = 90.0
MAX_WAIT_S = 20.0
MERGE_FRAC = 0.30
JUDGED_TOL_S = 2.0  # a peak this close to a judged one is the same moment
OVERLAP_S = 2.0
SR = 16000
CLIP_WORDS = ("clip that", "clip it", "clip this")  # spoken commands that make a clip on the spot


def overlap(a: dict, b: dict) -> float:
    return max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def shift_clip(c: dict, dt: float) -> dict:
    """A candidate/clip dict with every time moved by dt seconds (window time <-> stream time)."""
    out = dict(c)
    for k in ("start", "end", "peak"):
        if k in out:
            out[k] = out[k] + dt
    if "arc" in c:
        out["arc"] = {k: [a + dt, b + dt] for k, (a, b) in c["arc"].items()}
    if "segments" in c:
        out["segments"] = [{**s, "src_s": s["src_s"] + dt, "src_e": s["src_e"] + dt} for s in c["segments"]]
    if "words" in c:
        out["words"] = [{**w, "s": w["s"] + dt, "e": w["e"] + dt} for w in c["words"]]
    return out


def merge_words(old: list, new: list, cut: float) -> list:
    """Words before cut from the previous pass, words from cut on from the new pass (overlap de-dupe)."""
    return [w for w in old if w["s"] < cut] + [w for w in new if w["s"] >= cut]


def find_clip_triggers(words: list, phrases=CLIP_WORDS) -> list:
    """[(t, label)] where a configured phrase is said (t = the first word's start), longest phrase first."""
    norm = [norm_token(w["w"]) for w in words]
    pats = sorted(
        ((tuple(norm_token(x) for x in str(p).split()), str(p)) for p in phrases or () if str(p).strip()),
        key=lambda p: -len(p[0]),
    )
    out, used = [], set()
    for pat, label in pats:
        n = len(pat)
        for i in range(len(norm) - n + 1):
            if tuple(norm[i : i + n]) == pat and not used.intersection(range(i, i + n)):
                out.append((float(words[i]["s"]), label))
                used.update(range(i, i + n))
    return sorted(out)


def is_ready(c: dict, bounds: list, buffered_end: float, max_wait: float = MAX_WAIT_S, final: bool = False) -> bool:
    """
    The reaction has ended: a fully buffered silence >= 0.6 s or a sentence end (speech after it buffered)
    from the clip end on; or max_wait seconds after the peak are buffered; or the stream has ended.
    """
    if final or c["peak"] + max_wait <= buffered_end:
        return True
    if c["end"] > buffered_end - 0.05:
        return False  # the window end is still the buffer edge
    for b in bounds:
        if b["gs"] < c["end"] - 0.3 or b["gs"] > buffered_end:
            continue
        if b["kind"] == "sentence" and b["ge"] < buffered_end:
            return True
        if min(b["ge"], buffered_end) - b["gs"] >= 0.6:
            return True
    return False


class Moment:
    """One emitted clip: queued for the worker, started, done or cancelled (replaced before it started)."""

    def __init__(self, clip: dict):
        self.clip = clip
        self.started = False
        self.cancelled = False
        self.done = False

    @property
    def start(self):
        return self.clip["start"]

    @property
    def end(self):
        return self.clip["end"]


class LiveASR:
    """Whisper per segment with OVERLAP_S of the previous segment in front (model loaded once)."""

    def __init__(self, model_name: str = "auto", device: str = "auto", language: str = None):
        self.model_name, self.device = model_name or "auto", device or "auto"
        self.language = None if not language or language == "auto" else language
        self.w = None
        self.tail = None
        self.langs = []

    def load(self):
        if self.w is None:
            from streamclipper.asr import load_model

            # duration 'long' -> CPU picks the small 'base' model; CUDA uses large-v3-turbo
            self.w = load_model(self.device, 1e9, self.model_name)
            if self.w.device == "cpu":
                print("Warning: Whisper runs on the CPU ('base' model); captions are less accurate and may lag.")
        return self.w

    def pcm(self, path) -> object:
        np = _np()
        res = run_cmd(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-vn",
                "-af",
                "highpass=f=180,lowpass=f=3800",
                "-ac",
                "1",
                "-ar",
                str(SR),
                "-f",
                "s16le",
                "-",
            ],
            "live audio for Whisper",
            binary=True,
        )
        return np.frombuffer(res.stdout or b"", dtype="<i2").astype(np.float32) / 32768.0

    def __call__(self, seg) -> tuple:
        """(words in stream time, cut): the caller keeps old words before cut and these from cut on."""
        np = _np()
        w = self.load()
        cur = self.pcm(seg.path)
        tail = self.tail if self.tail is not None else np.zeros(0, dtype=np.float32)
        audio = np.concatenate([tail, cur])
        t0 = seg.start - len(tail) / SR
        self.tail = cur[-int(OVERLAP_S * SR) :] if len(cur) else tail
        if len(audio) < SR // 2:
            return [], seg.start
        got, lang = w.run(audio, self.language)
        if lang:
            self.langs.append(lang)
        words = [{**x, "s": round(t0 + x["s"], 3), "e": round(t0 + x["e"], 3)} for x in got]
        return words, seg.start - (len(tail) / SR) / 2.0

    @property
    def language_guess(self) -> str:
        return max(set(self.langs), key=self.langs.count) if self.langs else (self.language or "auto")


class Engine:
    """Rolling state (absolute stream time) and the emit decisions."""

    def __init__(
        self,
        st: dict = None,
        transcriber=None,
        analysis_s: float = ANALYSIS_S,
        min_buffer_s: float = MIN_BUFFER_S,
        max_wait_s: float = MAX_WAIT_S,
        max_per_hour: int = 12,
        merge_frac: float = MERGE_FRAC,
        keep_s: float = 900.0,
        tmp_dir=None,
        clip_words=None,
    ):
        np = _np()
        self.st = dict(LIVE_ST, **(st or {}))
        self.transcriber = transcriber
        self.transcribed = transcriber is not None
        self.analysis_s = min(analysis_s, keep_s)
        self.min_buffer_s, self.max_wait_s = min_buffer_s, max_wait_s
        self.max_per_hour, self.merge_frac = int(max_per_hour), merge_frac
        self.keep_s = keep_s
        self.tmp_dir = tmp_dir
        self.clip_words = tuple(clip_words) if clip_words is not None else CLIP_WORDS
        self.full = np.zeros(0, dtype=np.float32)
        self.vox = np.zeros(0, dtype=np.float32)
        self.env_t0 = 0.0
        self.end = 0.0
        self.words = []
        self.moments = []
        self.lock = threading.Lock()
        self.notes = set()
        self.judged = []  # stream-time peaks already judged (emitted or rejected)
        self.prev_end = None
        self.candidates_seen = 0

    # ------------------------------------------------------------------ state
    def add(self, start: float, dur: float, full, vox, words: list = None, cut: float = None) -> None:
        """Append one segment's envelopes (padded/trimmed to dur) and words (merged at cut)."""
        np = _np()
        if not len(self.full):
            self.env_t0 = start
        n = int(round(dur / HOP_S))

        def fit(a):
            a = np.asarray(a, dtype=np.float32)[:n]
            return np.pad(a, (0, n - len(a)), constant_values=-96.0) if len(a) < n else a

        self.full = np.concatenate([self.full, fit(full)])
        self.vox = np.concatenate([self.vox, fit(vox)])
        self.end = start + dur
        if words is not None:
            self.words = merge_words(self.words, words, start if cut is None else cut)
        # keep memory bounded on 10 h streams: only what the analysis window can still use
        keep_from = self.end - max(self.analysis_s, self.keep_s) - 60.0
        drop = int((keep_from - self.env_t0) / HOP_S)
        if drop > 0:
            self.full, self.vox = self.full[drop:], self.vox[drop:]
            self.env_t0 += drop * HOP_S
            self.words = [w for w in self.words if w["e"] >= self.env_t0]

    def feed(self, seg) -> int:
        """Analyse one ring Segment (envelopes + Whisper). Returns the number of new words."""
        full, vox = envelope_arrays(seg.path, self.tmp_dir)
        words, cut = (None, None)
        if self.transcriber is not None:
            words, cut = self.transcriber(seg)
        self.add(seg.start, seg.dur, full, vox, words, cut)
        return len(words or [])

    # ------------------------------------------------------------------ analysis
    def candidates(self, chat_msgs=None) -> tuple:
        """(picked: passing the bar, best first; sentence bounds; w0; every candidate), all in window time."""
        w0 = max(self.env_t0, self.end - self.analysis_s)
        i0 = max(0, int(round((w0 - self.env_t0) / HOP_S)))
        env = {"full": self.full[i0:], "vox": self.vox[i0:], "hop": HOP_S}
        env["dur"] = dur = len(env["full"]) * HOP_S
        words = [{**w, "s": w["s"] - w0, "e": w["e"] - w0} for w in self.words if w["s"] >= w0]
        st = self.st
        islands = islands_from_env(env, st["db"], 0.20)
        peaks = find_peaks(env, 0, max(st["max_dur"], 20.0))
        # "clip that": a spoken command is a payoff on its own, so it does not wait for a loudness peak
        for t, label in find_clip_triggers(words, self.clip_words):
            if not any(abs(t - pk["t"]) < 3.0 for pk in peaks):
                peaks = peaks + [dict(peak_near(env, t, 1.5), t=round(t, 2), trigger=label)]
        sig, chat_spikes = None, []
        msgs = [(t - w0, text) for t, text in (chat_msgs or []) if w0 <= t <= self.end]
        if msgs:
            sig = chat_signal(msgs, dur)
            chat_spikes = spikes(sig)
        if not peaks and not chat_spikes:
            return [], [], w0, []
        laughs = laughter_bursts(env, st["db"])
        cands = build_candidates(
            env,
            words,
            islands,
            laughs,
            peaks,
            sig,
            chat_spikes,
            [],
            None,
            st,
            dur,
            transcribed=self.transcribed,
            game=detect_game(manual=st.get("game")),
        )
        self.candidates_seen = len(cands)
        picked = select(cands, st)
        # the command outranks the quality bar: a spoken "clip that" is why this clip exists at all
        for c in [x for x in cands if "trigger" in (x.get("sources") or []) and x["score"] > 0]:
            if not any(c["start"] < q["end"] and q["start"] < c["end"] for q in picked):
                picked.append(c)
        return picked, sentence_boundaries(words, islands, dur), w0, cands

    def analyze(self, chat_msgs=None, final: bool = False) -> list:
        """
        Ready moments to emit now: [(action, clip in stream time, replaced Moment or None)], action
        'new' or 'replace'. Needs >= min_buffer_s buffered (unless final).
        """
        if self.end - self.env_t0 < self.min_buffer_s and not final:
            return []
        picked, bounds, w0, cands = self.candidates(chat_msgs)
        chosen = {id(c) for c in picked}
        # a peak that was already ready at the previous analysis has been judged then
        horizon = -1e9 if self.prev_end is None else self.prev_end - self.max_wait_s - 1.0
        self.prev_end = self.end
        out = []
        # each moment is judged once, when its reaction has ended (as VOD mode sees it): a moment that fails
        # the bar then is not re-judged on later segments with a different baseline
        for c in sorted(cands, key=lambda c: -c["score"]):
            local_end = self.end - w0
            if c["start"] <= 0.05 and w0 > self.env_t0:
                continue  # cut off by the analysis window edge; it was judged when it was inside
            if not is_ready(c, bounds, local_end, self.max_wait_s, final):
                continue
            peak = c["peak"] + w0
            if peak < horizon or any(abs(peak - j) < JUDGED_TOL_S for j in self.judged):
                continue
            self.judged.append(peak)
            if id(c) not in chosen:
                continue
            clip = shift_clip(c, w0)
            action, other = self.decide(clip)
            if action in ("new", "replace"):
                out.append((action, clip, other))
        return out

    def decide(self, clip: dict) -> tuple:
        """
        ('new', None) | ('replace', queued Moment) | ('skip', reason) | ('rate', reason).
        Registers the moment for 'new'/'replace' (the caller hands it to the worker).
        """
        length = max(1e-6, clip["end"] - clip["start"])
        sep = max(self.st["max_dur"], 20.0)  # VOD peak rule (audio.find_peaks): one moment per max_dur seconds
        with self.lock:
            live = [m for m in self.moments if not m.cancelled]
            for m in live:
                ov = overlap(clip, m.clip)
                if ov <= self.merge_frac * length and abs(clip["peak"] - m.clip["peak"]) >= sep:
                    continue
                if not m.started and clip["score"] > m.clip["score"]:
                    m.cancelled = True
                    self.moments.append(Moment(clip))
                    return "replace", m
                return "skip", "overlaps (or is within a minute of) a clip already made"
            recent = [m for m in live if clip["peak"] - 3600.0 < m.clip["peak"]]  # sliding 60 min
            if self.max_per_hour and len(recent) >= self.max_per_hour:
                key = round(clip["peak"])
                if key not in self.notes:
                    self.notes.add(key)
                    print(f"Live: moment at {clip['peak']:.0f}s skipped: --max-clips-per-hour {self.max_per_hour}")
                return "rate", "max clips per hour"
            self.moments.append(Moment(clip))
            return "new", None

    def moment_for(self, clip: dict):
        with self.lock:
            return next((m for m in self.moments if m.clip is clip), None)
