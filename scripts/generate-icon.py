"""Generate MkPFS application icons from scratch using only stdlib.

Outputs:
    assets/icon.png   (256x256 RGBA)
    assets/icon.ico   (multi-resolution ICO with PNG data)
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable
from pathlib import Path


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    chunk_len: bytes = struct.pack(">I", len(data))
    chunk_crc: bytes = struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    return chunk_len + chunk_type + data + chunk_crc


def create_png(width: int, height: int, pixel_func: Callable) -> bytes:
    """Create a PNG from a pixel callback."""
    signature: bytes = b"\x89PNG\r\n\x1a\n"
    ihdr: bytes = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunks: list[bytes] = [signature, _png_chunk(b"IHDR", ihdr)]

    raw_rows: bytearray = bytearray()
    for y in range(height):
        raw_rows.append(0)  # filter: none
        for x in range(width):
            r, g, b, a = pixel_func(x, y, width, height)
            raw_rows.extend([r, g, b, a])

    compressed: bytes = zlib.compress(bytes(raw_rows), level=9)
    chunks.append(_png_chunk(b"IDAT", compressed))
    chunks.append(_png_chunk(b"IEND", b""))
    return b"".join(chunks)


def create_ico(png256: bytes, sizes: list[int] | None = None) -> bytes:
    """Create a Windows ICO embedding the same PNG at multiple sizes."""
    sizes = sizes or [16, 32, 48, 64, 128, 256]
    entries: list[bytes] = []
    images: list[bytes] = []
    offset: int = 6 + len(sizes) * 16
    for size in sizes:
        w: int = size if size < 256 else 0
        h: int = size if size < 256 else 0
        entries.append(struct.pack("BBBBHHII", w, h, 0, 0, 1, 32, len(png256), offset))
        images.append(png256)
        offset += len(png256)
    header: bytes = struct.pack("<HHH", 0, 1, len(sizes))
    return header + b"".join(entries) + b"".join(images)


# Dark blue PlayStation-inspired palette
DARK: tuple[int, int, int] = (26, 35, 126)
MID: tuple[int, int, int] = (48, 63, 159)
LIGHT: tuple[int, int, int] = (92, 107, 192)
ACCENT: tuple[int, int, int] = (3, 169, 244)


def _pixel(x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
    """Render a simple dark-blue disc icon."""
    cx: float = w / 2.0 - 0.5
    cy: float = h / 2.0 - 0.5
    dx: float = x - cx
    dy: float = y - cy
    dist: float = (dx * dx + dy * dy) ** 0.5
    max_r: float = min(w, h) / 2.0

    if dist > max_r:
        return (0, 0, 0, 0)

    # radial gradient: dark -> mid
    t: float = dist / max_r
    r: int = int(DARK[0] * (1 - t) + MID[0] * t)
    g: int = int(DARK[1] * (1 - t) + MID[1] * t)
    b_c: int = int(DARK[2] * (1 - t) + MID[2] * t)

    # inner ring highlight
    ring_inner: float = max_r * 0.35
    ring_outer: float = max_r * 0.45
    if ring_inner < dist < ring_outer:
        blend: float = 1.0 - abs(dist - (ring_inner + ring_outer) / 2.0) / ((ring_outer - ring_inner) / 2.0)
        r = int(r * (1 - blend) + LIGHT[0] * blend)
        g = int(g * (1 - blend) + LIGHT[1] * blend)
        b_c = int(b_c * (1 - blend) + LIGHT[2] * blend)

    # center dot
    if dist < max_r * 0.12:
        r, g, b_c = ACCENT

    return (r, g, b_c, 255)


def main() -> int:
    assets_dir: Path = Path(__file__).resolve().parent.parent / "assets"
    assets_dir.mkdir(exist_ok=True)

    png_data: bytes = create_png(256, 256, _pixel)
    png_path: Path = assets_dir / "icon.png"
    png_path.write_bytes(png_data)
    print(f"Wrote {png_path} ({len(png_data)} bytes)")

    ico_data: bytes = create_ico(png_data)
    ico_path: Path = assets_dir / "icon.ico"
    ico_path.write_bytes(ico_data)
    print(f"Wrote {ico_path} ({len(ico_data)} bytes)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
