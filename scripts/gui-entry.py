"""PyInstaller entry point for the MkPFS GUI.

This file is used by PyInstaller to produce a single-file executable.
Run it locally with:

    uv run pyinstaller --onefile --windowed \
        --hidden-import tkinter \
        --collect-data tkinter \
        --collect-binaries tkinter \
        --collect-data customtkinter \
        scripts/gui-entry.py
"""

from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--smoke-test" in sys.argv:
        print("MkPFS GUI smoke test passed")
        raise SystemExit(0)
    from mkpfs.gui import run_gui

    raise SystemExit(run_gui())
