"""Graphical user interface for MkPFS.

This module provides a cross-platform desktop GUI built with customtkinter.
It wraps the CLI workflows so users can pack, verify, inspect, unpack, and
browse PFS images without typing commands.
"""

from __future__ import annotations

import argparse
import io
import queue
import re
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from tkinter import filedialog, messagebox

# CustomTkinter is optional so the CLI stays lightweight.
try:
    import customtkinter as ctk
except ImportError as exc:
    raise ImportError("mkpfs GUI requires 'customtkinter'. Install it with: uv add customtkinter") from exc

from mkpfs.cli import (
    cli_mkpfs_add_create_args,
    cli_mkpfs_check_run,
    cli_mkpfs_create_run,
    cli_mkpfs_extract_run,
    cli_mkpfs_inspect_run,
    cli_mkpfs_ls_run,
    cli_mkpfs_pack_archive_run,
    cli_mkpfs_pack_file_run,
)

PROGRESS_LINE_RE: re.Pattern[str] = re.compile(r"\[(?P<bar>[#-]{4,})\]\s*(?P<pct>\d{1,3})%\s*(?P<label>[^\r\n]*)")


class GuiLogRedirect(io.TextIOBase):
    """Thread-safe redirector that feeds a Tkinter text widget via a queue.

    Inherits io.TextIOBase (not io.StringIO) so no internal StringIO buffer
    is allocated. The stream is write-only: write() enqueues text directly
    without buffering it here.
    """

    def __init__(self, app: MkPFSApp, tag: str = "info") -> None:
        super().__init__()  # Required: initialises C-level io.TextIOBase state.
        self._app: MkPFSApp = app
        self._tag: str = tag

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self._app.enqueue_log(text, self._tag)
        return len(text)

    def flush(self) -> None:
        pass


