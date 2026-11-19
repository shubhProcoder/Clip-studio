"""Whisper transcription (faster-whisper): device/model choice, candidate regions, chunked full streams."""

import json
import time
from collections import Counter
from pathlib import Path

from streamclipper.audio import _mmss
from streamclipper.env import cuda_status, gpu_info
from streamclipper.state import write_json_atomic
from streamclipper.util import TOOL_HINTS, ToolError, file_tag, run_cmd

CUDA_MODELS = ("large-v3-turbo", "large-v3", "small")  # tried in order (download/load failure -> next)
CPU_SHORT_MODEL, CPU_LONG_MODEL, CPU_LONG_AFTER_S = "small", "base", 1800.0
CHUNK_S, CHUNK_SLACK_S = 600.0, 15.0
_VOICE_BAND = "highpass=f=180,lowpass=f=3800"  # keeps game SFX from turning into hallucinated words

# Whisper writes Hindi speech in Urdu script when nothing anchors the script. A Devanagari sentence as the
# initial prompt keeps --language hi in Devanagari, so the captions and the LLM prompt agree.
HINDI_SCRIPT_ANCHOR = "यह एक हिंदी गेमिंग स्ट्रीम है, लड़के हिंदी में बात कर रहे हैं और खेल खेल रहे हैं।"


def script_anchor(language) -> str:
    """The initial prompt that keeps the transcript in the right script (Devanagari for Hindi, else none)."""
    return HINDI_SCRIPT_ANCHOR if language == "hi" else None


def choose_model(device: str, duration: float, requested: str = "auto") -> list:
    """Model names to try, best first. 'auto': CUDA -> large-v3-turbo/large-v3/small; CPU -> small or base."""
    if requested and requested != "auto":
        return [requested]
    if device == "cuda":
        return list(CUDA_MODELS)
    return [CPU_SHORT_MODEL if duration <= CPU_LONG_AFTER_S else CPU_LONG_MODEL]


def _short(e: Exception) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:160]


def _is_cuda_error(e: Exception) -> bool:
    s = str(e).lower()
    return any(k in s for k in ("cuda", "cublas", "cudnn", "gpu", "out of memory", ".dll", ".so"))


class Whisper:
    """A loaded faster-whisper model plus how it runs (batched pipeline on CUDA, plain on CPU)."""

    def __init__(self, model, name: str, device: str, compute_type: str, batch_size: int = 0):
        self.model, self.name, self.device, self.compute_type = model, name, device, compute_type
        self.batch_size = batch_size
        self._pipe = None

    @property
    def label(self) -> str:
        extra = f", batch {self.batch_size}" if self.device == "cuda" and self.batch_size else ""
        return f"device {self.device} ({self.compute_type}{extra}), model {self.name}"

    def run(self, audio, language=None) -> tuple:
        """(words relative to the audio start, detected language). audio = wav path or 16 kHz float array."""
        kw = {
            "word_timestamps": True,
            "vad_filter": True,
            "language": language,
            "initial_prompt": script_anchor(language),
        }
        if isinstance(audio, (str, Path)):
            audio = read_wav16k(audio)
        if self.device == "cuda" and self.batch_size:
            if self._pipe is None:
                try:
                    from faster_whisper import BatchedInferencePipeline

                    self._pipe = BatchedInferencePipeline(model=self.model)
                except ImportError:
                    self.batch_size = 0
            if self._pipe is not None:
                segs, info = self._pipe.transcribe(audio, batch_size=self.batch_size, **kw)
            else:
                segs, info = self.model.transcribe(audio, **kw)
        else:
            segs, info = self.model.transcribe(audio, **kw)
        words = []
        for seg in segs:  # decoding happens while iterating, so runtime errors surface here
            for w in seg.words or []:
                txt = w.word.strip()
                if txt:
                    words.append({"w": txt, "s": round(w.start, 3), "e": round(w.end, 3)})
        return words, info.language


def read_wav16k(path):
    """16 kHz mono 16-bit wav (from _extract_wav) -> float32 array. faster-whisper then never decodes files
    itself: its PyAV decoder breaks with av >= 19 (open() lost the metadata_errors argument)."""
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as f:
        if f.getsampwidth() != 2 or f.getnchannels() != 1 or f.getframerate() != 16000:
            raise ToolError(f"Unexpected Whisper input format in {path}.", "Delete the work folder and run again.")
        data = f.readframes(f.getnframes())
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def _warmup(model) -> None:
    """Run the encoder once so missing CUDA DLLs fail now, not in the middle of a long stream."""
    import numpy as np

    segs, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language="en", beam_size=1, vad_filter=False)
    list(segs)


