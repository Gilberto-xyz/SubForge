#!/usr/bin/env python3
"""Backward-compatible entrypoint. Use convertir_mp4_a_mkv.py instead."""

from pathlib import Path
import runpy


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("convertir_mp4_a_mkv.py")), run_name="__main__")
