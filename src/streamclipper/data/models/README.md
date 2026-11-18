# Bundled models

## blaze_face_full_range.tflite

- What: MediaPipe BlazeFace (full range) face detector, float16, for the MediaPipe Tasks `FaceDetector`.
  StreamClipper runs it on the whole frame plus a 3x2 grid of overlapping tiles (`streamclipper.faces`), so
  small faces (a facecam in a corner, a wide podcast shot) are found too.
- Why full range: on a corner facecam (face ~8% of the frame height) it found the face on 14 of 20 test
  frames with tiles; the short-range model found it on 0 of 20.
- Source: https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_full_range/float16/1/blaze_face_full_range.tflite
- SHA-256: `3698b18f063835bc609069ef052228fbe86d9c9a6dc8dcb7c7c2d69aed2b181b`
- Size: 1,083,786 bytes
- Licence: Apache License 2.0 (Google MediaPipe models). Apache-2.0 is compatible with StreamClipper's
  AGPL-3.0-or-later.
- Used by: `streamclipper.faces.MediaPipeFaces` (needs the `vision` extra: `pip install -e ".[vision]"`).
  Without mediapipe, the OpenCV Haar cascades are used (`vision-haar` extra), and without either the
  layouts fall back to the skin-colour heuristic.

## en_dict.txt (bundled) and the OCR model (downloaded on first use)
- What: the character list (95 characters) of PaddleOCR's English PP-OCRv4 text recognition model. The model
  itself (`en_PP-OCRv4_rec_mobile.onnx`, ONNX export by RapidOCR) is not bundled: `streamclipper.hud.ocr`
  downloads it once into the user folder (`%LOCALAPPDATA%\streamclipper\models` or `~/.cache/streamclipper/models`),
  checks the SHA-256 and writes it atomically.
- Model source (pinned to RapidOCR release v3.10.0):
  https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv4/rec/en_PP-OCRv4_rec_mobile.onnx
- Model SHA-256: `e8770c967605983d1570cdf5352041dfb68fa0c21664f49f47b155abd3e0e318`, 7,653,044 bytes.
- Dictionary source: https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/paddle/PP-OCRv4/rec/en_PP-OCRv4_rec_mobile/en_dict.txt,
  SHA-256 `5662df9d2d03f0e8ca0d3b0649d6acbab904b6a14b3d3521463c71c37c668ce3`.
- Licence: Apache License 2.0 (PaddleOCR models and dictionaries; RapidOCR's ONNX exports keep it). Compatible
  with StreamClipper's AGPL-3.0-or-later.
- Used by: game HUD detection (`--hud`, `calibrate-hud`), through onnxruntime (`vision` extra). Without
  onnxruntime or the model, HUD detection uses colour cues only.

## bd.rnnn (speech enhancement, `--enhance-speech`)

- Model: RNNoise-nu `beguiling-drafter` (signal: voice incl. laughter, noise: recording noise), used by ffmpeg's `arnndn` filter.
- Source: https://github.com/GregorR/rnnoise-models/blob/master/beguiling-drafter-2018-08-30/bd.rnnn
- SHA-256: `ae3f7411e1e6a884f839a4a145c394408398f09854dbc1216ee02faafc98a17b` (299,693 bytes)
- Licence: the rnnoise-models repository states that, apart from its tools, none of the work is creative and so none of it is subject to copyright. RNNoise itself is BSD-3-Clause.