def load_model(device: str = "auto", duration: float = 0.0, requested: str = "auto") -> Whisper:
    """
    Order: cuda float16 -> cuda int8_float16 -> cpu int8. 'auto' models per choose_model(); a model that
    fails to download/load falls back to the next one. Any CUDA load error falls back to the CPU.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ToolError(
            "Transcription needs faster-whisper, which is not installed.", TOOL_HINTS["faster-whisper"]
        ) from None
    attempts = []
    if device in ("auto", "cuda"):
        cuda_ok, detail = cuda_status()
        if cuda_ok:
            attempts += [("cuda", "float16"), ("cuda", "int8_float16")]
        elif device == "cuda":
            print(f"CUDA is not available ({detail}); using the CPU. Fix: pip install -e .[gpu]")
    attempts.append(("cpu", "int8"))
    bad, errors = set(), []
    for dev, ctype in attempts:
        for name in choose_model(dev, duration, requested):
            if name in bad:
                continue
            t0 = time.time()
            try:
                model = WhisperModel(name, device=dev, compute_type=ctype)
                if dev == "cuda":
                    _warmup(model)
            except Exception as e:
                errors.append(f"{name} on {dev}/{ctype}: {_short(e)}")
                print(f"  could not load Whisper '{name}' on {dev} ({ctype}): {_short(e)}")
                if dev == "cuda" and _is_cuda_error(e):
                    break  # this compute type does not work on this GPU; try the next one
                bad.add(name)
                continue
            batch = 0
            if dev == "cuda":
                vram = gpu_info()[1]
                batch = 16 if vram >= 12000 else 8
            w = Whisper(model, name, dev, ctype, batch)
            print(f"Whisper: {w.label} (loaded in {time.time() - t0:.1f}s)")
            return w
    raise ToolError(
        "faster-whisper could not load any model: " + "; ".join(errors[-3:]),
        "check the --whisper-model name and your network (first use downloads the model), or pass --asr none",
    )


def _extract_wav(source: Path, a: float, b: float, wav: Path) -> Path:
    """[a, b] of the source as 16 kHz mono wav in the voice band."""
    wav.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{a:.3f}",
            "-i",
            str(source),
            "-t",
            f"{b - a:.3f}",
            "-vn",
            "-af",
            _VOICE_BAND,
            "-ac",
            "1",
            "-ar",
            "16000",
            str(wav),
        ],
        "audio extraction for Whisper",
    )
    return wav


def transcribe_regions(
    source: Path, regions: list, model_name: str, language: str, work_dir: Path, device: str = "cpu"
) -> tuple:
    """
    faster-whisper on candidate regions only (merged when overlapping). Each region is cut as 16 kHz
    mono wav (voice band) and cached as asr_<hash>.json. Returns (words in source seconds, language).
    """
    merged = []
    for a, b in sorted(regions):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    lang_arg = None if not language or language == "auto" else language
    model = None
    words, langs = [], []
    total = sum(b - a for a, b in merged)
    for k, (a, b) in enumerate(merged, 1):
        tag = file_tag(source, round(a, 2), round(b, 2), model_name, language, "asr1")
        cache = work_dir / f"asr_{tag}.json"
        t0 = time.time()
        if cache.is_file():
            data = json.loads(cache.read_text(encoding="utf-8"))
        else:
            wav = _extract_wav(source, a, b, work_dir / f"asr_{tag}.wav")
            if model is None:
                model = load_model(device, total, model_name)
                t0 = time.time()
            got, lang = model.run(wav, lang_arg)
            data = {
                "language": lang,
                "words": [{**w, "s": round(a + w["s"], 3), "e": round(a + w["e"], 3)} for w in got],
            }
            cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            wav.unlink()
        print(
            f"  ASR region {k}/{len(merged)} {_mmss(a)}-{_mmss(b)}: {len(data['words'])} words "
            f"({time.time() - t0:.1f}s)"
        )
        words.extend(data["words"])
        if data.get("language"):
            langs.append(data["language"])
    words.sort(key=lambda w: w["s"])
    lang = max(set(langs), key=langs.count) if langs else (language or "auto")
    return words, lang


# ---------------------------------------------------------------- chunked full transcription


def silence_gaps(islands: list, dur: float) -> list:
    """(start, end) of every stretch between speech islands (padded island bounds), incl. head and tail."""
    gaps, prev = [], 0.0
    for isl in sorted(islands, key=lambda x: x["cut_start"]):
        if isl["cut_start"] > prev:
            gaps.append((prev, float(isl["cut_start"])))
        prev = max(prev, float(isl["cut_end"]))
    if dur > prev:
        gaps.append((prev, float(dur)))
    return gaps


def chunk_bounds(dur: float, islands: list, chunk_s: float = CHUNK_S, slack_s: float = CHUNK_SLACK_S) -> list:
    """
    [(a, b)] covering [0, dur] in ~chunk_s pieces. Each cut sits inside the silence gap nearest to the
    target time within +-slack_s (so no word is split); with no gap in reach it cuts at the target.
    """
    if dur <= 0:
        return []
    gaps = silence_gaps(islands, dur)
    cuts = [0.0]
    while dur - cuts[-1] > chunk_s + slack_s:
        t = cuts[-1] + chunk_s
        best = None
        for g0, g1 in gaps:
            lo, hi = max(g0, t - slack_s), min(g1, t + slack_s)
            if hi <= lo:
                continue
            mid = (lo + hi) / 2.0
            key = (abs(mid - t), -(hi - lo))
            if best is None or key < best[0]:
                best = (key, mid)
        cuts.append(round(best[1] if best else t, 3))
    return list(zip(cuts, cuts[1:] + [round(float(dur), 3)], strict=True))


class _ChunkTranscriber:
    """Default chunk transcriber: wav cut -> Whisper; on a CUDA error it reloads on the CPU and retries."""

    def __init__(self, source: Path, tmp_dir: Path, device: str, model_name: str, language, duration: float):
        self.source, self.tmp_dir, self.device = Path(source), Path(tmp_dir), device
        self.model_name, self.language, self.duration = model_name, language, duration
        self.w = None

    def load(self):
        if self.w is None:
            self.w = load_model(self.device, self.duration, self.model_name)

    @property
    def label(self) -> str:
        return self.w.label if self.w else "-"

    def __call__(self, a: float, b: float) -> tuple:
        self.load()
        wav = _extract_wav(self.source, a, b, self.tmp_dir / "chunk_tmp.wav")
        try:
            try:
                got, lang = self.w.run(wav, self.language)
            except Exception as e:
                if self.w.device != "cuda":
                    raise
                print(f"CUDA error during transcription ({_short(e)}); switching to the CPU for the remaining chunks.")
                self.w = load_model("cpu", self.duration, self.model_name)
                got, lang = self.w.run(wav, self.language)
        finally:
            wav.unlink(missing_ok=True)
        return [{**w, "s": round(a + w["s"], 3), "e": round(a + w["e"], 3)} for w in got], lang


def transcribe_full(
    source: Path,
    work_dir: Path,
    state,
    islands: list,
    dur: float,
    model_name: str = "auto",
    device: str = "auto",
    language: str = None,
    chunk_s: float = CHUNK_S,
    slack_s: float = CHUNK_SLACK_S,
    transcribe_chunk=None,
) -> tuple:
    """
    Whole-stream transcription in ~10 min chunks cut at silences. Each finished chunk is saved to
    <work>/asr/chunk_NNNN.json (words in source seconds, seconds taken, x-realtime) and marked in the
    run state, so an interrupted run resumes at the next chunk. transcribe_chunk(a, b) -> (words, lang)
    can be injected (tests). Returns (words, language, summary).
    """
    lang_arg = None if not language or language == "auto" else language
    asr_dir = Path(work_dir) / "asr"
    asr_dir.mkdir(parents=True, exist_ok=True)
    bounds = chunk_bounds(dur, islands, chunk_s, slack_s)
    n = len(bounds)
    chunks = [None] * n
    for i, (a, b) in enumerate(bounds):
        p = asr_dir / f"chunk_{i:04d}.json"
        if state is not None and state.chunk_done(i) and p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if abs(data.get("start", -1) - a) < 0.01 and abs(data.get("end", -1) - b) < 0.01:
                chunks[i] = data
    done = sum(1 for c in chunks if c is not None)
    if done:
        print(f"Resuming: {done}/{n} chunks done")
    pending = [i for i in range(n) if chunks[i] is None]
    tr = transcribe_chunk
    if pending and tr is None:
        tr = _ChunkTranscriber(source, asr_dir, device, model_name, lang_arg, dur)
        tr.load()
        if tr.w.device == "cpu" and dur > CPU_LONG_AFTER_S:
            print(
                "Tip: CPU transcription of a long stream is slow. A CUDA GPU (pip install -e .[gpu]), "
                "--asr regions or --asr none is faster."
            )
    print(f"Transcribing {_mmss(dur)} of audio in {n} chunk(s) ({len(pending)} to do) ...")
    run_audio = run_secs = 0.0
    for i in pending:
        a, b = bounds[i]
        t0 = time.time()
        words, lang = tr(a, b)
        secs = max(1e-6, time.time() - t0)
        xrt = (b - a) / secs
        label = getattr(tr, "label", "custom")
        data = {
            "index": i,
            "start": a,
            "end": b,
            "language": lang,
            "words": words,
            "seconds": round(secs, 2),
            "x_realtime": round(xrt, 1),
            "engine": label,
        }
        write_json_atomic(asr_dir / f"chunk_{i:04d}.json", data)
        if state is not None:
            state.mark_chunk(i)
        chunks[i] = data
        run_audio += b - a
        run_secs += secs
        print(
            f"  ASR chunk {i + 1}/{n} {_mmss(a)}-{_mmss(b)}: {len(words)} words, {secs:.1f}s "
            f"({xrt:.1f}x realtime, {label})"
        )
        if run_audio == b - a and len(pending) > 1:
            left = sum(bounds[j][1] - bounds[j][0] for j in pending[1:])
            print(f"  ETA for the remaining {len(pending) - 1} chunk(s): about {_mmss(left / xrt)}")
    words = [w for c in chunks for w in c["words"]]
    words.sort(key=lambda w: w["s"])
    langs = [c["language"] for c in chunks if c.get("language")]
    lang = Counter(langs).most_common(1)[0][0] if langs else (lang_arg or "auto")
    if not run_secs:  # everything came from the resume state
        run_audio = sum(c["end"] - c["start"] for c in chunks)
        run_secs = sum(c.get("seconds", 0.0) for c in chunks) or 1e-6
    total_xrt = run_audio / run_secs
    engine = getattr(tr, "label", None) if tr is not None else (chunks[-1].get("engine") if chunks else "-")
    print(
        f"ASR total: {_mmss(run_audio)} of audio in {run_secs:.1f}s = {total_xrt:.1f}x realtime ({engine}); "
        f"{len(words)} words, language {lang}"
    )
    summary = {
        "chunks": n,
        "resumed_chunks": done,
        "audio_seconds": round(run_audio, 1),
        "seconds": round(run_secs, 1),
        "x_realtime": round(total_xrt, 1),
        "engine": engine,
    }
    return words, lang, summary


# ---------------------------------------------------------------- caption rescue pass on a clip window

RESCUE_MIN_PROB = 0.5  # rescued words below this Whisper probability are dropped (noise, game sound)
RESCUE_MAX_WORD_S = 1.5  # without VAD Whisper sometimes stretches a word over seconds of noise
_RESCUE_MODELS = {}


def merge_rescued(words: list, heard: list, min_prob: float = RESCUE_MIN_PROB, pad: float = 0.05) -> tuple:
    """
    words + the words only the no-VAD pass heard: a heard word joins when its probability is >= min_prob, it
    lasts <= 1.5 s, its midpoint is not inside a known word (+-pad) and known words cover < half of it.
    Returns (merged words sorted by start, number added).
    """
    out, added = [dict(w) for w in words], 0
    for h in heard:
        if float(h.get("p", 1.0)) < min_prob or not str(h.get("w", "")).strip():
            continue
        hs, he = float(h["s"]), float(h["e"])
        if he - hs > RESCUE_MAX_WORD_S:
            continue
        mid = (hs + he) / 2
        if any(float(x["s"]) - pad <= mid <= float(x["e"]) + pad for x in out):
            continue
        overlap = sum(max(0.0, min(he, float(x["e"])) - max(hs, float(x["s"]))) for x in out)
        if overlap > 0.5 * (he - hs):
            continue
        out.append({"w": h["w"], "s": round(float(h["s"]), 3), "e": round(float(h["e"]), 3)})
        added += 1
    out.sort(key=lambda w: w["s"])
    return out, added


# Whisper leaves 'um'/'uh' out of its transcripts unless the prompt itself is disfluent (a known Whisper habit).
FILLER_PROMPT = "Umm, so, uh, I was like, you know, hmm... Uh, okay. Um, erm, yeah."
FILLER_MIN_PROB = 0.3  # fillers are short and quiet: Whisper gives them lower probabilities than words


def filler_pass(source: Path, a: float, b: float, words: list, asr: dict, cache_dir: Path) -> tuple:
    """
    --remove-fillers: one Whisper pass over the clip window [a, b] of `source` with a disfluent prompt, so the
    um/uh the normal pass leaves out are transcribed. Only filler tokens (fillers.FILLER_WORDS) are merged into
    words (source seconds) by merge_rescued (never over a known word). Cached per window. Returns
    (words, number added, seconds taken).
    """
    from streamclipper.fillers import FILLER_WORDS, _norm

    lang = asr.get("language")
    lang = None if not lang or lang == "auto" else lang
    model_name = asr.get("model") or "auto"
    tag = file_tag(source, round(a, 2), round(b, 2), model_name, lang, "fillers1")
    cache = Path(cache_dir) / f"asr_fillers_{tag}.json"
    t0 = time.time()
    if cache.is_file():
        heard = json.loads(cache.read_text(encoding="utf-8"))["words"]
    else:
        key = (asr.get("device") or "auto", model_name)
        if key not in _RESCUE_MODELS:
            _RESCUE_MODELS[key] = load_model(key[0], float(asr.get("duration") or 0.0), model_name)
        w = _RESCUE_MODELS[key]
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        wav = _extract_wav(Path(source), a, b, Path(cache_dir) / f"asr_fillers_{tag}.wav")
        try:
            segs, _ = w.model.transcribe(
                read_wav16k(wav),
                word_timestamps=True,
                vad_filter=False,
                condition_on_previous_text=False,
                initial_prompt=FILLER_PROMPT
                + (" " + HINDI_SCRIPT_ANCHOR if lang == "hi" else ""),
                language=lang,
            )
            heard = [
                {
                    "w": x.word.strip(),
                    "s": round(a + x.start, 3),
                    "e": round(a + x.end, 3),
                    "p": round(x.probability, 3),
                }
                for s in segs
                for x in s.words or []
                if _norm(x.word) in FILLER_WORDS
            ]
        finally:
            wav.unlink(missing_ok=True)
        write_json_atomic(cache, {"words": heard})
    merged, added = merge_rescued(words, heard, min_prob=FILLER_MIN_PROB)
    return merged, added, round(time.time() - t0, 2)


def rescue_words(source: Path, a: float, b: float, words: list, asr: dict, cache_dir: Path) -> tuple:
    """
    Second pass over one clip window [a, b] (seconds in `source`): Whisper without its VAD filter, which drops
    shouted or overlapped speech under game sound (measured: 0 of 13 words of a shouted line kept). Words
    only this pass hears are merged into `words` (source seconds) by merge_rescued. asr: {model, device,
    language, duration}. Cached per window in cache_dir. Returns (words, number added, seconds taken).
    """
    lang = asr.get("language")
    lang = None if not lang or lang == "auto" else lang
    model_name = asr.get("model") or "auto"
    tag = file_tag(source, round(a, 2), round(b, 2), model_name, lang, "rescue1")
    cache = Path(cache_dir) / f"asr_rescue_{tag}.json"
    t0 = time.time()
    if cache.is_file():
        heard = json.loads(cache.read_text(encoding="utf-8"))["words"]
    else:
        key = (asr.get("device") or "auto", model_name)
        if key not in _RESCUE_MODELS:
            _RESCUE_MODELS[key] = load_model(key[0], float(asr.get("duration") or 0.0), model_name)
        w = _RESCUE_MODELS[key]
        wav = _extract_wav(Path(source), a, b, Path(cache_dir) / f"asr_rescue_{tag}.wav")
        try:
            segs, _ = w.model.transcribe(
                read_wav16k(wav),
                word_timestamps=True,
                vad_filter=False,
                condition_on_previous_text=False,
                initial_prompt=script_anchor(lang),
                language=lang,
            )
            heard = [
                {
                    "w": x.word.strip(),
                    "s": round(a + x.start, 3),
                    "e": round(a + x.end, 3),
                    "p": round(x.probability, 3),
                }
                for s in segs
                for x in s.words or []
                if x.word.strip()
            ]
        finally:
            wav.unlink(missing_ok=True)
        write_json_atomic(cache, {"words": heard})
    merged, added = merge_rescued(words, heard)
    return merged, added, round(time.time() - t0, 2)
