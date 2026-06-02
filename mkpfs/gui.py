"""Graphical user interface for MkPFS.

This module provides a cross-platform desktop GUI built with customtkinter.
It wraps the CLI workflows so users can pack, verify, inspect, unpack, and
browse PFS images without typing commands.
"""

from __future__ import annotations

import argparse
import io
import queue
import sys
import threading
import tkinter as tk
from collections.abc import Callable
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
    cli_mkpfs_pack_file_run,
)


class GuiLogRedirect(io.StringIO):
    """Thread-safe redirector that feeds a Tkinter text widget via a queue."""

    def __init__(self, app: MkPFSApp, tag: str = "info") -> None:
        self._app: MkPFSApp = app
        self._tag: str = tag
        super().__init__()

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

    def __init__(self, root: ctk.CTk) -> None:
        self.root: ctk.CTk = root
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None

        self._setup_window()
        self._build_ui()
        self._start_log_polling()

    def _setup_window(self) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        self.root.title("MkPFS - PlayStation File System Builder")
        self.root.geometry("1100x780")
        self.root.minsize(900, 650)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=0)
        self.root.rowconfigure(1, weight=1)
        self.root.rowconfigure(2, weight=0)
        self.root.rowconfigure(3, weight=0)

        self._build_header()
        self._build_tabs()
        self._build_progress_bar()
        self._build_log_console()
        self._build_status_bar()

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
        self.notebook.add("Pack File")
        self.notebook.add("Verify")
        self.notebook.add("Inspect")
        self.notebook.add("Unpack")
        self.notebook.add("Tree")

        self._build_pack_folder_tab(self.notebook.tab("Pack Folder"))
        self._build_pack_file_tab(self.notebook.tab("Pack File"))
        self._build_verify_tab(self.notebook.tab("Verify"))
        self._build_inspect_tab(self.notebook.tab("Inspect"))
        self._build_unpack_tab(self.notebook.tab("Unpack"))
        self._build_tree_tab(self.notebook.tab("Tree"))

    def _build_pack_folder_tab(self, parent: ctk.CTkFrame) -> None:
        container: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(parent)
        container.pack(fill="both", expand=True, padx=8, pady=8)

        self.pack_folder_source_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(
            container, "Source folder", self.pack_folder_source_var, "Browse", self._pick_folder, 0
        )

        self.pack_folder_output_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Output image", self.pack_folder_output_var, "Save As", self._save_file, 1)

        preset_frame: ctk.CTkFrame = ctk.CTkFrame(container)
        preset_frame.grid(row=2, column=0, columnspan=3, padx=4, pady=(12, 4), sticky="ew")
        ctk.CTkLabel(preset_frame, text="Quick Presets:", font=ctk.CTkFont(weight="bold")).pack(
            side="left", padx=8, pady=6
        )
        ctk.CTkButton(
            preset_frame, text="PS5 Compressed", command=lambda: self._apply_pack_preset("PS5", True), width=140
        ).pack(side="left", padx=4, pady=6)
        ctk.CTkButton(
            preset_frame, text="PS4 Compressed", command=lambda: self._apply_pack_preset("PS4", True), width=140
        ).pack(side="left", padx=4, pady=6)
        ctk.CTkButton(
            preset_frame, text="Uncompressed", command=lambda: self._apply_pack_preset("PS5", False), width=140
        ).pack(side="left", padx=4, pady=6)

        adv_frame: ctk.CTkFrame = ctk.CTkFrame(container)
        adv_frame.grid(row=3, column=0, columnspan=3, padx=4, pady=(8, 4), sticky="ew")
        ctk.CTkLabel(adv_frame, text="Advanced Options", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=4, padx=8, pady=(8, 4), sticky="w"
        )

        self.pack_version_var: tk.StringVar = tk.StringVar(value="PS5")
        ctk.CTkLabel(adv_frame, text="Version:").grid(row=1, column=0, padx=8, pady=4, sticky="e")
        ctk.CTkOptionMenu(adv_frame, values=["PS4", "PS5"], variable=self.pack_version_var, width=100).grid(
            row=1, column=1, padx=4, pady=4, sticky="w"
        )

        self.pack_inode_var: tk.StringVar = tk.StringVar(value="32")
        ctk.CTkLabel(adv_frame, text="Inode bits:").grid(row=1, column=2, padx=8, pady=4, sticky="e")
        ctk.CTkOptionMenu(adv_frame, values=["32", "64"], variable=self.pack_inode_var, width=100).grid(
            row=1, column=3, padx=4, pady=4, sticky="w"
        )

        self.pack_block_var: tk.StringVar = tk.StringVar(value="65536")
        ctk.CTkLabel(adv_frame, text="Block size:").grid(row=2, column=0, padx=8, pady=4, sticky="e")
        ctk.CTkOptionMenu(
            adv_frame,
            values=["auto", "4096", "8192", "16384", "32768", "65536", "auto-fit"],
            variable=self.pack_block_var,
            width=100,
        ).grid(row=2, column=1, padx=4, pady=4, sticky="w")

        self.pack_compression_level_var: tk.StringVar = tk.StringVar(value="9")
        ctk.CTkLabel(adv_frame, text="Zlib level:").grid(row=2, column=2, padx=8, pady=4, sticky="e")
        ctk.CTkOptionMenu(
            adv_frame,
            values=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"],
            variable=self.pack_compression_level_var,
            width=80,
        ).grid(row=2, column=3, padx=4, pady=4, sticky="w")

        self.pack_compress_var: tk.BooleanVar = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(adv_frame, text="Enable PFSC compression", variable=self.pack_compress_var).grid(
            row=3, column=0, columnspan=2, padx=8, pady=4, sticky="w"
        )

        self.pack_skip_exe_var: tk.BooleanVar = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(adv_frame, text="Skip executable compression", variable=self.pack_skip_exe_var).grid(
            row=3, column=2, columnspan=2, padx=8, pady=4, sticky="w"
        )

        self.pack_signed_var: tk.BooleanVar = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(adv_frame, text="Signed image", variable=self.pack_signed_var).grid(
            row=4, column=0, columnspan=2, padx=8, pady=4, sticky="w"
        )

        self.pack_encrypted_var: tk.BooleanVar = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(adv_frame, text="Encrypted", variable=self.pack_encrypted_var).grid(
            row=4, column=2, columnspan=2, padx=8, pady=4, sticky="w"
        )

        self.pack_case_var: tk.BooleanVar = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(adv_frame, text="Case insensitive", variable=self.pack_case_var).grid(
            row=5, column=0, columnspan=2, padx=8, pady=4, sticky="w"
        )

        self.pack_verify_var: tk.BooleanVar = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(adv_frame, text="Verify after pack", variable=self.pack_verify_var).grid(
            row=5, column=2, columnspan=2, padx=8, pady=4, sticky="w"
        )

        self.pack_require_game_var: tk.BooleanVar = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            adv_frame, text="Require game files (param.json + eboot.bin)", variable=self.pack_require_game_var
        ).grid(row=6, column=0, columnspan=2, padx=8, pady=4, sticky="w")

        ctk.CTkLabel(adv_frame, text="Min compress size:").grid(row=6, column=2, padx=8, pady=4, sticky="e")
        self.pack_min_size_var: tk.StringVar = tk.StringVar(value="65536")
        ctk.CTkEntry(adv_frame, textvariable=self.pack_min_size_var, width=100).grid(
            row=6, column=3, padx=4, pady=4, sticky="w"
        )

        ctk.CTkLabel(adv_frame, text="Max compressed ratio (%):").grid(row=7, column=0, padx=8, pady=4, sticky="e")
        self.pack_max_ratio_var: tk.StringVar = tk.StringVar(value="95")
        ctk.CTkEntry(adv_frame, textvariable=self.pack_max_ratio_var, width=100).grid(
            row=7, column=1, padx=4, pady=4, sticky="w"
        )

        ctk.CTkLabel(adv_frame, text="EKPFS key (hex, optional):").grid(row=8, column=0, padx=8, pady=4, sticky="e")
        self.pack_ekpfs_var: tk.StringVar = tk.StringVar()
        ctk.CTkEntry(
            adv_frame, textvariable=self.pack_ekpfs_var, width=420, placeholder_text="64 hex chars or leave blank"
        ).grid(row=8, column=1, columnspan=3, padx=4, pady=4, sticky="w")

        ctk.CTkButton(
            container,
            text="Pack Folder",
            command=self._on_pack_folder,
            font=ctk.CTkFont(weight="bold"),
            width=180,
            height=36,
        ).grid(row=4, column=0, columnspan=3, padx=8, pady=(16, 8))

    def _build_pack_file_tab(self, parent: ctk.CTkFrame) -> None:
        container: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(parent)
        container.pack(fill="both", expand=True, padx=8, pady=8)

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

        ctk.CTkButton(
            container,
            text="Pack File",
            command=self._on_pack_file,
            font=ctk.CTkFont(weight="bold"),
            width=180,
            height=36,
        ).grid(row=3, column=0, columnspan=3, padx=8, pady=(16, 8))

    def _build_verify_tab(self, parent: ctk.CTkFrame) -> None:
        container: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(parent)
        container.pack(fill="both", expand=True, padx=8, pady=8)

        self.verify_image_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Image file", self.verify_image_var, "Browse", self._pick_file, 0)

        self.verify_source_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(
            container, "Source folder (optional)", self.verify_source_var, "Browse", self._pick_folder, 1
        )

        ctk.CTkButton(
            container,
            text="Verify Image",
            command=self._on_verify,
            font=ctk.CTkFont(weight="bold"),
            width=180,
            height=36,
        ).grid(row=2, column=0, columnspan=3, padx=8, pady=(16, 8))

    def _build_inspect_tab(self, parent: ctk.CTkFrame) -> None:
        container: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(parent)
        container.pack(fill="both", expand=True, padx=8, pady=8)

        self.inspect_image_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Image file", self.inspect_image_var, "Browse", self._pick_file, 0)

        self.inspect_format_var: tk.StringVar = tk.StringVar(value="text")
        ctk.CTkLabel(container, text="Output format:").grid(row=1, column=0, padx=8, pady=4, sticky="e")
        ctk.CTkOptionMenu(container, values=["text", "json"], variable=self.inspect_format_var, width=100).grid(
            row=1, column=1, padx=4, pady=4, sticky="w"
        )

        ctk.CTkButton(
            container,
            text="Inspect Image",
            command=self._on_inspect,
            font=ctk.CTkFont(weight="bold"),
            width=180,
            height=36,
        ).grid(row=2, column=0, columnspan=3, padx=8, pady=(16, 8))

    def _build_unpack_tab(self, parent: ctk.CTkFrame) -> None:
        container: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(parent)
        container.pack(fill="both", expand=True, padx=8, pady=8)

        self.unpack_image_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Image file", self.unpack_image_var, "Browse", self._pick_file, 0)

        self.unpack_output_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Output folder", self.unpack_output_var, "Browse", self._pick_folder, 1)

        self.unpack_overwrite_var: tk.BooleanVar = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(container, text="Overwrite existing files", variable=self.unpack_overwrite_var).grid(
            row=2, column=0, columnspan=3, padx=8, pady=4, sticky="w"
        )

        ctk.CTkButton(
            container,
            text="Unpack Image",
            command=self._on_unpack,
            font=ctk.CTkFont(weight="bold"),
            width=180,
            height=36,
        ).grid(row=3, column=0, columnspan=3, padx=8, pady=(16, 8))

    def _build_tree_tab(self, parent: ctk.CTkFrame) -> None:
        container: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(parent)
        container.pack(fill="both", expand=True, padx=8, pady=8)

        self.tree_image_var: tk.StringVar = tk.StringVar()
        self._build_file_picker(container, "Image file", self.tree_image_var, "Browse", self._pick_file, 0)

        ctk.CTkButton(
            container,
            text="Print Tree",
            command=self._on_tree,
            font=ctk.CTkFont(weight="bold"),
            width=180,
            height=36,
        ).grid(row=1, column=0, columnspan=3, padx=8, pady=(16, 8))

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
        entry: ctk.CTkEntry = ctk.CTkEntry(parent, textvariable=var, width=460)
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

    def _build_status_bar(self) -> None:
        self.status_bar: ctk.CTkLabel = ctk.CTkLabel(self.root, text="Ready", anchor="w", font=ctk.CTkFont(size=11))
        self.status_bar.grid(row=4, column=0, padx=12, pady=(2, 8), sticky="ew")

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
        self.log_queue.put((text, tag))

    def _start_log_polling(self) -> None:
        self._poll_log_queue()

    def _poll_log_queue(self) -> None:
        try:
            while True:
                text, _tag = self.log_queue.get_nowait()
                self._append_log(text)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_log_queue)

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _set_progress(self, value: float, label: str = "") -> None:
        self.progress_var.set(value)
        if label:
            self.progress_label.configure(text=label)
            self.status_bar.configure(text=label)

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
            except Exception as exc:
                print(f"Unhandled error during {name}: {exc}", file=sys.stderr)
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                self.root.after(0, lambda: self._on_worker_done())

        self.worker_thread = threading.Thread(target=wrapper, daemon=True)
        self.worker_thread.start()

    def _on_worker_done(self) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_var.set(1.0)
        self.progress_label.configure(text="Done")
        self.status_bar.configure(text="Ready")

    def _apply_pack_preset(self, version: str, compress: bool) -> None:
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
        info = f"Applied preset: {version}"
        if compress:
            info += " with compression"
        self.status_bar.configure(text=info)

    def _on_pack_folder(self) -> None:
        source: str = self.pack_folder_source_var.get().strip()
        output: str = self.pack_folder_output_var.get().strip()
        if not source or not Path(source).exists():
            messagebox.showerror("Error", "Please select a valid source folder.")
            return
        if not output:
            messagebox.showerror("Error", "Please specify an output image path.")
            return
        self._run_worker("Pack Folder", lambda: self._do_pack_folder(source, output))

    def _do_pack_folder(self, source: str, output: str) -> int:
        argv: list[str] = [
            "pack",
            "folder",
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

        parser = argparse.ArgumentParser()
        cli_mkpfs_add_create_args(parser)
        args = parser.parse_args(argv[2:])
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
        self._run_worker("Pack File", lambda: self._do_pack_file(source, output))

    def _do_pack_file(self, source: str, output: str) -> int:
        argv: list[str] = ["pack", "file", source, output]
        if not self.pack_file_compress_var.get():
            argv.append("--no-compress")
        if self.pack_file_verify_var.get():
            argv.append("--verify")

        parser = argparse.ArgumentParser()
        cli_mkpfs_add_create_args(parser, source_arg_name="source_file", include_require_game_files=False)
        args = parser.parse_args(argv[2:])
        args.command = "pack"
        args.pack_command = "file"
        return cli_mkpfs_pack_file_run(args)

    def _on_verify(self) -> None:
        image: str = self.verify_image_var.get().strip()
        if not image or not Path(image).exists():
            messagebox.showerror("Error", "Please select a valid image file.")
            return
        self._run_worker("Verify", lambda: self._do_verify(image))

    def _do_verify(self, image: str) -> int:
        source: str = self.verify_source_var.get().strip()
        argv: list[str] = ["verify", image]
        if source:
            argv.extend(["--source-dir", source])
        parser = argparse.ArgumentParser()
        parser.add_argument("image_file")
        check_source_group = parser.add_mutually_exclusive_group()
        check_source_group.add_argument("--source-dir")
        check_source_group.add_argument("--source-file")
        args = parser.parse_args(argv[1:])
        args.command = "verify"
        return cli_mkpfs_check_run(args)

    def _on_inspect(self) -> None:
        image: str = self.inspect_image_var.get().strip()
        if not image or not Path(image).exists():
            messagebox.showerror("Error", "Please select a valid image file.")
            return
        self._run_worker("Inspect", lambda: self._do_inspect(image))

    def _do_inspect(self, image: str) -> int:
        argv: list[str] = ["inspect", image, "--format", self.inspect_format_var.get()]
        parser = argparse.ArgumentParser()
        parser.add_argument("image_file")
        parser.add_argument("--format", choices=["text", "json"], default="text")
        args = parser.parse_args(argv[1:])
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
        self._run_worker("Unpack", lambda: self._do_unpack(image, output))

    def _do_unpack(self, image: str, output: str) -> int:
        argv: list[str] = ["unpack", image, output]
        if self.unpack_overwrite_var.get():
            argv.append("--overwrite")
        parser = argparse.ArgumentParser()
        parser.add_argument("image_file")
        parser.add_argument("output_dir")
        parser.add_argument("--overwrite", action="store_true")
        args = parser.parse_args(argv[1:])
        args.command = "unpack"
        return cli_mkpfs_extract_run(args)

    def _on_tree(self) -> None:
        image: str = self.tree_image_var.get().strip()
        if not image or not Path(image).exists():
            messagebox.showerror("Error", "Please select a valid image file.")
            return
        self._run_worker("Tree", lambda: self._do_tree(image))

    def _do_tree(self, image: str) -> int:
        argv: list[str] = ["tree", image]
        parser = argparse.ArgumentParser()
        parser.add_argument("image_file")
        args = parser.parse_args(argv[1:])
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
