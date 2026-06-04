"""Generate simple placeholder icons so the example config renders out of the box.

Run once after installing dependencies:

    python assets/generate_placeholders.py
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))

# filename -> (background RGB, glyph text)
ICONS = {
    "terminal.png": ((30, 30, 30), ">_"),
    "firefox.png": ((200, 90, 20), "F"),
    "files.png": ((40, 110, 200), "Fi"),
}


def _font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> None:
    font = _font(36)
    for name, (color, text) in ICONS.items():
        image = Image.new("RGBA", (96, 96), (*color, 255))
        draw = ImageDraw.Draw(image)
        draw.text((48, 48), text, font=font, anchor="mm", fill="white")
        out = os.path.join(HERE, name)
        image.save(out)
        print("wrote", out)


if __name__ == "__main__":
    main()