class MkPFSApp:
    """Main MkPFS GUI application.

    Attributes:
        root: CustomTkinter root window.
        log_queue: Thread-safe queue for log messages from worker threads.
        worker_thread: Currently running background thread, if any.
    """

    # Maximum items held in each inter-thread queue. Capping these prevents
    # a fast-writing worker from growing queues without bound when the 100 ms
    # poller cannot keep up (e.g. a very verbose pack run on a slow machine).
    _QUEUE_MAXSIZE: int = 10_000

    def __init__(self, root: ctk.CTk) -> None:
        self.root: ctk.CTk = root
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=self._QUEUE_MAXSIZE)
        self.progress_queue: queue.Queue[tuple[float, str]] = queue.Queue(maxsize=self._QUEUE_MAXSIZE)
        self.completion_queue: queue.Queue[None] = queue.Queue(maxsize=self._QUEUE_MAXSIZE)
        self.worker_thread: threading.Thread | None = None
        self.action_buttons: list[ctk.CTkButton] = []
        self.is_closing: bool = False
        self.log_after_id: str | None = None
        self.progress_after_id: str | None = None
        self.completion_after_id: str | None = None

        self._setup_window()
        self._build_ui()
        self._start_log_polling()
        self._start_progress_polling()
        self._start_completion_polling()

    def _setup_window(self) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        self.root.title("MkPFS - PlayStation File System Builder")
        self.root.geometry("1200x900")
        self.root.minsize(1000, 750)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=0)
        self.root.rowconfigure(1, weight=3)
        self.root.rowconfigure(2, weight=0)
        self.root.rowconfigure(3, weight=1)

        self._build_header()
        self._build_tabs()
        self._build_progress_bar()
        self._build_log_console()

    def _build_header(self) -> None:
        header: ctk.CTkFrame = ctk.CTkFrame(self.root)
        header.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")
        ctk.CTkLabel(header, text="MkPFS", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left", padx=10, pady=8)
        ctk.CTkLabel(
            header,
            text="Create and manage PS4 / PS5 PFS disc images",
            font=ctk.CTkFont(size=13),
        ).pack(side="left", padx=(0, 10), pady=8)

    def _build_tabs(self) -> None:
        self.notebook: ctk.CTkTabview = ctk.CTkTabview(self.root)
        self.notebook.grid(row=1, column=0, padx=12, pady=6, sticky="nsew")

        self.notebook.add("Pack Folder")
        self.notebook.add("Pack Archive")
        self.notebook.add("Pack File")
        self.notebook.add("Verify")
        self.notebook.add("Inspect")
        self.notebook.add("Unpack")
        self.notebook.add("Tree")

        self._build_pack_folder_tab(self.notebook.tab("Pack Folder"))
        self._build_pack_archive_tab(self.notebook.tab("Pack Archive"))
        self._build_pack_file_tab(self.notebook.tab("Pack File"))
        self._build_verify_tab(self.notebook.tab("Verify"))
        self._build_inspect_tab(self.notebook.tab("Inspect"))
        self._build_unpack_tab(self.notebook.tab("Unpack"))
        self._build_tree_tab(self.notebook.tab("Tree"))

    def _build_pack_folder_tab(self, parent: ctk.CTkFrame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        parent.rowconfigure(1, weight=0)

        container: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(parent)
        container.grid(row=0, column=0, padx=8, pady=(8, 4), sticky="nsew")
        container.columnconfigure(1, weight=1)

        self.pack_folder_source_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(
            container, "Source folder", self.pack_folder_source_var, "Browse", self._pick_folder, 0
        )

        self.pack_folder_output_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Output image", self.pack_folder_output_var, "Save As", self._save_file, 1)

        self.pack_version_var: tk.StringVar = tk.StringVar(value="PS5")
        self.pack_inode_var: tk.StringVar = tk.StringVar(value="32")
        self.pack_block_var: tk.StringVar = tk.StringVar(value="65536")
        self.pack_compression_level_var: tk.StringVar = tk.StringVar(value="9")
        self.pack_compress_var: tk.BooleanVar = tk.BooleanVar(value=True)
        self.pack_skip_exe_var: tk.BooleanVar = tk.BooleanVar(value=True)
        self.pack_signed_var: tk.BooleanVar = tk.BooleanVar(value=False)
        self.pack_encrypted_var: tk.BooleanVar = tk.BooleanVar(value=False)
        self.pack_case_var: tk.BooleanVar = tk.BooleanVar(value=True)
        self.pack_verify_var: tk.BooleanVar = tk.BooleanVar(value=True)
        self.pack_require_game_var: tk.BooleanVar = tk.BooleanVar(value=False)
        self.pack_min_size_var: tk.StringVar = tk.StringVar(value="65536")
        self.pack_max_ratio_var: tk.StringVar = tk.StringVar(value="95")
        self.pack_ekpfs_var: tk.StringVar = tk.StringVar()
        self.pack_show_advanced_var: tk.BooleanVar = tk.BooleanVar(value=False)

        basic_frame: ctk.CTkFrame = ctk.CTkFrame(container)
        basic_frame.grid(row=2, column=0, columnspan=3, padx=4, pady=(12, 4), sticky="ew")
        basic_frame.columnconfigure(3, weight=1)

        ctk.CTkLabel(basic_frame, text="Package type:").grid(row=0, column=0, padx=8, pady=(8, 4), sticky="e")
        ctk.CTkSegmentedButton(
            basic_frame,
            values=["PS5", "PS4"],
            variable=self.pack_version_var,
            width=180,
        ).grid(row=0, column=1, padx=4, pady=(8, 4), sticky="w")

        ctk.CTkCheckBox(
            basic_frame,
            text="Advanced",
            variable=self.pack_show_advanced_var,
            command=self._toggle_pack_advanced_options,
        ).grid(row=0, column=2, padx=8, pady=(8, 4), sticky="w")

        ctk.CTkCheckBox(basic_frame, text="Compress image", variable=self.pack_compress_var).grid(
            row=1, column=1, padx=4, pady=6, sticky="w"
        )
        ctk.CTkCheckBox(basic_frame, text="Verify after pack", variable=self.pack_verify_var).grid(
            row=1, column=2, padx=8, pady=6, sticky="w"
        )

        self.pack_advanced_frame: ctk.CTkFrame = ctk.CTkFrame(container)
        self.pack_advanced_frame.grid(row=3, column=0, columnspan=3, padx=4, pady=(8, 4), sticky="ew")
        self.pack_advanced_frame.grid_remove()
        ctk.CTkLabel(self.pack_advanced_frame, text="Advanced Options", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=4, padx=8, pady=(8, 4), sticky="w"
        )

        ctk.CTkLabel(self.pack_advanced_frame, text="Inode bits:").grid(row=1, column=0, padx=8, pady=4, sticky="e")
        ctk.CTkOptionMenu(self.pack_advanced_frame, values=["32", "64"], variable=self.pack_inode_var, width=100).grid(
            row=1, column=1, padx=4, pady=4, sticky="w"
        )

        ctk.CTkLabel(self.pack_advanced_frame, text="Block size:").grid(row=1, column=2, padx=8, pady=4, sticky="e")
        ctk.CTkOptionMenu(
            self.pack_advanced_frame,
            values=["auto", "4096", "8192", "16384", "32768", "65536", "auto-fit"],
            variable=self.pack_block_var,
            width=100,
        ).grid(row=1, column=3, padx=4, pady=4, sticky="w")

        ctk.CTkLabel(self.pack_advanced_frame, text="Zlib level:").grid(row=2, column=0, padx=8, pady=4, sticky="e")
        ctk.CTkOptionMenu(
            self.pack_advanced_frame,
            values=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"],
            variable=self.pack_compression_level_var,
            width=80,
        ).grid(row=2, column=1, padx=4, pady=4, sticky="w")

        ctk.CTkCheckBox(
            self.pack_advanced_frame, text="Skip executable compression", variable=self.pack_skip_exe_var
        ).grid(row=2, column=2, columnspan=2, padx=8, pady=4, sticky="w")

        ctk.CTkCheckBox(self.pack_advanced_frame, text="Signed image", variable=self.pack_signed_var).grid(
            row=3, column=0, columnspan=2, padx=8, pady=4, sticky="w"
        )

        ctk.CTkCheckBox(self.pack_advanced_frame, text="Encrypted", variable=self.pack_encrypted_var).grid(
            row=3, column=2, columnspan=2, padx=8, pady=4, sticky="w"
        )

        ctk.CTkCheckBox(self.pack_advanced_frame, text="Case insensitive", variable=self.pack_case_var).grid(
            row=4, column=0, columnspan=2, padx=8, pady=4, sticky="w"
        )

        ctk.CTkCheckBox(
            self.pack_advanced_frame,
            text="Require game files (param.json + eboot.bin)",
            variable=self.pack_require_game_var,
        ).grid(row=4, column=2, columnspan=2, padx=8, pady=4, sticky="w")

        ctk.CTkLabel(self.pack_advanced_frame, text="Min compress size:").grid(
            row=5, column=0, padx=8, pady=4, sticky="e"
        )
        ctk.CTkEntry(self.pack_advanced_frame, textvariable=self.pack_min_size_var, width=100).grid(
            row=5, column=1, padx=4, pady=4, sticky="w"
        )

        ctk.CTkLabel(self.pack_advanced_frame, text="Max compressed ratio (%):").grid(
            row=5, column=2, padx=8, pady=4, sticky="e"
        )
        ctk.CTkEntry(self.pack_advanced_frame, textvariable=self.pack_max_ratio_var, width=100).grid(
            row=5, column=3, padx=4, pady=4, sticky="w"
        )

        ctk.CTkLabel(self.pack_advanced_frame, text="EKPFS key (hex, optional):").grid(
            row=6, column=0, padx=8, pady=4, sticky="e"
        )
        ctk.CTkEntry(
            self.pack_advanced_frame,
            textvariable=self.pack_ekpfs_var,
            width=420,
            placeholder_text="64 hex chars or leave blank",
        ).grid(row=6, column=1, columnspan=3, padx=4, pady=4, sticky="w")

        action_frame: ctk.CTkFrame = ctk.CTkFrame(parent)
        action_frame.grid(row=1, column=0, padx=8, pady=(4, 8), sticky="ew")
        action_frame.columnconfigure(0, weight=1)

        pack_folder_button: ctk.CTkButton = ctk.CTkButton(
            action_frame,
            text="Pack Folder",
            command=self._on_pack_folder,
            font=ctk.CTkFont(weight="bold"),
            width=180,
            height=36,
        )
        pack_folder_button.grid(row=0, column=0, padx=8, pady=8)
        self._register_action_button(pack_folder_button)

    def _build_pack_file_tab(self, parent: ctk.CTkFrame) -> None:
        container: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(parent)
        container.pack(fill="both", expand=True, padx=8, pady=8)
        container.columnconfigure(1, weight=1)

        self.pack_file_source_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Source file", self.pack_file_source_var, "Browse", self._pick_file, 0)

        self.pack_file_output_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Output image", self.pack_file_output_var, "Save As", self._save_file, 1)

        adv_frame: ctk.CTkFrame = ctk.CTkFrame(container)
        adv_frame.grid(row=2, column=0, columnspan=3, padx=4, pady=(12, 4), sticky="ew")

        self.pack_file_compress_var: tk.BooleanVar = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(adv_frame, text="Enable PFSC compression", variable=self.pack_file_compress_var).pack(
            padx=8, pady=6, anchor="w"
        )

        self.pack_file_verify_var: tk.BooleanVar = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(adv_frame, text="Verify after pack", variable=self.pack_file_verify_var).pack(
            padx=8, pady=6, anchor="w"
        )

        pack_file_button: ctk.CTkButton = ctk.CTkButton(
            container,
            text="Pack File",
            command=self._on_pack_file,
            font=ctk.CTkFont(weight="bold"),
            width=180,
            height=36,
        )
        pack_file_button.grid(row=3, column=0, columnspan=3, padx=8, pady=(16, 8))
        self._register_action_button(pack_file_button)

    def _build_pack_archive_tab(self, parent: ctk.CTkFrame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        parent.rowconfigure(1, weight=0)

        container: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(parent)
        container.grid(row=0, column=0, padx=8, pady=(8, 4), sticky="nsew")
        container.columnconfigure(1, weight=1)

        self.pack_archive_source_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(
            container, "Source archive", self.pack_archive_source_var, "Browse", self._pick_archive_file, 0
        )

        self.pack_archive_password_var: tk.StringVar = tk.StringVar()
        ctk.CTkLabel(container, text="Archive password:").grid(row=1, column=0, padx=8, pady=4, sticky="e")
        ctk.CTkEntry(
            container,
            textvariable=self.pack_archive_password_var,
            show="*",
            placeholder_text="Optional",
        ).grid(row=1, column=1, padx=4, pady=4, sticky="ew")

        self.pack_archive_output_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Output image", self.pack_archive_output_var, "Save As", self._save_file, 2)

        self.pack_archive_version_var: tk.StringVar = tk.StringVar(value="PS5")
        self.pack_archive_inode_var: tk.StringVar = tk.StringVar(value="32")
        self.pack_archive_block_var: tk.StringVar = tk.StringVar(value="65536")
        self.pack_archive_compression_level_var: tk.StringVar = tk.StringVar(value="9")
        self.pack_archive_compress_var: tk.BooleanVar = tk.BooleanVar(value=True)
        self.pack_archive_skip_exe_var: tk.BooleanVar = tk.BooleanVar(value=True)
        self.pack_archive_signed_var: tk.BooleanVar = tk.BooleanVar(value=False)
        self.pack_archive_encrypted_var: tk.BooleanVar = tk.BooleanVar(value=False)
        self.pack_archive_case_var: tk.BooleanVar = tk.BooleanVar(value=True)
        self.pack_archive_verify_var: tk.BooleanVar = tk.BooleanVar(value=True)
        self.pack_archive_require_game_var: tk.BooleanVar = tk.BooleanVar(value=False)
        self.pack_archive_min_size_var: tk.StringVar = tk.StringVar(value="65536")
        self.pack_archive_max_ratio_var: tk.StringVar = tk.StringVar(value="95")
        self.pack_archive_ekpfs_var: tk.StringVar = tk.StringVar()
        self.pack_archive_show_advanced_var: tk.BooleanVar = tk.BooleanVar(value=False)

        basic_frame: ctk.CTkFrame = ctk.CTkFrame(container)
        basic_frame.grid(row=3, column=0, columnspan=3, padx=4, pady=(12, 4), sticky="ew")
        basic_frame.columnconfigure(3, weight=1)

        ctk.CTkLabel(basic_frame, text="Package type:").grid(row=0, column=0, padx=8, pady=(8, 4), sticky="e")
        ctk.CTkSegmentedButton(
            basic_frame,
            values=["PS5", "PS4"],
            variable=self.pack_archive_version_var,
            width=180,
        ).grid(row=0, column=1, padx=4, pady=(8, 4), sticky="w")

        ctk.CTkCheckBox(
            basic_frame,
            text="Advanced",
            variable=self.pack_archive_show_advanced_var,
            command=self._toggle_pack_archive_advanced_options,
        ).grid(row=0, column=2, padx=8, pady=(8, 4), sticky="w")

        ctk.CTkCheckBox(basic_frame, text="Compress image", variable=self.pack_archive_compress_var).grid(
            row=1, column=1, padx=4, pady=6, sticky="w"
        )
        ctk.CTkCheckBox(basic_frame, text="Verify after pack", variable=self.pack_archive_verify_var).grid(
            row=1, column=2, padx=8, pady=6, sticky="w"
        )

        self.pack_archive_advanced_frame: ctk.CTkFrame = ctk.CTkFrame(container)
        self.pack_archive_advanced_frame.grid(row=4, column=0, columnspan=3, padx=4, pady=(8, 4), sticky="ew")
        self.pack_archive_advanced_frame.grid_remove()
        ctk.CTkLabel(
            self.pack_archive_advanced_frame, text="Advanced Options", font=ctk.CTkFont(size=14, weight="bold")
        ).grid(row=0, column=0, columnspan=4, padx=8, pady=(8, 4), sticky="w")

        ctk.CTkLabel(self.pack_archive_advanced_frame, text="Inode bits:").grid(
            row=1, column=0, padx=8, pady=4, sticky="e"
        )
        ctk.CTkOptionMenu(
            self.pack_archive_advanced_frame, values=["32", "64"], variable=self.pack_archive_inode_var, width=100
        ).grid(row=1, column=1, padx=4, pady=4, sticky="w")

        ctk.CTkLabel(self.pack_archive_advanced_frame, text="Block size:").grid(
            row=1, column=2, padx=8, pady=4, sticky="e"
        )
        ctk.CTkOptionMenu(
            self.pack_archive_advanced_frame,
            values=["auto", "4096", "8192", "16384", "32768", "65536", "auto-fit"],
            variable=self.pack_archive_block_var,
            width=100,
        ).grid(row=1, column=3, padx=4, pady=4, sticky="w")

        ctk.CTkLabel(self.pack_archive_advanced_frame, text="Zlib level:").grid(
            row=2, column=0, padx=8, pady=4, sticky="e"
        )
        ctk.CTkOptionMenu(
            self.pack_archive_advanced_frame,
            values=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"],
            variable=self.pack_archive_compression_level_var,
            width=80,
        ).grid(row=2, column=1, padx=4, pady=4, sticky="w")

        ctk.CTkCheckBox(
            self.pack_archive_advanced_frame,
            text="Skip executable compression",
            variable=self.pack_archive_skip_exe_var,
        ).grid(row=2, column=2, columnspan=2, padx=8, pady=4, sticky="w")

        ctk.CTkCheckBox(
            self.pack_archive_advanced_frame, text="Signed image", variable=self.pack_archive_signed_var
        ).grid(row=3, column=0, columnspan=2, padx=8, pady=4, sticky="w")

        ctk.CTkCheckBox(
            self.pack_archive_advanced_frame, text="Encrypted", variable=self.pack_archive_encrypted_var
        ).grid(row=3, column=2, columnspan=2, padx=8, pady=4, sticky="w")

        ctk.CTkCheckBox(
            self.pack_archive_advanced_frame, text="Case insensitive", variable=self.pack_archive_case_var
        ).grid(row=4, column=0, columnspan=2, padx=8, pady=4, sticky="w")

        ctk.CTkCheckBox(
            self.pack_archive_advanced_frame,
            text="Require game files (param.json + eboot.bin)",
            variable=self.pack_archive_require_game_var,
        ).grid(row=4, column=2, columnspan=2, padx=8, pady=4, sticky="w")

        ctk.CTkLabel(self.pack_archive_advanced_frame, text="Min compress size:").grid(
            row=5, column=0, padx=8, pady=4, sticky="e"
        )
        ctk.CTkEntry(self.pack_archive_advanced_frame, textvariable=self.pack_archive_min_size_var, width=100).grid(
            row=5, column=1, padx=4, pady=4, sticky="w"
        )

        ctk.CTkLabel(self.pack_archive_advanced_frame, text="Max compressed ratio (%):").grid(
            row=5, column=2, padx=8, pady=4, sticky="e"
        )
        ctk.CTkEntry(self.pack_archive_advanced_frame, textvariable=self.pack_archive_max_ratio_var, width=100).grid(
            row=5, column=3, padx=4, pady=4, sticky="w"
        )

        ctk.CTkLabel(self.pack_archive_advanced_frame, text="EKPFS key (hex, optional):").grid(
            row=6, column=0, padx=8, pady=4, sticky="e"
        )
        ctk.CTkEntry(
            self.pack_archive_advanced_frame,
            textvariable=self.pack_archive_ekpfs_var,
            width=420,
            placeholder_text="64 hex chars or leave blank",
        ).grid(row=6, column=1, columnspan=3, padx=4, pady=4, sticky="w")

        action_frame: ctk.CTkFrame = ctk.CTkFrame(parent)
        action_frame.grid(row=1, column=0, padx=8, pady=(4, 8), sticky="ew")
        action_frame.columnconfigure(0, weight=1)

        pack_archive_button: ctk.CTkButton = ctk.CTkButton(
            action_frame,
            text="Pack Archive",
            command=self._on_pack_archive,
            font=ctk.CTkFont(weight="bold"),
            width=180,
            height=36,
        )
        pack_archive_button.grid(row=0, column=0, padx=8, pady=8)
        self._register_action_button(pack_archive_button)

    def _build_verify_tab(self, parent: ctk.CTkFrame) -> None:
        container: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(parent)
        container.pack(fill="both", expand=True, padx=8, pady=8)
        container.columnconfigure(1, weight=1)

        self.verify_image_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Image file", self.verify_image_var, "Browse", self._pick_file, 0)

        self.verify_source_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(
            container, "Source folder (optional)", self.verify_source_var, "Browse", self._pick_folder, 1
        )

        verify_button: ctk.CTkButton = ctk.CTkButton(
            container,
            text="Verify Image",
            command=self._on_verify,
            font=ctk.CTkFont(weight="bold"),
            width=180,
            height=36,
        )
        verify_button.grid(row=2, column=0, columnspan=3, padx=8, pady=(16, 8))
        self._register_action_button(verify_button)

    def _build_inspect_tab(self, parent: ctk.CTkFrame) -> None:
        container: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(parent)
        container.pack(fill="both", expand=True, padx=8, pady=8)
        container.columnconfigure(1, weight=1)

        self.inspect_image_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Image file", self.inspect_image_var, "Browse", self._pick_file, 0)

        self.inspect_format_var: tk.StringVar = tk.StringVar(value="text")
        ctk.CTkLabel(container, text="Output format:").grid(row=1, column=0, padx=8, pady=4, sticky="e")
        ctk.CTkOptionMenu(container, values=["text", "json"], variable=self.inspect_format_var, width=100).grid(
            row=1, column=1, padx=4, pady=4, sticky="w"
        )

        inspect_button: ctk.CTkButton = ctk.CTkButton(
            container,
            text="Inspect Image",
            command=self._on_inspect,
            font=ctk.CTkFont(weight="bold"),
            width=180,
            height=36,
        )
        inspect_button.grid(row=2, column=0, columnspan=3, padx=8, pady=(16, 8))
        self._register_action_button(inspect_button)

    def _build_unpack_tab(self, parent: ctk.CTkFrame) -> None:
        container: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(parent)
        container.pack(fill="both", expand=True, padx=8, pady=8)
        container.columnconfigure(1, weight=1)

        self.unpack_image_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Image file", self.unpack_image_var, "Browse", self._pick_file, 0)

        self.unpack_output_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Output folder", self.unpack_output_var, "Browse", self._pick_folder, 1)

        self.unpack_overwrite_var: tk.BooleanVar = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(container, text="Overwrite existing files", variable=self.unpack_overwrite_var).grid(
            row=2, column=0, columnspan=3, padx=8, pady=4, sticky="w"
        )

        unpack_button: ctk.CTkButton = ctk.CTkButton(
            container,
            text="Unpack Image",
            command=self._on_unpack,
            font=ctk.CTkFont(weight="bold"),
            width=180,
            height=36,
        )
        unpack_button.grid(row=3, column=0, columnspan=3, padx=8, pady=(16, 8))
        self._register_action_button(unpack_button)

    def _build_tree_tab(self, parent: ctk.CTkFrame) -> None:
        container: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(parent)
        container.pack(fill="both", expand=True, padx=8, pady=8)
        container.columnconfigure(1, weight=1)

        self.tree_image_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Image file", self.tree_image_var, "Browse", self._pick_file, 0)

        tree_button: ctk.CTkButton = ctk.CTkButton(
            container,
            text="Print Tree",
            command=self._on_tree,
            font=ctk.CTkFont(weight="bold"),
            width=180,
            height=36,
        )
        tree_button.grid(row=1, column=0, columnspan=3, padx=8, pady=(16, 8))
        self._register_action_button(tree_button)

    def _build_file_picker(
        self,
        parent: ctk.CTkFrame | ctk.CTkScrollableFrame,
        label: str,
        var: tk.StringVar,
        button_text: str,
        dialog: Callable[[], str | None],
        row: int,
    ) -> None:
        ctk.CTkLabel(parent, text=label + ":").grid(row=row, column=0, padx=8, pady=6, sticky="e")
        entry: ctk.CTkEntry = ctk.CTkEntry(parent, textvariable=var, width=600)
        entry.grid(row=row, column=1, padx=4, pady=6, sticky="ew")
        ctk.CTkButton(parent, text=button_text, command=lambda: self._browse(var, dialog), width=80).grid(
            row=row, column=2, padx=4, pady=6
        )

    def _build_progress_bar(self) -> None:
        self.progress_frame: ctk.CTkFrame = ctk.CTkFrame(self.root)
        self.progress_frame.grid(row=2, column=0, padx=12, pady=(4, 0), sticky="ew")
        self.progress_frame.columnconfigure(0, weight=1)

        self.progress_var: tk.DoubleVar = tk.DoubleVar(value=0.0)
        self.progress_bar: ctk.CTkProgressBar = ctk.CTkProgressBar(
            self.progress_frame, variable=self.progress_var, mode="determinate"
        )
        self.progress_bar.set(0.0)
        self.progress_bar.grid(row=0, column=0, padx=10, pady=(8, 4), sticky="ew")

        self.progress_label: ctk.CTkLabel = ctk.CTkLabel(self.progress_frame, text="Ready")
        self.progress_label.grid(row=1, column=0, padx=10, pady=(0, 6), sticky="w")

    def _build_log_console(self) -> None:
        log_frame: ctk.CTkFrame = ctk.CTkFrame(self.root)
        log_frame.grid(row=3, column=0, padx=12, pady=(6, 0), sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text: ctk.CTkTextbox = ctk.CTkTextbox(
            log_frame,
            height=200,
            wrap="word",
            font=ctk.CTkFont(family="Courier", size=11),
        )
        self.log_text.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")
        self.log_text.configure(state="disabled")

        # Configure color tags for modern terminal look
        self.log_text.tag_config(
            "header", foreground="#38bdf8", font=ctk.CTkFont(family="Courier", size=11, weight="bold")
        )
        self.log_text.tag_config(
            "section", foreground="#c084fc", font=ctk.CTkFont(family="Courier", size=11, weight="bold")
        )
        self.log_text.tag_config("success", foreground="#34d399")
        self.log_text.tag_config("warning", foreground="#fbbf24")
        self.log_text.tag_config(
            "error", foreground="#f87171", font=ctk.CTkFont(family="Courier", size=11, weight="bold")
        )
        self.log_text.tag_config("label", foreground="#94a3b8")
        self.log_text.tag_config("value", foreground="#f1f5f9")
        self.log_text.tag_config("info", foreground="#cbd5e1")

    def _on_close(self) -> None:
        """Cancel scheduled UI polling callbacks and close the window.

        Returns:
            None.
        """
        self.is_closing = True
        for after_id in (self.log_after_id, self.progress_after_id, self.completion_after_id):
            if after_id is not None:
                with suppress(tk.TclError):
                    self.root.after_cancel(after_id)
        self.root.destroy()

    def _register_action_button(self, button: ctk.CTkButton) -> None:
        """Track a primary action button so running workers can disable it.

        Args:
            button: Button to include in bulk enabled-state updates.

        Returns:
            None.
        """
        self.action_buttons.append(button)

    def _set_action_buttons_enabled(self, enabled: bool) -> None:
        """Set all primary action buttons to enabled or disabled.

        Args:
            enabled: Whether users can start a new primary operation.

        Returns:
            None.
        """
        state: str = "normal" if enabled else "disabled"
        for button in self.action_buttons:
            button.configure(state=state)

    def _browse(self, var: tk.StringVar, dialog: Callable[[], str | None]) -> None:
        result: str | None = dialog()
        if result:
            var.set(result)

    def _pick_folder(self) -> str | None:
        path: str = filedialog.askdirectory()
        return path if path else None

    def _pick_file(self) -> str | None:
        path: str = filedialog.askopenfilename(
            filetypes=[
                ("PFS images", "*.ffpfs *.ffpfsc *.pfs *.dat *.bin"),
                ("All files", "*.*"),
            ]
        )
        return path if path else None

    def _pick_archive_file(self) -> str | None:
        """Open a file picker for ZIP and RAR archive sources.

        Returns:
            Selected archive path, or None when the dialog is cancelled.
        """
        path: str = filedialog.askopenfilename(
            filetypes=[
                ("Archives", "*.zip *.rar *.part1.rar *.r00 *.r01 *.001"),
                ("All files", "*.*"),
            ]
        )
        return path if path else None

    def _save_file(self) -> str | None:
        path: str = filedialog.asksaveasfilename(
            defaultextension=".ffpfs",
            filetypes=[
                ("PFS image", "*.ffpfs"),
                ("PFSC compressed image", "*.ffpfsc"),
                ("All files", "*.*"),
            ],
        )
        return path if path else None

    def enqueue_log(self, text: str, tag: str = "info") -> None:
        # Use put_nowait so a full queue drops items rather than blocking the
        # worker thread. A blocked worker cannot reach its finally block, which
        # would leave sys.stdout/stderr redirected and all action buttons
        # permanently disabled (deadlock).
        for match in PROGRESS_LINE_RE.finditer(text):
            percent: int = max(0, min(int(match.group("pct")), 100))
            label: str = match.group("label").strip() or "Working"
            with suppress(queue.Full):
                self.progress_queue.put_nowait((percent / 100, label))
        log_text: str = PROGRESS_LINE_RE.sub("", text.replace("\r", "\n"))
        if log_text.strip():
            with suppress(queue.Full):
                self.log_queue.put_nowait((log_text, tag))

    def _start_log_polling(self) -> None:
        self._poll_log_queue()

    def _start_progress_polling(self) -> None:
        """Start polling for parsed CLI progress updates.

        Returns:
            None.
        """
        self._poll_progress_queue()

    def _start_completion_polling(self) -> None:
        """Start polling for worker completion events.

        Returns:
            None.
        """
        self._poll_completion_queue()

    def _poll_log_queue(self) -> None:
        if self.is_closing:
            return
        chunks: list[str] = []
        try:
            while True:
                text, _tag = self.log_queue.get_nowait()
                chunks.append(text)
        except queue.Empty:
            pass
        if chunks:
            self._append_log("".join(chunks))
        self.log_after_id = self.root.after(100, self._poll_log_queue)

    def _poll_progress_queue(self) -> None:
        """Poll parsed CLI progress updates.

        Returns:
            None.
        """
        if self.is_closing:
            return
        self._drain_progress_queue()
        self.progress_after_id = self.root.after(100, self._poll_progress_queue)

    def _drain_progress_queue(self) -> None:
        """Apply queued worker progress updates to Tk widgets on the main thread.

        Returns:
            None.
        """
        latest: tuple[float, str] | None = None
        try:
            while True:
                latest = self.progress_queue.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            value, label = latest
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            self._set_progress(value, f"{label} ({int(value * 100)}%)")

    def _poll_completion_queue(self) -> None:
        """Drain worker completion events and update Tk widgets on the main thread.

        Returns:
            None.
        """
        if self.is_closing:
            return
        completed: bool = False
        try:
            while True:
                self.completion_queue.get_nowait()
                completed = True
        except queue.Empty:
            pass
        if completed:
            self._drain_progress_queue()
            self._on_worker_done()
        self.completion_after_id = self.root.after(100, self._poll_completion_queue)

    _MAX_LOG_LINES: int = 5000

    def _trim_log(self) -> None:
        """Keep the log widget under a maximum line count to prevent memory growth.

        Tkinter text delete("1.0", "N.0") removes lines 1 … N-1 (the end index
        is exclusive at the character level). To delete exactly *trim_to* lines
        we must pass trim_to + 1 as the end line number.
        """
        line_count_str: str = self.log_text.index("end-1c")
        line_count: int = int(line_count_str.split(".")[0])
        if line_count > self._MAX_LOG_LINES:
            trim_to: int = line_count - self._MAX_LOG_LINES
            self.log_text.delete("1.0", f"{trim_to + 1}.0")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        lines: list[str] = text.split("\n")
        for i, line in enumerate(lines):
            suffix: str = "\n" if i < len(lines) - 1 else ""
            stripped: str = line.strip()

            if stripped.startswith("===") or stripped.endswith("==="):
                self.log_text.insert("end", line + suffix, "header")
                continue

            if stripped in (
                "PFS Image Builder - Parameters",
                "Build Summary",
                "PFS Check Report",
                "Build Details",
                "PFS Image Info",
                "PFS Image Inspection",
            ):
                self.log_text.insert("end", line + suffix, "section")
                continue

            if any(
                term in line
                for term in (
                    "Error:",
                    "error:",
                    "ERROR:",
                    "failed:",
                    "failed",
                    "mismatch",
                    "escapes",
                    "NameError",
                    "BuildError",
                    "ValueError",
                    "Exception",
                    "FileNotFoundError",
                )
            ):
                self.log_text.insert("end", line + suffix, "error")
                continue

            if any(term in line.lower() for term in ("warning", "warn", "stale")):
                self.log_text.insert("end", line + suffix, "warning")
                continue

            if any(term in line.lower() for term in ("successfully", "passed", "✓")) or "completed" in line.lower():
                self.log_text.insert("end", line + suffix, "success")
                continue

            if line.startswith("  ") and ":" in line:
                parts: list[str] = line.split(":", 1)
                self.log_text.insert("end", parts[0] + ":", "label")
                self.log_text.insert("end", parts[1] + suffix, "value")
                continue

            self.log_text.insert("end", line + suffix, "info")

        self._trim_log()
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _set_progress(self, value: float, label: str = "") -> None:
        self.progress_var.set(value)
        if label:
            self.progress_label.configure(text=label)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _run_worker(self, name: str, target: Callable[[], int | None]) -> None:
        if self.worker_thread is not None and self.worker_thread.is_alive():
            messagebox.showwarning("Busy", "An operation is already running. Please wait.")
            return

        self._clear_log()
        self._set_progress(0.0, f"Running {name}...")
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()
        self._set_action_buttons_enabled(False)

        def wrapper() -> None:
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            sys.stdout = GuiLogRedirect(self, "info")
            sys.stderr = GuiLogRedirect(self, "error")
            try:
                result: int | None = target()
                if result == 0:
                    self.enqueue_log(f"\n{name} completed successfully.\n", "ok")
                else:
                    self.enqueue_log(f"\n{name} finished with exit code {result}.\n", "warning")
            except SystemExit as exc:
                code: int = exc.code if isinstance(exc.code, int) else 1
                if code == 0:
                    self.enqueue_log(f"\n{name} completed successfully.\n", "ok")
                else:
                    self.enqueue_log(f"\n{name} finished with exit code {code}.\n", "warning")
            except Exception as exc:
                print(f"Unhandled error during {name}: {exc}", file=sys.stderr)
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                # Non-blocking: the poller drains this queue every 100 ms so it
                # is virtually always empty. Dropping is safer than blocking a
                # daemon thread that can no longer be joined.
                with suppress(queue.Full):
                    self.completion_queue.put_nowait(None)

        self.worker_thread = threading.Thread(target=wrapper, daemon=True)
        self.worker_thread.start()

    def _on_worker_done(self) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_var.set(1.0)
        self.progress_label.configure(text="Done")
        self._set_action_buttons_enabled(True)
        self.worker_thread = None
        # Drain any leftover queue items to free references and prevent memory growth.
        for q in (self.log_queue, self.progress_queue, self.completion_queue):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass

    def _apply_pack_preset(self, version: str, compress: bool) -> None:
        """Apply a named preset to the Pack Folder tab controls.

        Args:
            version: Target platform string, either "PS5" or "PS4".
            compress: Whether to enable image compression.

        Returns:
            None.

        Note:
            This method is currently not connected to any UI element and is
            dead code. Wire it to a button or menu item before exposing it.
        """
        self.pack_version_var.set(version)
        self.pack_compress_var.set(compress)
        self.pack_inode_var.set("32")
        self.pack_block_var.set("65536")
        self.pack_compression_level_var.set("9")
        self.pack_skip_exe_var.set(True)
        self.pack_signed_var.set(False)
        self.pack_encrypted_var.set(False)
        self.pack_case_var.set(True)
        self.pack_verify_var.set(True)
        self.pack_require_game_var.set(False)
        self.pack_min_size_var.set("65536")
        self.pack_max_ratio_var.set("95")
        self.pack_ekpfs_var.set("")
        info: str = f"Applied preset: {version}"
        if compress:
            info += " with compression"
        self.progress_label.configure(text=info)

    def _toggle_pack_advanced_options(self) -> None:
        """Show or hide uncommon pack-folder options.

        Returns:
            None.
        """
        if self.pack_show_advanced_var.get():
            self.pack_advanced_frame.grid()
        else:
            self.pack_advanced_frame.grid_remove()

    def _toggle_pack_archive_advanced_options(self) -> None:
        """Show or hide uncommon pack-archive options.

        Returns:
            None.
        """
        if self.pack_archive_show_advanced_var.get():
            self.pack_archive_advanced_frame.grid()
        else:
            self.pack_archive_advanced_frame.grid_remove()

    def _on_pack_folder(self) -> None:
        source: str = self.pack_folder_source_var.get().strip()
        output: str = self.pack_folder_output_var.get().strip()
        if not source or not Path(source).exists():
            messagebox.showerror("Error", "Please select a valid source folder.")
            return
        if not output:
            messagebox.showerror("Error", "Please specify an output image path.")
            return
        argv: list[str] = [
            source,
            output,
            "--version",
            self.pack_version_var.get(),
            "--inode-bits",
            self.pack_inode_var.get(),
            "--block-size",
            self.pack_block_var.get(),
        ]
        if not self.pack_compress_var.get():
            argv.append("--no-compress")
        argv.extend(["--compression-level", self.pack_compression_level_var.get()])
        if self.pack_skip_exe_var.get():
            argv.append("--skip-executable-compression")
        argv.extend(["--min-compress-size", self.pack_min_size_var.get()])
        argv.extend(["--max-compressed-ratio", self.pack_max_ratio_var.get()])
        if self.pack_signed_var.get():
            argv.append("--signed")
        if self.pack_encrypted_var.get():
            argv.append("--encrypted")
        if self.pack_case_var.get():
            argv.append("--case-insensitive")
        else:
            argv.append("--case-sensitive")
        if self.pack_verify_var.get():
            argv.append("--verify")
        if self.pack_require_game_var.get():
            argv.append("--require-game-files")
        ekpfs: str = self.pack_ekpfs_var.get().strip()
        if ekpfs:
            argv.extend(["--ekpfs-key", ekpfs])
        self._run_worker("Pack Folder", lambda: self._do_pack_folder(argv))

    def _do_pack_folder(self, argv: list[str]) -> int:
        parser = argparse.ArgumentParser()
        cli_mkpfs_add_create_args(parser)
        args = parser.parse_args(argv)
        args.command = "pack"
        args.pack_command = "folder"
        return cli_mkpfs_create_run(args)

    def _on_pack_file(self) -> None:
        source: str = self.pack_file_source_var.get().strip()
        output: str = self.pack_file_output_var.get().strip()
        if not source or not Path(source).exists():
            messagebox.showerror("Error", "Please select a valid source file.")
            return
        if not output:
            messagebox.showerror("Error", "Please specify an output image path.")
            return
        argv: list[str] = [source, output]
        if not self.pack_file_compress_var.get():
            argv.append("--no-compress")
        if self.pack_file_verify_var.get():
            argv.append("--verify")
        self._run_worker("Pack File", lambda: self._do_pack_file(argv))

    def _do_pack_file(self, argv: list[str]) -> int:
        parser = argparse.ArgumentParser()
        cli_mkpfs_add_create_args(parser, source_arg_name="source_file", include_require_game_files=False)
        args = parser.parse_args(argv)
        args.command = "pack"
        args.pack_command = "file"
        return cli_mkpfs_pack_file_run(args)

    def _on_pack_archive(self) -> None:
        """Build the archive pack argv from current GUI state and start a worker.

        Returns:
            None.
        """
        source: str = self.pack_archive_source_var.get().strip()
        output: str = self.pack_archive_output_var.get().strip()
        if not source or not Path(source).exists():
            messagebox.showerror("Error", "Please select a valid source archive.")
            return
        if not output:
            messagebox.showerror("Error", "Please specify an output image path.")
            return
        argv: list[str] = [
            source,
            output,
            "--version",
            self.pack_archive_version_var.get(),
            "--inode-bits",
            self.pack_archive_inode_var.get(),
            "--block-size",
            self.pack_archive_block_var.get(),
        ]
        password: str = self.pack_archive_password_var.get()
        if password:
            argv.extend(["--password", password])
        if not self.pack_archive_compress_var.get():
            argv.append("--no-compress")
        argv.extend(["--compression-level", self.pack_archive_compression_level_var.get()])
        if self.pack_archive_skip_exe_var.get():
            argv.append("--skip-executable-compression")
        argv.extend(["--min-compress-size", self.pack_archive_min_size_var.get()])
        argv.extend(["--max-compressed-ratio", self.pack_archive_max_ratio_var.get()])
        if self.pack_archive_signed_var.get():
            argv.append("--signed")
        if self.pack_archive_encrypted_var.get():
            argv.append("--encrypted")
        if self.pack_archive_case_var.get():
            argv.append("--case-insensitive")
        else:
            argv.append("--case-sensitive")
        if self.pack_archive_verify_var.get():
            argv.append("--verify")
        if self.pack_archive_require_game_var.get():
            argv.append("--require-game-files")
        ekpfs: str = self.pack_archive_ekpfs_var.get().strip()
        if ekpfs:
            argv.extend(["--ekpfs-key", ekpfs])
        self._run_worker("Pack Archive", lambda: self._do_pack_archive(argv))

    def _do_pack_archive(self, argv: list[str]) -> int:
        """Run the archive pack command from a GUI worker.

        Args:
            argv: Argument list for the archive pack parser.

        Returns:
            Process exit code from the archive pack workflow.
        """
        parser = argparse.ArgumentParser()
        cli_mkpfs_add_create_args(parser, source_arg_name="source_archive")
        parser.add_argument("--password")
        args = parser.parse_args(argv)
        args.command = "pack"
        args.pack_command = "archive"
        return cli_mkpfs_pack_archive_run(args)

    def _on_verify(self) -> None:
        image: str = self.verify_image_var.get().strip()
        if not image or not Path(image).exists():
            messagebox.showerror("Error", "Please select a valid image file.")
            return
        source: str = self.verify_source_var.get().strip()
        argv: list[str] = [image]
        if source:
            argv.extend(["--source-dir", source])
        self._run_worker("Verify", lambda: self._do_verify(argv))

    def _do_verify(self, argv: list[str]) -> int:
        parser = argparse.ArgumentParser()
        parser.add_argument("image_file")
        check_source_group = parser.add_mutually_exclusive_group()
        check_source_group.add_argument("--source-dir")
        check_source_group.add_argument("--source-file")
        args = parser.parse_args(argv)
        args.command = "verify"
        return cli_mkpfs_check_run(args)

    def _on_inspect(self) -> None:
        image: str = self.inspect_image_var.get().strip()
        if not image or not Path(image).exists():
            messagebox.showerror("Error", "Please select a valid image file.")
            return
        argv: list[str] = [image, "--format", self.inspect_format_var.get()]
        self._run_worker("Inspect", lambda: self._do_inspect(argv))

    def _do_inspect(self, argv: list[str]) -> int:
        parser = argparse.ArgumentParser()
        parser.add_argument("image_file")
        parser.add_argument("--format", choices=["text", "json"], default="text")
        args = parser.parse_args(argv)
        args.command = "inspect"
        return cli_mkpfs_inspect_run(args)

    def _on_unpack(self) -> None:
        image: str = self.unpack_image_var.get().strip()
        output: str = self.unpack_output_var.get().strip()
        if not image or not Path(image).exists():
            messagebox.showerror("Error", "Please select a valid image file.")
            return
        if not output:
            messagebox.showerror("Error", "Please specify an output folder.")
            return
        argv: list[str] = [image, output]
        if self.unpack_overwrite_var.get():
            argv.append("--overwrite")
        self._run_worker("Unpack", lambda: self._do_unpack(argv))

    def _do_unpack(self, argv: list[str]) -> int:
        parser = argparse.ArgumentParser()
        parser.add_argument("image_file")
        parser.add_argument("output_dir")
        parser.add_argument("--overwrite", action="store_true")
        args = parser.parse_args(argv)
        args.command = "unpack"
        return cli_mkpfs_extract_run(args)

    def _on_tree(self) -> None:
        image: str = self.tree_image_var.get().strip()
        if not image or not Path(image).exists():
            messagebox.showerror("Error", "Please select a valid image file.")
            return
        argv: list[str] = [image]
        self._run_worker("Tree", lambda: self._do_tree(argv))

    def _do_tree(self, argv: list[str]) -> int:
        parser = argparse.ArgumentParser()
        parser.add_argument("image_file")
        args = parser.parse_args(argv)
        args.command = "tree"
        return cli_mkpfs_ls_run(args)


def run_gui() -> int:
    """Launch the MkPFS GUI.

    Returns:
        Exit code. Always 0 unless the GUI fails to initialise.
    """
    root: ctk.CTk = ctk.CTk()
    _app: MkPFSApp = MkPFSApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_gui())
