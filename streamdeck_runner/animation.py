"""Background driver that advances animated key frames on the visible page.

Animated keys are pre-rendered to native-byte :class:`~streamdeck_runner.renderer.Frame`
sequences at startup, so the only per-tick work here is a single ``set_key_image``
per key whose frame is due. The driver runs on its own daemon thread — never the
StreamDeck read thread and never the action thread pool — and the controller owns
its lifecycle: ``start`` on every (re)connect, ``notify_page_changed`` when the
visible page switches, and ``stop`` before the deck is closed.

All device writes hold the deck lock (``with deck:``) exactly like the controller's
page updates, so the two serialise; a failed write means the deck went away and is
reported straight back to the controller.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from StreamDeck.Transport.Transport import TransportError

from .renderer import Frame

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Clip:
    frames: list[Frame]   # two or more native-byte frames
    loop: bool            # repeat forever, or stop on the last frame


@dataclass
class _State:
    key: int
    clip: Clip
    index: int
    due: float            # monotonic time at which the next frame should be drawn


class Animator:
    """Owns a daemon thread that pushes successive frames to the animated keys."""

    def __init__(
        self,
        deck: Any,
        clips: dict[tuple[str, int], Clip],
        get_page: Callable[[], str],
        on_disconnect: Callable[[], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        min_idle: float = 0.01,
        max_idle: float = 0.5,
    ) -> None:
        self._deck = deck
        self._clips = clips
        self._get_page = get_page
        self._on_disconnect = on_disconnect
        self._clock = clock
        self._min_idle = min_idle
        self._max_idle = max_idle
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="animator", daemon=True)
        self._thread.start()

    def notify_page_changed(self) -> None:
        """Wake the loop so it rebuilds frame state for the now-visible page."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # --- driver loop ------------------------------------------------------

    def _run(self) -> None:
        last_page: str | None = None
        states: dict[int, _State] = {}
        while not self._stop.is_set():
            page = self._get_page()
            if page != last_page:
                states = self._build_states(page)
                last_page = page
            timeout = self._step(states, page)
            self._wake.wait(timeout)
            self._wake.clear()

    def _build_states(self, page: str) -> dict[int, _State]:
        """Fresh per-key state for *page*, each starting from frame 0."""
        now = self._clock()
        states: dict[int, _State] = {}
        for (clip_page, key), clip in self._clips.items():
            if clip_page == page:
                states[key] = _State(key=key, clip=clip, index=0, due=now + clip.frames[0].duration)
        return states

    def _step(self, states: dict[int, _State], page: str) -> float:
        """Draw every key whose frame is due and return how long the loop may idle.

        The returned delay is bounded by ``max_idle`` so a page change is noticed
        promptly even without an explicit wake, and floored by ``min_idle`` so a
        backlog of due frames can never spin the loop.
        """
        if not states:
            return self._max_idle

        now = self._clock()
        due = [state for state in states.values() if now >= state.due]
        if due:
            try:
                with self._deck:
                    # The page may have switched while we waited for the lock; if so,
                    # abandon this write so we never paint a stale frame over the new
                    # page. The loop rebuilds state for the new page on its next pass.
                    if self._get_page() != page:
                        return 0.0
                    for state in due:
                        self._advance(state, now)
            except TransportError:
                self._on_disconnect()
                self._stop.set()
                return 0.0

        next_due = min(state.due for state in states.values())
        return _clamp(next_due - self._clock(), self._min_idle, self._max_idle)

    def _advance(self, state: _State, now: float) -> None:
        """Move *state* to its next frame and write it (called while holding the lock)."""
        frames = state.clip.frames
        if state.index >= len(frames) - 1 and not state.clip.loop:
            state.due = math.inf   # park on the final frame; nothing more to draw
            return
        state.index = (state.index + 1) % len(frames)
        frame = frames[state.index]
        self._deck.set_key_image(state.key, frame.image)
        state.due = now + frame.duration


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))
