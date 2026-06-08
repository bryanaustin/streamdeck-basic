"""Device lifecycle, button dispatch and the disconnect-resilient supervisor loop.

The Stream Deck library has no hotplug support: when a device is unplugged its
internal read thread dies silently while the object still looks valid, and any
write raises ``TransportError``. This controller therefore owns all resilience:

* a supervisor loop that re-enumerates forever and re-applies the full config on
  every (re)connect, so unplug/replug and suspend/resume just work;
* a health loop that polls both ``connected()`` and the library's read-thread
  liveness, so a silently-dead reader (which still enumerates as connected but
  delivers no key events) is caught and recovered instead of wedging forever;
* every device write guarded so a disconnect mid-update drops cleanly back to the
  supervisor instead of crashing;
* a key callback whose body is fully guarded so nothing can kill the read thread.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from typing import Any

from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.Transport.Transport import TransportError

from .actions import ActionRunner, CommandHandle
from .animation import Animator, Clip
from .config import AppConfig, Button
from .renderer import KeyRenderer

log = logging.getLogger(__name__)

# Per-button execution states (see _on_command_done for transitions).
IDLE = "idle"
RUNNING = "running"
ERRORED = "errored"
COMPLETED = "completed"


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
        # Caches are keyed by (page, key, state) so each button can show a different
        # image while idle / running / errored / completed.
        self._cache: dict[tuple[str, int, str], bytes] = {}  # -> native image bytes
        self._animations: dict[tuple[str, int, str], Clip] = {}  # -> animated frames
        self._animator: Animator | None = None
        self._blank: bytes | None = None
        self._page_index: dict[str, dict[int, Button]] = {
            name: {b.key: b for b in buttons} for name, buttons in config.pages.items()
        }
        self._disconnected = threading.Event()  # set by the read thread on a failed write
        # Runtime state, guarded by _state_lock (always acquired before the deck lock).
        self._state_lock = threading.RLock()
        self._key_state: dict[tuple[str, int], str] = {}  # (page, key) -> current state
        self._running: dict[tuple[str, int], CommandHandle] = {}  # live commands
        self._deck: Any | None = None  # the connected deck, or None between connections

    # --- supervisor -------------------------------------------------------

    def run(self) -> None:
        """Run forever (until shutdown), reconnecting to the deck as needed."""
        manager = DeviceManager()
        timing = self.config.timing
        while not self.shutdown.is_set():
            try:
                deck, serial = self._find_deck(manager)
            except Exception:
                # Enumeration itself failed (USB subsystem hiccup, hidapi error). This
                # must never kill the supervisor — log it and retry like any disconnect.
                log.exception("Failed to look for a Stream Deck; retrying")
                self.shutdown.wait(timing.reconnect_interval)
                continue

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
            except (TransportError, DeckDisconnected) as exc:
                log.warning("Stream Deck connection lost (%s); reconnecting", exc)
            except Exception:
                log.exception("Unexpected error with the Stream Deck; retrying")
                self.shutdown.wait(timing.reconnect_interval)
            finally:
                self._stop_animator()  # stop writing before the deck is closed
                with self._state_lock:
                    self._deck = None  # late on_done callbacks now skip device writes
                self._safe_close(deck)

        log.info("Shutting down")

    def _health_loop(self, deck: Any) -> None:
        """Block until shutdown or the deck becomes unusable, then raise to reconnect.

        Beyond our own failed-write flag and the library's ``connected()`` (which only
        reports physical USB presence), this watches the library's HID *read* thread.
        That thread catches only ``TransportError`` and dies silently on anything else,
        after which the deck still enumerates as connected yet delivers no key events —
        the classic "unresponsive, nothing logged" wedge. Polling its liveness here is
        what turns that otherwise-silent failure into a logged reconnect.
        """
        interval = self.config.timing.poll_interval
        while not self.shutdown.is_set():
            reason = self._unhealthy_reason(deck)
            if reason is not None:
                raise DeckDisconnected(reason)
            self.shutdown.wait(interval)

    def _unhealthy_reason(self, deck: Any) -> str | None:
        """Return why *deck* is unusable right now, or ``None`` while it is healthy."""
        if self._disconnected.is_set():
            return "a device write failed"
        if not self._reader_alive(deck):
            return "the HID read thread stopped — key input is dead"
        try:
            if not deck.connected():
                return "the device is no longer enumerated"
        except Exception as exc:  # connected() does USB I/O; any failure is its own signal
            return f"health check raised {exc!r}"
        return None

    @staticmethod
    def _reader_alive(deck: Any) -> bool:
        """Whether the library's HID read thread is still running.

        Degrades to ``True`` (assume alive) if the library stops exposing the thread,
        so an internal change can never turn this check into a reconnect loop.
        """
        thread = getattr(deck, "read_thread", None)
        if thread is None:
            return True
        return thread.is_alive()

    # --- device discovery & setup ----------------------------------------

    def _find_deck(self, manager: DeviceManager) -> tuple[Any | None, str | None]:
        target = self.config.device.serial
        decks = manager.enumerate()
        for deck in decks:
            try:
                deck.open()
                serial = deck.get_serial_number()
            except Exception as exc:  # one flaky device must not abort discovery of the rest
                log.debug("Skipping a Stream Deck that failed to open: %s", exc)
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
        # Publish the deck only once its caches are fully built, so a late worker-thread
        # on_done (from a command still running across a reconnect) can't read a
        # half-cleared cache while _build_cache repopulates it.
        with self._state_lock:
            self._deck = deck
        deck.set_key_callback(self._on_key)
        self._current_page = self.config.start_page
        self._show_page(deck, self._current_page)
        self._start_animator(deck)

    def _build_cache(self, deck: Any) -> None:
        """Pre-render every page once for this deck so navigation is instant.

        Each button is rendered for every state it can be in (``idle`` always, plus
        ``running``/``errored``/``completed`` for command buttons). Animated states
        also have their full frame sequence stashed in ``self._animations`` for the
        animation driver; the first frame doubles as the static cache entry shown on
        page load and for unused-key fallbacks.
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
                self._cache_button(deck, name, button)

    def _cache_button(self, deck: Any, name: str, button: Button) -> None:
        """Render and cache every state of *button* on page *name*."""
        idle_frames = self.renderer.render_frames(deck, button)
        self._store_frames(name, button.key, IDLE, idle_frames, button.animation.loop)
        if not button.command:
            return

        # running: configured image, else a generated spinner.
        if button.states.running is not None:
            running = self.renderer.render_frames(
                deck, dataclasses.replace(button, image=button.states.running)
            )
        else:
            running = self.renderer.default_running_frames(deck, button.label)
        self._store_frames(name, button.key, RUNNING, running, button.animation.loop)

        # errored / completed: configured image, else fall back to the idle image.
        for state, image in ((ERRORED, button.states.errored), (COMPLETED, button.states.completed)):
            if image is not None:
                frames = self.renderer.render_frames(
                    deck, dataclasses.replace(button, image=image)
                )
            else:
                frames = idle_frames
            self._store_frames(name, button.key, state, frames, button.animation.loop)

    def _store_frames(self, name: str, key: int, state: str, frames: list, loop: bool) -> None:
        self._cache[(name, key, state)] = frames[0].image
        if len(frames) > 1:
            self._animations[(name, key, state)] = Clip(frames, loop)

    def _start_animator(self, deck: Any) -> None:
        """(Re)start the animation driver for this deck if any key is animated."""
        self._stop_animator()
        if not self._animations:
            return
        self._animator = Animator(
            deck,
            self._animations,
            lambda: self._current_page,
            self._state_of,
            self._disconnected.set,
        )
        self._animator.start()

    def _state_of(self, key: int) -> str:
        """Current state of *key* on the visible page (thread-safe; default IDLE)."""
        with self._state_lock:
            return self._key_state.get((self._current_page, key), IDLE)

    def _stop_animator(self) -> None:
        if self._animator is not None:
            self._animator.stop()
            self._animator = None

    # --- rendering & input ------------------------------------------------

    def _show_page(self, deck: Any, name: str) -> None:
        count = deck.key_count()
        try:
            with self._state_lock:  # state lock before deck lock (consistent ordering)
                with deck:  # hold the device lock for the whole page update
                    for key in range(count):
                        state = self._key_state.get((name, key), IDLE)
                        image = self._cache.get((name, key, state)) or self._blank
                        deck.set_key_image(key, image)
        except TransportError as exc:
            raise DeckDisconnected(str(exc)) from exc
        self._current_page = name
        if self._animator is not None:
            self._animator.notify_page_changed()
        log.info("Showing page '%s'", name)

    def _on_key(self, deck: Any, key: int, state: bool) -> None:
        # Runs on the deck's read thread: guard everything so it can never die.
        try:
            page = self._current_page
            button = self._page_index.get(page, {}).get(key)
            if button is None:
                return
            edge = "press" if state else "release"
            if button.trigger != edge:
                return
            if button.command:
                self._handle_command(deck, page, key, button)
            if button.goto:
                self._show_page(deck, button.goto)
        except (TransportError, DeckDisconnected):
            self._disconnected.set()
        except Exception:
            log.exception("Error handling key %d", key)

    def _handle_command(self, deck: Any, page: str, key: int, button: Button) -> None:
        """Launch the button's command, or stop it if it is already running."""
        key_id = (page, key)
        with self._state_lock:
            handle = self._running.get(key_id)
            if handle is not None:
                handle.kill()  # second press: stop it; on_done will reset to IDLE
                return
            self._running[key_id] = self.actions.run(
                button.command,
                on_done=lambda rc, killed: self._on_command_done(key_id, rc, killed),
            )
            # Apply RUNNING while still holding the lock so an instantly-finishing
            # command's on_done (which also takes the lock) can't be overtaken.
            self._set_state(deck, page, key, RUNNING)

    def _on_command_done(self, key_id: tuple[str, int], returncode: int, killed: bool) -> None:
        """Worker-thread callback: move the button to its post-run state."""
        with self._state_lock:
            self._running.pop(key_id, None)
            if killed:
                new_state = IDLE
            elif returncode == 0:
                new_state = COMPLETED
            else:
                new_state = ERRORED
            deck = self._deck
        self._set_state(deck, key_id[0], key_id[1], new_state)

    def _set_state(self, deck: Any, page: str, key: int, new_state: str) -> None:
        """Record *new_state* and, if its page is visible, push the matching image."""
        with self._state_lock:
            self._key_state[(page, key)] = new_state
            if deck is not None and page == self._current_page:
                image = self._cache.get((page, key, new_state)) or self._blank
                try:
                    with deck:
                        deck.set_key_image(key, image)
                        # Mark the animator dirty while still holding the deck lock so a
                        # stale in-flight frame can't repaint over this state image, and
                        # so it switches to (or drops) this state's animation.
                        if self._animator is not None:
                            self._animator.notify_page_changed()
                except TransportError:
                    self._disconnected.set()

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
