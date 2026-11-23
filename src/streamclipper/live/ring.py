"""Ring buffer of live segments on disk: index from ffmpeg's segment lists, window export, safe deletion."""

import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from streamclipper.util import ToolError, run_cmd

SEG_RE = re.compile(r"^seg_\d{6}\.ts$")


@dataclass
class Segment:
    number: int
    path: Path
    start: float  # stream time (seconds of media buffered since the run started)
    dur: float
    wall: float  # wall-clock time the segment was seen complete
    deleted: bool = False

    @property
    def end(self) -> float:
        return self.start + self.dur


class Ring:
    """
    Segment index of one live run. Segments are listed by ffmpeg (-segment_list csv); stream time is
    contiguous media time (a reconnect gap is not counted). Only seg_NNNNNN.ts files that this run's own
    segment lists named, inside its own buffer folder, are ever deleted.
    """

    def __init__(self, buffer_dir: Path, keep_s: float = 900.0):
        self.dir = Path(buffer_dir).resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keep_s = float(keep_s)
        self.segments = []
        self._own = set()
        self._lists = []  # [path, characters already read]
        self._lock = threading.Lock()
        self.deleted = 0

    def watch(self, csv_path: Path) -> None:
        """Read this segment list (one per ingest attempt) on every poll()."""
        with self._lock:
            self._lists.append([Path(csv_path), 0])

    @property
    def end(self) -> float:
        return self.segments[-1].end if self.segments else 0.0

    def next_number(self) -> int:
        return self.segments[-1].number + 1 if self.segments else 0

    def last_wall(self):
        return self.segments[-1].wall if self.segments else None

    def poll(self, now: float = None) -> list:
        """New complete segments from the watched lists, in order (stream times assigned here)."""
        now = time.time() if now is None else now
        new = []
        with self._lock:
            for entry in self._lists:
                path, pos = entry
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                chunk = text[pos:]
                done = chunk.rfind("\n") + 1  # only complete lines; ffmpeg may be mid-write
                if done <= 0:
                    continue
                entry[1] = pos + done
                for line in chunk[:done].splitlines():
                    seg = self._parse(line, now)
                    if seg is not None:
                        self.segments.append(seg)
                        self._own.add(seg.path)
                        new.append(seg)
        return new

    def _parse(self, line: str, now: float):
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 3:
            return None
        name = Path(parts[0]).name
        if not SEG_RE.match(name):
            return None
        try:
            dur = float(parts[2]) - float(parts[1])
        except ValueError:
            return None
        path = (self.dir / name).resolve()
        if dur <= 0 or not path.is_file():
            return None
        return Segment(int(name[4:10]), path, self.end, dur, now)

    def owns(self, path: Path) -> bool:
        p = Path(path).resolve()
        return p.parent == self.dir and bool(SEG_RE.match(p.name)) and p in self._own

    def prune(self, keep_s: float = None) -> int:
        """Delete own segments that ended more than keep_s before the buffered end. Returns how many."""
        cutoff = self.end - (self.keep_s if keep_s is None else keep_s)
        n = 0
        for seg in self.segments:
            if seg.deleted or seg.end > cutoff:
                continue
            if self.owns(seg.path):
                try:
                    seg.path.unlink()
                except FileNotFoundError:
                    pass
                seg.deleted = True
                n += 1
        self.deleted += n
        return n

    def select(self, a: float, b: float) -> list:
        """Segments that overlap [a, b], in order. Raises when one of them was already deleted."""
        segs = [s for s in self.segments if s.end > a and s.start < b]
        if any(s.deleted for s in segs):
            raise ToolError(
                f"Part of {a:.1f}-{b:.1f}s has already left the ring buffer.",
                "use a larger --buffer-min",
            )
        return segs

    def segment_at(self, t: float):
        return next((s for s in self.segments if s.start <= t < s.end), self.segments[-1] if self.segments else None)

    def wall_to_stream(self, wall: float) -> float:
        """Stream time being written at wall-clock time `wall` (for live chat timestamps)."""
        last = self.segments[-1] if self.segments else None
        if last is None:
            return 0.0
        return last.end + max(0.0, min(wall - last.wall, 30.0))

    def export_window(self, a: float, b: float, out_mp4: Path) -> tuple:
        """
        Stream time [a, b] -> one MP4 (concat demuxer, -c copy, +faststart) made from whole segments, so it
        survives the ring deletion. Returns (path, t0, t1): stream times of the file's first and last frame.
        """
        segs = self.select(a, b)
        if not segs:
            raise ToolError(f"No buffered segment covers {a:.1f}-{b:.1f}s.", "the moment is outside the buffer")
        out_mp4 = Path(out_mp4)
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        lst = out_mp4.with_suffix(".txt")
        lst.write_text(
            "".join("file '" + s.path.as_posix().replace("'", "'\\''") + "'\n" for s in segs), encoding="utf-8"
        )
        tmp = out_mp4.with_name(out_mp4.stem + ".part.mp4")
        run_cmd(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(lst),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(tmp),
            ],
            "live window export",
        )
        os.replace(tmp, out_mp4)
        lst.unlink(missing_ok=True)
        return out_mp4, segs[0].start, segs[-1].end
