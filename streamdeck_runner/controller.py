"""Device lifecycle, button dispatch and the disconnect-resilient supervisor loop.

The Stream Deck library has no hotplug support: when a device is unplugged its
internal read thread dies silently while the object still looks valid, and any
write raises ``TransportError``. This controller therefore owns all resilience:

* a supervisor loop that re-enumerates forever and re-applies the full config on
  every (re)connect, so unplug/replug and suspend/resume just work;
* a health loop that polls ``connected()`` to notice silent disconnects;
* every device write guarded so a disconnect mid-update drops cleanly back to the
  supervisor instead of crashing;
* a key callback whose body is fully guarded so nothing can kill the read thread.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.Transport.Transport import TransportError

from .actions import ActionRunner
from .animation import Animator, Clip
from .config import AppConfig, Button
from .renderer import KeyRenderer

log = logging.getLogger(__name__)


class DeckDisconnected(Exception):
    """Internal signal that the active deck went away and we should re-enumerate."""


class DeckController:
    def __init__(
        self,
        config: AppConfig,
        renderer: KeyRenderer,
        actions: ActionRunner,
        shutdown: threading.Event,
    ) -> None:
        self.config = config
        self.renderer = renderer
        self.actions = actions
        self.shutdown = shutdown
        self._current_page = config.start_page
        self._cache: dict[tuple[str, int], bytes] = {}  # (page, key) -> native image bytes
        self._animations: dict[tuple[str, int], Clip] = {}  # (page, key) -> animated frames
        self._animator: Animator | None = None
        self._blank: bytes | None = None
        self._page_index: dict[str, dict[int, Button]] = {
            name: {b.key: b for b in buttons} for name, buttons in config.pages.items()
        }
        self._disconnected = threading.Event()  # set by the read thread on a failed write

    # --- supervisor -------------------------------------------------------

    def run(self) -> None:
        """Run forever (until shutdown), reconnecting to the deck as needed."""
        manager = DeviceManager()
        timing = self.config.timing
        while not self.shutdown.is_set():
            deck, serial = self._find_deck(manager)
            if deck is None:
                log.info("Waiting for a Stream Deck...")
                self.shutdown.wait(timing.reconnect_interval)
                continue

            self._disconnected.clear()
            try:
                self._setup(deck)
                log.info(
                    "Connected to %s (serial %s, %d keys)",
                    deck.deck_type(), serial, deck.key_count(),
                )
                self._health_loop(deck)
            except (TransportError, DeckDisconnected):
                log.warning("Stream Deck disconnected — will reconnect")
            except Exception:
                log.exception("Unexpected error with the Stream Deck; retrying")
                self.shutdown.wait(timing.reconnect_interval)
            finally:
                self._stop_animator()  # stop writing before the deck is closed
                self._safe_close(deck)

        log.info("Shutting down")

    def _health_loop(self, deck: Any) -> None:
        interval = self.config.timing.poll_interval
        while not self.shutdown.is_set():
            if self._disconnected.is_set() or not deck.connected():
                raise DeckDisconnected("connection lost")
            self.shutdown.wait(interval)

    # --- device discovery & setup ----------------------------------------

    def _find_deck(self, manager: DeviceManager) -> tuple[Any | None, str | None]:
        target = self.config.device.serial
        decks = manager.enumerate()
        for deck in decks:
            try:
                deck.open()
                serial = deck.get_serial_number()
            except TransportError:
                self._safe_close(deck)
                continue
            if target is None or serial == target:
                return deck, serial
            self._safe_close(deck)
        if target is not None and decks:
            log.warning("No Stream Deck with serial %r found among %d device(s)", target, len(decks))
        return None, None

    def _setup(self, deck: Any) -> None:
        deck.reset()
        deck.set_brightness(self.config.brightness)
        self._build_cache(deck)
        deck.set_key_callback(self._on_key)
        self._current_page = self.config.start_page
        self._show_page(deck, self._current_page)
        self._start_animator(deck)

    def _build_cache(self, deck: Any) -> None:
        """Pre-render every page once for this deck so navigation is instant.

        Animated keys also have their full frame sequence stashed in
        ``self._animations`` for the animation driver; the first frame doubles as
        the static cache entry shown on page load and for unused-key fallbacks.
        """
        self._cache.clear()
        self._animations.clear()
        self._blank = self.renderer.blank(deck)
        count = deck.key_count()
        for name, buttons in self.config.pages.items():
            for button in buttons:
                if button.key >= count:
                    log.warning(
                        "page '%s' key %d exceeds device key count %d; skipping",
                        name, button.key, count,
                    )
                    continue
                frames = self.renderer.render_frames(deck, button)
                self._cache[(name, button.key)] = frames[0].image
                if len(frames) > 1:
                    self._animations[(name, button.key)] = Clip(frames, button.animation.loop)

    def _start_animator(self, deck: Any) -> None:
        """(Re)start the animation driver for this deck if any key is animated."""
        self._stop_animator()
        if not self._animations:
            return
        self._animator = Animator(
            deck,
            self._animations,
            lambda: self._current_page,
            self._disconnected.set,
        )
        self._animator.start()

    def _stop_animator(self) -> None:
        if self._animator is not None:
            self._animator.stop()
            self._animator = None

    # --- rendering & input ------------------------------------------------

    def _show_page(self, deck: Any, name: str) -> None:
        count = deck.key_count()
        try:
            with deck:  # hold the device lock for the whole page update
                for key in range(count):
                    deck.set_key_image(key, self._cache.get((name, key), self._blank))
        except TransportError as exc:
            raise DeckDisconnected(str(exc)) from exc
        self._current_page = name
        if self._animator is not None:
            self._animator.notify_page_changed()
        log.info("Showing page '%s'", name)

    def _on_key(self, deck: Any, key: int, state: bool) -> None:
        # Runs on the deck's read thread: guard everything so it can never die.
        try:
            button = self._page_index.get(self._current_page, {}).get(key)
            if button is None:
                return
            edge = "press" if state else "release"
            if button.trigger != edge:
                return
            if button.command:
                self.actions.run(button.command)
            if button.goto:
                self._show_page(deck, button.goto)
        except (TransportError, DeckDisconnected):
            self._disconnected.set()
        except Exception:
            log.exception("Error handling key %d", key)

    # --- teardown ---------------------------------------------------------

    @staticmethod
    def _safe_close(deck: Any | None) -> None:
        if deck is None:
            return
        try:
            deck.reset()
        except Exception:
            pass
        try:
            deck.close()
        except Exception:
            pass
