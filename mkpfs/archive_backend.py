"""Runtime helpers for locating archive decompression backends."""

from __future__ import annotations

import os
import platform
import stat
import sys
import tarfile
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path

import rarfile

from .logging import info
from .pfs import BuildError
from .utils import _app_directory

SEVENZIP_VERSION: str = "2501"
SEVENZIP_BASE_URL: str = "https://www.7-zip.org/a"
SEVENZIP_PLATFORM_ARCHIVES: dict[tuple[str, str], str] = {
    ("Darwin", "arm64"): f"7z{SEVENZIP_VERSION}-mac.tar.xz",
    ("Darwin", "x86_64"): f"7z{SEVENZIP_VERSION}-mac.tar.xz",
    ("Linux", "x86_64"): f"7z{SEVENZIP_VERSION}-linux-x64.tar.xz",
    ("Linux", "aarch64"): f"7z{SEVENZIP_VERSION}-linux-arm64.tar.xz",
}


def _candidate_binary_names() -> tuple[str, ...]:
    """Return archive backend executable names to try for the current platform.

    Returns:
        Candidate executable filenames, ordered by preference.
    """
    if platform.system() == "Windows":
        return "7zz.exe", "7z.exe", "unrar.exe", "unar.exe"
    return "7zz", "7z", "unrar", "unar"


def _runtime_binary_dirs() -> list[Path]:
    """Return app-local folders that may contain bundled decompression tools.

    Returns:
        Candidate binary directories for source, wheel, and frozen application runs.
    """
    dirs: list[Path] = []
    package_dir: Path = Path(__file__).resolve().parent
    dirs.append(package_dir / "bin")
    if getattr(sys, "frozen", False):
        executable_dir: Path = Path(sys.executable).resolve().parent
        dirs.append(executable_dir)
        dirs.append(executable_dir / "bin")
        meipass: str | None = getattr(sys, "_MEIPASS", None)
        if meipass is not None:
            dirs.append(Path(meipass) / "bin")
    return dirs


def _find_existing_backend() -> Path | None:
    """Return an existing archive backend from app folders or PATH.

    Returns:
        Absolute executable path when one is found, otherwise None.
    """
    for directory in _runtime_binary_dirs():
        for name in _candidate_binary_names():
            candidate: Path = directory / name
            if candidate.is_file():
                return candidate.resolve()

    for name in _candidate_binary_names():
        resolved: str | None = shutil_which(name=name)
        if resolved is not None:
            return Path(resolved).resolve()
    return None


def shutil_which(*, name: str) -> str | None:
    """Wrap shutil.which using a small import boundary for testability.

    Args:
        name: Executable name to locate on PATH.

    Returns:
        Resolved executable path, or None when not found.
    """
    from shutil import which

    return which(name)


def _backend_cache_dir() -> Path:
    """Return the cache directory used for downloaded decompression tools.

    Prefers a subfolder inside the app directory so the backend stays with
    the application. Falls back to per-user system caches when the app
    directory is not writable.

    Returns:
        Writable directory path for cached archive backends.
    """
    override: str | None = os.environ.get("MKPFS_ARCHIVE_BACKEND_DIR")
    if override:
        return Path(override).expanduser().resolve()

    app_cache: Path = _app_directory() / "archive-backends"
    try:
        app_cache.mkdir(parents=True, exist_ok=True)
        test_file: Path = app_cache / ".write_test"
        test_file.write_text("ok")
        with suppress(OSError):
            test_file.unlink()
        return app_cache
    except (OSError, PermissionError):
        pass

    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Caches" / "mkpfs" / "archive-backends"
    if platform.system() == "Windows":
        appdata: str | None = os.environ.get("LOCALAPPDATA")
        if appdata:
            return Path(appdata) / "mkpfs" / "archive-backends"
    cache_home: str = os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
    return Path(cache_home) / "mkpfs" / "archive-backends"


def _downloadable_archive_name() -> str:
    """Return the 7-Zip archive name matching the current platform.

    Returns:
        7-Zip archive filename for the current OS and architecture.

    Raises:
        BuildError: If no portable 7-Zip build is configured for this platform.
    """
    system_name: str = platform.system()
    machine_name: str = platform.machine().lower()
    if machine_name in {"amd64", "x86_64"}:
        arch_name: str = "x86_64"
    elif machine_name in {"arm64", "aarch64"}:
        arch_name = "arm64" if system_name == "Darwin" else "aarch64"
    else:
        arch_name = machine_name

    archive_name: str | None = SEVENZIP_PLATFORM_ARCHIVES.get((system_name, arch_name))
    if archive_name is None:
        raise BuildError(
            "RAR extraction requires unrar/unar/7z. Automatic 7-Zip download is not configured "
            f"for {system_name} {machine_name}. Install one of those tools or bundle it with the app."
        )
    return archive_name


