"""Utilities shared between multiple modules."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import BinaryIO


def human_readable_size(size: int) -> str:
    """Convert a byte count to a human-readable string.

    Args:
        size: Number of bytes.

    Returns:
        Human readable string using binary prefixes (KB, MB, ...).
    """
    s: float = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if s < 1024.0:
            return f"{s:.2f} {unit}"
        s /= 1024.0
    return f"{s:.2f} PB"


def ceil_div(a: int, b: int) -> int:
    """Compute the integer ceiling of a / b.

    Args:
        a: Numerator.
        b: Denominator (must be positive).

    Returns:
        The smallest integer >= a / b.
    """
    result: int = (a + b - 1) // b
    return result


def is_power_of_two(v: int) -> bool:
    """Return True if ``v`` is a positive power of two.

    Args:
        v: Value to test.

    Returns:
        True when v is 1,2,4,8,...; False otherwise.
    """
    return v > 0 and (v & (v - 1)) == 0


def normalize_output_path(path_arg: str, desired_suffix: str, adjust: bool = True) -> tuple[Path, bool]:
    """Normalize an output path extension when automatic adjustment is enabled.

    Args:
        path_arg: Input path string provided by the user.
        desired_suffix: Desired output suffix, including the leading dot.
        adjust: When True, replace the current suffix when it does not match the
            desired suffix. When False, return the path unchanged.

    Returns:
        A tuple of ``(normalized_path, changed)`` where ``changed`` is True when
        the suffix was updated.
    """
    p: Path = Path(path_arg)
    if not adjust:
        return p, False
    if p.suffix.lower() == desired_suffix.lower():
        return p, False
    normalized: Path = p.with_suffix(desired_suffix)
    return normalized, True


def _app_directory() -> Path:
    """Return the directory where the application is installed or running.

    For frozen builds (PyInstaller etc.) this is the executable parent folder.
    For source runs it is the project root (parent of the mkpfs package).

    Returns:
        Absolute path to the app directory.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resolve_temp_root(temp_folder: Path | None = None) -> Path:
    """Resolve the temporary root directory used for pack artifacts.

    Uses the caller-provided folder when given, otherwise falls back to the
    system temporary directory. Large archive extractions should use the
    system temp folder; the app directory is reserved for small persistent
    caches such as downloaded decompression backends.

    Args:
        temp_folder: Optional caller-provided temp directory path.

    Returns:
        Existing directory path used for temporary files.
    """
    if temp_folder is not None:
        temp_root: Path = temp_folder.expanduser().resolve()
        temp_root.mkdir(parents=True, exist_ok=True)
        return temp_root

    # Try local ./tmp folder under app directory first to avoid space or OS pruning issues in system temp
    try:
        local_tmp: Path = _app_directory() / "tmp"
        if local_tmp.is_dir():
            test_file: Path = local_tmp / ".write_test"
            test_file.write_text("ok")
            test_file.unlink()
            return local_tmp
    except OSError:
        pass

    return Path(tempfile.gettempdir())


def read_param_json(path: Path) -> dict[str, object]:
    """Read and parse a JSON parameter file used by games.

    Args:
        path: Path to the JSON file.

    Returns:
        Parsed JSON object as a dict.

    Raises:
        ValueError: When the file cannot be read or parsed as JSON.
    """
    try:
        with path.open(mode="r", encoding="utf-8") as f:
            result: dict[str, object] = json.load(f)
            return result
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - bubble up
        raise ValueError(f"Failed to parse {path}: {exc}") from exc


def _read_exact(fh: BinaryIO, offset: int, size: int) -> bytes:
    """Read exactly ``size`` bytes from file handle starting at ``offset``.

    Args:
        fh: Binary file-like object supporting seek and read.
        offset: Offset in bytes from the start of the file where read begins.
        size: Number of bytes to read.

    Returns:
        The requested bytes.

    Raises:
        ValueError: If the read returns fewer than ``size`` bytes.
    """
    fh.seek(offset)
    data: bytes = fh.read(size)
    if len(data) != size:
        raise ValueError(f"truncated read at offset {offset} (wanted {size}, got {len(data)})")
    return data
