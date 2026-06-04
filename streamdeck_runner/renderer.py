"""Rendering of button images (icon + text label) into native key bytes.

A button is composed of an optional scaled icon plus an optional text label, then
converted to the device's native image format. Fonts are cached, and missing
images or fonts degrade gracefully (a warning plus a blank/label-only key) rather
than crashing the application.

A button whose image is a multi-frame file (animated GIF/APNG/WebP) renders to a
sequence of :class:`Frame` objects — one native-byte image per source frame, each
tagged with how long it should be shown — which the controller hands to the
animation driver. Static images (and any that fail to load) collapse to a single
frame, so the rest of the pipeline is unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from StreamDeck.ImageHelpers import PILHelper

from .config import Button, Defaults

log = logging.getLogger(__name__)

MIN_FRAME_S = 0.02   # clamp zero-duration frames so the animator never busy-loops
MAX_FRAMES = 600     # soft cap on pre-rendered frames per key to bound memory use


@dataclass(frozen=True)
class Frame:
    image: bytes      # native key bytes (output of PILHelper.to_native_key_format)
    duration: float   # seconds to display this frame (0.0 for a static, single frame)


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
        """Render *button*'s first (or only) frame and return native image bytes."""
        return self.render_frames(deck, button)[0].image

    def render_frames(self, deck: Any, button: Button) -> list[Frame]:
        """Render *button* into one or more native-format frames.

        Static images — and missing or broken ones — yield a single frame. An
        animated image yields one frame per source frame, each carrying the time
        it should be displayed for.
        """
        label = button.label
        icons = self._icon_frames(button)
        if icons is None:
            return [Frame(self._compose(deck, None, label), 0.0)]
        return [Frame(self._compose(deck, icon, label), duration) for icon, duration in icons]

    def _compose(self, deck: Any, icon: Any | None, label: str | None) -> bytes:
        """Scale *icon* (or fill with the background), draw *label*, return native bytes."""
        defaults = self.defaults
        if icon is not None:
            # With a label, reserve the configured margins (which leave room for the
            # text); without one, let the icon fill the whole button.
            margins = list(defaults.margins) if label else [0, 0, 0, 0]
            image = PILHelper.create_scaled_key_image(deck, icon, margins=margins)
        else:
            image = PILHelper.create_key_image(deck, background=defaults.background)

        if label:
            draw = ImageDraw.Draw(image)
            font = self._font(defaults.font, defaults.font_size)
            draw.text(
                (image.width / 2, image.height - 5),
                text=label,
                font=font,
                anchor="ms",
                fill=defaults.text_color,
            )

        return PILHelper.to_native_key_format(deck, image)

    def _icon_frames(self, button: Button) -> list[tuple[Any, float]] | None:
        """Load *button*'s icon as (RGBA image, duration) pairs, or None to fall back.

        Returns ``None`` when there is no image or it cannot be loaded, so the caller
        degrades gracefully to a blank/label-only key.
        """
        if not button.image:
            return None
        try:
            icon = Image.open(button.image)
            animated = button.animate and getattr(icon, "is_animated", False)
            if not animated:
                return [(icon.convert("RGBA"), 0.0)]

            count = getattr(icon, "n_frames", 1)
            if count > MAX_FRAMES:
                log.warning(
                    "Image %r has %d frames; only the first %d will be animated",
                    button.image, count, MAX_FRAMES,
                )
                count = MAX_FRAMES

            fps = button.animation.fps
            frames: list[tuple[Any, float]] = []
            for index in range(count):
                icon.seek(index)
                if fps:
                    duration = 1.0 / fps
                else:
                    # GIF/WebP store per-frame display time in milliseconds.
                    duration = max(icon.info.get("duration", 100) / 1000.0, MIN_FRAME_S)
                # convert() copies the composited current frame, so each entry is
                # an independent image even as we seek the shared handle onward.
                frames.append((icon.convert("RGBA"), duration))
            return frames
        except (OSError, ValueError) as exc:
            log.warning("Could not load image %r for key %d: %s", button.image, button.key, exc)
            return None
