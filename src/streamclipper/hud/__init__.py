"""Game HUD event detection: on-screen events (WASTED, BUSTED, mission passed, wanted stars) as a clip signal.

profiles.py  game profiles (data/hud/*.json or a user JSON)
frames.py    low-res frame source (keyframe pass over the stream, 3 fps dense pass per window)
detect.py    colour/shape cues and event onsets (numpy only)
ocr.py       PaddleOCR English recognition model through onnxruntime (no OCR package)
analyze.py   orchestration for discover(): game detection, events, face signal
calibrate.py calibrate-hud: find text regions and words for a new game (GTA 6 after its release)
"""
