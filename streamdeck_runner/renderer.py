"""Rendering of button images (icon + text label) into native key bytes.

A button is composed of an optional scaled icon plus an optional text label, then
converted to the device's native image format. Fonts are cached, and missing
images or fonts degrade gracefully (a warning plus a blank/label-only key) rather
than crashing the application.
"""

from __future__ import annotations

import logging
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from StreamDeck.ImageHelpers import PILHelper

from .config import Button, Defaults

log = logging.getLogger(__name__)


class KeyRenderer:
    def __init__(self, defaults: Defaults) -> None:
        self.defaults = defaults
        self._font_cache: dict[tuple[str, int], Any] = {}
        self._warned_fonts: set[str] = set()

    def _font(self, path: str, size: int) -> Any:
        cache_key = (path, size)
        cached = self._font_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            font = ImageFont.truetype(path, size)
        except OSError:
            if path not in self._warned_fonts:
                log.warning("Font %r not found; falling back to the PIL default font", path)
                self._warned_fonts.add(path)
            font = ImageFont.load_default()
        self._font_cache[cache_key] = font
        return font

    def blank(self, deck: Any) -> bytes:
        """A key filled with the default background — used for unused positions."""
        image = PILHelper.create_key_image(deck, background=self.defaults.background)
        return PILHelper.to_native_key_format(deck, image)

    def render(self, deck: Any, button: Button) -> bytes:
        """Render *button* for *deck* and return native image bytes."""
        defaults = self.defaults
        image = None

        if button.image:
            try:
                icon = Image.open(button.image).convert("RGBA")
                image = PILHelper.create_scaled_key_image(deck, icon, margins=list(defaults.margins))
            except (OSError, ValueError) as exc:
                log.warning("Could not load image %r for key %d: %s", button.image, button.key, exc)

        if image is None:
            image = PILHelper.create_key_image(deck, background=defaults.background)

        if button.label:
            draw = ImageDraw.Draw(image)
            font = self._font(defaults.font, defaults.font_size)
            draw.text(
                (image.width / 2, image.height - 5),
                text=button.label,
                font=font,
                anchor="ms",
                fill=defaults.text_color,
            )

        return PILHelper.to_native_key_format(deck, image)
