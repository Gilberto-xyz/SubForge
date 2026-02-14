#!/usr/bin/env python3
"""Backward-compatible entrypoint. Use extraer_subtitulos.py instead."""

from pathlib import Path
import runpy


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("extraer_subtitulos.py")), run_name="__main__")