def _extract_downloaded_backend(*, archive_path: Path, destination_dir: Path) -> Path:
    """Extract a downloaded 7-Zip archive and return the 7zz executable path.

    Args:
        archive_path: Downloaded tar archive path.
        destination_dir: Directory where the backend should be extracted.

    Returns:
        Path to the extracted 7zz executable.

    Raises:
        BuildError: If the archive cannot be extracted or does not contain 7zz.
    """
    try:
        with tarfile.open(name=archive_path, mode="r:xz") as archive:
            if sys.version_info >= (3, 12):
                archive.extractall(path=destination_dir, filter="data")
            else:
                archive.extractall(path=destination_dir)
    except (tarfile.TarError, OSError) as exc:
        raise BuildError(f"Failed to extract downloaded 7-Zip backend: {exc}") from exc

    binary_name: str = "7zz.exe" if platform.system() == "Windows" else "7zz"
    backend_path: Path = destination_dir / binary_name
    if not backend_path.is_file():
        matches: list[Path] = list(destination_dir.rglob(binary_name))
        if not matches:
            raise BuildError("Downloaded 7-Zip backend did not contain the expected 7zz executable")
        backend_path = matches[0]

    if platform.system() != "Windows":
        current_mode: int = backend_path.stat().st_mode
        backend_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return backend_path.resolve()


def _download_backend() -> Path:
    """Download a portable 7-Zip backend and return the executable path.

    Returns:
        Path to a cached 7zz executable.

    Raises:
        BuildError: If download or extraction fails.
    """
    archive_name: str = _downloadable_archive_name()
    cache_dir: Path = _backend_cache_dir()
    version_dir: Path = cache_dir / SEVENZIP_VERSION
    binary_name: str = "7zz.exe" if platform.system() == "Windows" else "7zz"
    cached_backend: Path = version_dir / binary_name
    if cached_backend.is_file():
        return cached_backend.resolve()

    for found in version_dir.rglob(binary_name):
        if found.is_file():
            return found.resolve()

    version_dir.mkdir(parents=True, exist_ok=True)
    archive_path: Path = version_dir / archive_name
    download_url: str = f"{SEVENZIP_BASE_URL}/{archive_name}"
    info(f"Downloading 7-Zip backend for RAR extraction: {download_url}")
    try:
        urllib.request.urlretrieve(download_url, archive_path)
    except (OSError, urllib.error.URLError) as exc:
        raise BuildError(f"Failed to download 7-Zip backend: {exc}") from exc
    try:
        return _extract_downloaded_backend(archive_path=archive_path, destination_dir=version_dir)
    finally:
        # Remove the downloaded archive whether extraction succeeded or not.
        # A failed extraction leaves a corrupt file that would block future
        # download attempts (the cached-binary check runs before downloading).
        with suppress(OSError):
            archive_path.unlink(missing_ok=True)


def configure_rar_backend() -> Path:
    """Configure rarfile to use an available or downloaded decompression backend.

    Returns:
        Path to the backend executable configured for rarfile.

    Raises:
        BuildError: If no backend can be found or downloaded.
    """
    backend_path: Path | None = _find_existing_backend()
    if backend_path is None:
        backend_path = _download_backend()

    backend_name: str = backend_path.name.lower()
    if backend_name.startswith("7zz"):
        rarfile.SEVENZIP2_TOOL = str(backend_path)
        rarfile.CURRENT_SETUP = None
    elif backend_name.startswith("7z"):
        rarfile.SEVENZIP_TOOL = str(backend_path)
        rarfile.CURRENT_SETUP = None
    elif backend_name.startswith("unrar"):
        rarfile.UNRAR_TOOL = str(backend_path)
        rarfile.CURRENT_SETUP = None
    elif backend_name.startswith("unar"):
        rarfile.UNAR_TOOL = str(backend_path)
        rarfile.CURRENT_SETUP = None
    else:
        raise BuildError(f"Unsupported RAR backend executable: {backend_path}")

    info(f"Using RAR decompression backend: {backend_path}")
    return backend_path
