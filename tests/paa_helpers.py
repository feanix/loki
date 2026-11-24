"""Test helpers: build a PAA header the way ImageToPAA lays it out, for fake converters."""

import struct
from pathlib import Path

from PIL import Image


def paa_bytes(width: int, height: int, lzo: bool = True, palette: int = 0) -> bytes:
    """Type tag, CGVA/CXAM/SFFO tag blocks, palette, first mip size, then a little payload."""
    out = b"\x01\xff"
    for name, data in ((b"CGVA", b"\x1e\x1e\xc8\xff"), (b"CXAM", b"\xff\xff\xff\xff"), (b"SFFO", bytes(64))):
        out += b"GGAT" + name + struct.pack("<I", len(data)) + data
    out += struct.pack("<H", palette) + bytes(3 * palette)
    return out + struct.pack("<HH", width | (0x8000 if lzo else 0), height) + b"\x00\x01\x00payload"


def paa_for_png(png: Path, max_size: int | None = None) -> bytes:
    from loki import core

    with Image.open(png) as im:
        return paa_bytes(*core.target_size(*im.size, max_size))
