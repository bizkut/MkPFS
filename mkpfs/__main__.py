"""MKPFS CLI main() hook with optional GUI launch."""

from __future__ import annotations

import sys

from mkpfs.cli import cli_mkpfs_main


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for ``python -m mkpfs``.

    Supports ``--gui`` to launch the graphical interface.

    Args:
        argv: Optional argument vector. When omitted, sys.argv is used by
            the argument parser.

    Returns:
        The integer exit code from the CLI or GUI handler.
    """
    effective_argv: list[str] = list(sys.argv[1:] if argv is None else argv)
    if effective_argv and effective_argv[0] == "--gui":
        from mkpfs.gui import run_gui

        return run_gui()
    return cli_mkpfs_main(argv)


# When executed as a script, run the main entrypoint.
if __name__ == "__main__":
    raise SystemExit(main())
