"""
TikTok publish pack: TikTok's Content Posting API needs an app review, so StreamClipper does not upload there.
A pack is a folder <out>/publish_pack/<clip>/ with the MP4, caption.txt, hashtags.txt and best_time.txt,
ready to post by hand from the TikTok app or website (or any other platform).
"""

import re
import shutil
from pathlib import Path

from streamclipper.publish.meta import tiktok_caption
from streamclipper.publish.schedule import best_time_text

PACK_DIR = "publish_pack"
FILES = ("caption.txt", "hashtags.txt", "best_time.txt")


def make_pack(mp4, base: dict, out_dir, name: str = "", planned=None, now=None) -> Path:
    mp4 = Path(mp4)
    name = re.sub(r"[^\w\-]+", "_", name or mp4.stem).strip("_") or "clip"
    folder = Path(out_dir) / PACK_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / f"{name}.mp4"
    if mp4.resolve() != dst.resolve():
        shutil.copy2(mp4, dst)
    (folder / "caption.txt").write_text(tiktok_caption(base) + "\n", encoding="utf-8")
    (folder / "hashtags.txt").write_text(" ".join(base.get("hashtags") or []) + "\n", encoding="utf-8")
    (folder / "best_time.txt").write_text(best_time_text(now, planned), encoding="utf-8")
    return folder
