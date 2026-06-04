"""Generate simple placeholder icons so the example config renders out of the box.

Run once after installing dependencies:

    python assets/generate_placeholders.py
"""

from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))

# filename -> (background RGB, glyph text)
ICONS = {
    "terminal.png": ((30, 30, 30), ">_"),
    "firefox.png": ((200, 90, 20), "F"),
    "files.png": ((40, 110, 200), "Fi"),
    "error.png": ((170, 0, 0), "✗"),   # errored-state placeholder (red cross)
    "ok.png": ((17, 136, 17), "✓"),    # completed-state placeholder (green check)
}

SPINNER_FRAMES = 12          # frames in the animated placeholder
SPINNER_DELAY_MS = 80        # per-frame delay baked into the GIF


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


def _spinner_frame(angle: float) -> Image.Image:
    """One frame of a simple rotating-dot spinner, used to demo animation."""
    size = 96
    image = Image.new("RGBA", (size, size), (20, 20, 20, 255))
    draw = ImageDraw.Draw(image)
    cx = cy = size / 2
    for i in range(8):
        theta = angle + i * (math.pi / 4)
        x = cx + math.cos(theta) * 30
        y = cy + math.sin(theta) * 30
        shade = int(60 + 195 * (i / 7))  # trailing fade so rotation reads clearly
        r = 7
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(shade, shade, shade, 255))
    return image


def main() -> None:
    font = _font(36)
    for name, (color, text) in ICONS.items():
        image = Image.new("RGBA", (96, 96), (*color, 255))
        draw = ImageDraw.Draw(image)
        draw.text((48, 48), text, font=font, anchor="mm", fill="white")
        out = os.path.join(HERE, name)
        image.save(out)
        print("wrote", out)

    frames = [_spinner_frame(2 * math.pi * i / SPINNER_FRAMES) for i in range(SPINNER_FRAMES)]
    out = os.path.join(HERE, "spinner.gif")
    frames[0].save(
        out, save_all=True, append_images=frames[1:], duration=SPINNER_DELAY_MS, loop=0
    )
    print("wrote", out)


if __name__ == "__main__":
    main()
