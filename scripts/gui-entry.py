"""PyInstaller entry point for the MkPFS GUI.

This file is used by PyInstaller to produce a single-file executable.
Run it locally with:

    uv run pyinstaller --onefile --windowed \
        --collect-data customtkinter scripts/gui-entry.py
"""

from __future__ import annotations

import sys

from mkpfs.gui import run_gui

if __name__ == "__main__":
    raise SystemExit(run_gui())
