"""Generate assets/loki.ico (16-256 px): a green texture tile with a bold white "L".

Usage: python assets/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def draw(size: int = 256) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = size / 16
    d.rounded_rectangle([u * 0.5, u * 0.5, size - u * 0.5, size - u * 0.5], radius=u * 2, fill=(46, 92, 60, 255))
    for i in range(4):  # faint 2x2-style tile grid hinting at a texture
        for j in range(4):
            if (i + j) % 2 == 0:
                d.rectangle([u * (2 + i * 3), u * (2 + j * 3), u * (5 + i * 3) - 1, u * (5 + j * 3) - 1],
                            fill=(58, 112, 74, 255))
    d.rectangle([u * 4.5, u * 3, u * 7.5, u * 13], fill=(255, 255, 255, 255))
    d.rectangle([u * 4.5, u * 10, u * 11.5, u * 13], fill=(255, 255, 255, 255))
    return img


if __name__ == "__main__":
    out = Path(__file__).with_name("loki.ico")
    draw().save(out, format="ICO", sizes=SIZES)
    print(out)
