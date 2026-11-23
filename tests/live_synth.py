"""Synthetic live material for the live tests: 10 ms envelopes and words with planted reactions."""

import numpy as np

WORDS = "so we go in here and then we try the door and look what is behind it right now".split()


def synth(total_s: float, events: list, seed: int = 1) -> tuple:
    """
    (full, vox, words): talk at about -30 dB in 2.2 s phrases with 0.5 s pauses, words every 0.3 s and a
    sentence end per phrase; at each event t a 1.6 s scream at -4 dB, then 1.2 s of silence, then talk.
    """
    rng = np.random.default_rng(seed)
    n = int(round(total_s * 100))
    vox = np.full(n, -70.0, dtype=np.float32)
    words = []
    t, k = 0.5, 0
    while t < total_s:
        ev = next((e for e in events if t < e + 2.8 and e < t + 2.2), None)
        if ev is not None and t >= ev:
            t = ev + 2.8
            continue
        end = min(t + 2.2, total_s, ev if ev is not None else total_s)
        a, b = int(t * 100), int(end * 100)
        vox[a:b] = -30.0 + rng.normal(0, 1.5, b - a)
        s = t
        while s + 0.25 <= end:
            k += 1
            txt = WORDS[k % len(WORDS)]
            words.append({"w": txt, "s": round(s, 3), "e": round(s + 0.25, 3)})
            s += 0.3
        if words:
            words[-1]["w"] += "."
        t = end + 0.5
    for e in events:
        a, b = int(e * 100), int((e + 1.6) * 100)
        vox[a:b] = -4.0 + rng.normal(0, 0.8, b - a)
        vox[b : int((e + 2.8) * 100)] = -70.0
        words.append({"w": "NO", "s": e + 0.1, "e": e + 0.5})
        words.append({"w": "WAY!", "s": e + 0.6, "e": e + 1.4})
    words.sort(key=lambda w: w["s"])
    full = vox + 2.0
    return full.astype(np.float32), vox, words
