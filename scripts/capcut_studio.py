#!/usr/bin/env python3
"""Compatibility entry point: `python scripts/capcut_studio.py <command>` runs streamclipper.
Works with or without `pip install -e .` (falls back to the src/ folder next to this script).
All code lives in the streamclipper package (src/streamclipper)."""

import sys
from pathlib import Path

try:
    from streamclipper.cli import main
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from streamclipper.cli import main

if __name__ == "__main__":
    main()
