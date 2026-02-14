#!/usr/bin/env python3
"""Backward-compatible entrypoint. Use limpiar_tracks.py instead."""

from pathlib import Path
import runpy


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("limpiar_tracks.py")), run_name="__main__")
