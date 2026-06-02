"""Archive staging helpers for pack workflows."""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import rarfile

from .archive_backend import configure_rar_backend
from .logging import info
from .pfs import BuildError
from .utils import resolve_temp_root

ARCHIVE_SUFFIXES: set[str] = {".zip", ".rar", ".r00", ".r01", ".r02", ".r03", ".r04", ".001", ".002", ".003"}


def is_archive_path(path: Path) -> bool:
    """Return True when a path looks like a supported archive input.

    Args:
        path: Source path to classify.

    Returns:
        True for supported ZIP and RAR-style archive suffixes.
    """
    suffix: str = path.suffix.lower()
    name: str = path.name.lower()
    return suffix in ARCHIVE_SUFFIXES or name.endswith(".part1.rar")


def _archive_password_bytes(password: str | None) -> bytes | None:
    """Convert an optional password string to archive-library bytes.

    Args:
        password: Optional text password supplied by the user.

    Returns:
        UTF-8 encoded password bytes, or None when no password was provided.
    """
    if password is None or password == "":
        return None
    return password.encode("utf-8")


def _safe_member_path(staging_root: Path, member_name: str) -> Path:
    """Resolve an archive member path and reject traversal outside staging.

    Args:
        staging_root: Temporary extraction directory.
        member_name: Archive member name as stored in the archive.

    Returns:
        Absolute destination path for the member.

    Raises:
        BuildError: If the member path is absolute or escapes staging.
    """
    normalized_name: str = member_name.replace("\\", "/")
    if (
        normalized_name.startswith("/")
        or normalized_name.startswith("../")
        or "/../" in normalized_name
        or normalized_name.endswith("/..")  # e.g. "foo/.." resolves to parent of staging_root
    ):
        raise BuildError(f"Archive member escapes the extraction folder: {member_name}")
    resolved_root: Path = staging_root.resolve()
    destination: Path = (resolved_root / normalized_name).resolve()
    try:
        destination.relative_to(resolved_root)
    except ValueError as exc:
        raise BuildError(f"Archive member escapes the extraction folder: {member_name}") from exc
    return destination


def _extract_zip_archive(*, archive_path: Path, staging_root: Path, password: str | None) -> int:
    """Extract a ZIP archive into staging after validating member paths.

    Args:
        archive_path: Source ZIP archive path.
        staging_root: Temporary extraction directory.
        password: Optional archive password.

    Returns:
        Number of regular file members extracted.

    Raises:
        BuildError: If the archive is invalid, unsafe, or cannot be extracted.
    """
    password_bytes: bytes | None = _archive_password_bytes(password=password)
    extracted_files: int = 0
    try:
        with zipfile.ZipFile(file=archive_path) as archive:
            for member in archive.infolist():
                destination: Path = _safe_member_path(staging_root=staging_root, member_name=member.filename)
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name=member, mode="r", pwd=password_bytes) as source, destination.open(
                    mode="wb"
                ) as target:
                    shutil.copyfileobj(source, target)
                extracted_files += 1
    except RuntimeError as exc:
        raise BuildError("ZIP extraction failed, check the archive password") from exc
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise BuildError(f"ZIP extraction failed: {exc}") from exc
    return extracted_files


def _extract_rar_archive(*, archive_path: Path, staging_root: Path, password: str | None) -> int:
    """Extract a RAR archive into staging after validating member paths.

    Args:
        archive_path: Source RAR archive path.
        staging_root: Temporary extraction directory.
        password: Optional archive password.

    Returns:
        Number of regular file members extracted.

    Raises:
        BuildError: If the archive is invalid, unsafe, or cannot be extracted.
    """
    extracted_files: int = 0
    configure_rar_backend()
    try:
        with rarfile.RarFile(archive_path) as archive:
            members: list[rarfile.RarInfo] = archive.infolist()
            # Validate all member paths up-front before extracting anything.
            validated_paths: dict[str, Path] = {}
            for member in members:
                validated_paths[member.filename] = _safe_member_path(
                    staging_root=staging_root, member_name=member.filename
                )
            # Extract using the already-validated destination paths.
            for member in members:
                destination: Path = validated_paths[member.filename]
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, pwd=password) as source, destination.open(mode="wb") as target:
                    shutil.copyfileobj(source, target)
                extracted_files += 1
    except rarfile.RarCannotExec as exc:
        raise BuildError(
            "RAR extraction requires an external unrar/unar/7z-compatible backend available on PATH"
        ) from exc
    except rarfile.RarWrongPassword as exc:
        raise BuildError("RAR extraction failed, check the archive password") from exc
    except rarfile.NeedFirstVolume as exc:
        raise BuildError(
            "RAR multipart extraction must start from the first volume, usually .part1.rar or .rar"
        ) from exc
    except rarfile.Error as exc:
        raise BuildError(f"RAR extraction failed: {exc}") from exc
    except OSError as exc:
        raise BuildError(f"RAR extraction failed: {exc}") from exc
    return extracted_files


@contextmanager
def stage_archive_source_root(
    *, archive_path: Path, password: str | None, temp_folder: Path | None = None
) -> Iterator[Path]:
    """Extract an archive to a temporary source root for the existing packer.

    Args:
        archive_path: Existing ZIP or RAR source archive.
        password: Optional archive password.
        temp_folder: Optional temporary root for extraction staging.

    Yields:
        Temporary directory containing extracted archive contents.

    Raises:
        BuildError: If the archive path is unsupported, missing, empty, unsafe, or
            cannot be extracted.
    """
    source_archive: Path = archive_path.expanduser().resolve()
    if not source_archive.exists() or not source_archive.is_file():
        raise BuildError(f"--source-archive must be an existing file: {source_archive}")
    if not is_archive_path(source_archive):
        raise BuildError(f"Unsupported archive type: {source_archive.suffix or source_archive.name}")

    temp_root: Path = resolve_temp_root(temp_folder=temp_folder)
    with tempfile.TemporaryDirectory(prefix="mkpfs-archive-", dir=str(temp_root)) as staging_dir_name:
        staging_root: Path = Path(staging_dir_name).resolve()
        info(f"Extracting archive to temporary staging folder: {staging_root}")
        if source_archive.suffix.lower() == ".zip":
            extracted_files: int = _extract_zip_archive(
                archive_path=source_archive,
                staging_root=staging_root,
                password=password,
            )
        else:
            extracted_files = _extract_rar_archive(
                archive_path=source_archive,
                staging_root=staging_root,
                password=password,
            )
        if extracted_files == 0:
            raise BuildError("Archive did not contain any regular files to pack")
        info(f"Archive extraction complete: {extracted_files:,} files staged")
        yield staging_root
