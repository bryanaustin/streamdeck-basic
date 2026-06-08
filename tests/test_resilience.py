"""Hardware-free tests for the resilience/hardening behaviour.

These cover the failure modes that previously wedged the deck silently:

* a dead HID read thread that the health loop must notice (the deck still
  enumerates as connected but delivers no key events);
* an animator thread that must log-and-stop on an unexpected error instead of
  dying without a trace;
* a command runner that must always resolve a button (fire ``on_done``) even on
  binary output, a draining failure, or a shut-down pool, and must surface
  worker-pool saturation;
* device discovery that must skip a single flaky device rather than abort.
"""

import logging
import threading

import pytest

from streamdeck_basic.actions import ActionRunner
from streamdeck_basic.animation import Animator, Clip
from streamdeck_basic.config import AppConfig, Button, DeviceSel
from streamdeck_basic.controller import DeckController, DeckDisconnected
from streamdeck_basic.renderer import Frame, KeyRenderer


# --- fakes ----------------------------------------------------------------

class _FakeActions:
    def run(self, command, on_done=None):
        return None

    def shutdown(self):
        pass


class _FakeThread:
    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class _HealthDeck:
    """Just enough deck for the controller's health checks."""

    def __init__(self, *, reader_alive=True, connected=True, connected_raises=False):
        if reader_alive is not None:
            self.read_thread = _FakeThread(reader_alive)
        self._connected = connected
        self._connected_raises = connected_raises

    def connected(self) -> bool:
        if self._connected_raises:
            raise OSError("usb subsystem gone")
        return self._connected


class _AnimDeck:
    def __init__(self) -> None:
        self.images: dict[int, bytes] = {}

    def set_key_image(self, key, image) -> None:
        self.images[key] = image

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _GoodDeck:
    def __init__(self, serial: str) -> None:
        self._serial = serial
        self.closed = False

    def open(self) -> None:
        pass

    def get_serial_number(self) -> str:
        return self._serial

    def reset(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _OpenBoomDeck(_GoodDeck):
    def open(self) -> None:
        raise RuntimeError("device wedged on open")


class _FakeManager:
    def __init__(self, decks) -> None:
        self._decks = decks

    def enumerate(self):
        return self._decks


def _controller(**config_overrides) -> DeckController:
    base = dict(
        device=DeviceSel(),
        start_page="main",
        pages={"main": [Button(key=0, label="x")]},
    )
    base.update(config_overrides)
    config = AppConfig(**base)
    return DeckController(config, KeyRenderer(config.defaults), _FakeActions(), threading.Event())


# --- health loop: detecting a silently-dead reader ------------------------

def test_healthy_deck_reports_no_reason():
    ctrl = _controller()
    assert ctrl._unhealthy_reason(_HealthDeck()) is None


def test_dead_reader_is_detected():
    ctrl = _controller()
    reason = ctrl._unhealthy_reason(_HealthDeck(reader_alive=False))
    assert reason is not None and "read thread" in reason


def test_disconnected_flag_is_detected():
    ctrl = _controller()
    ctrl._disconnected.set()
    reason = ctrl._unhealthy_reason(_HealthDeck())
    assert reason is not None and "write failed" in reason


def test_unenumerated_device_is_detected():
    ctrl = _controller()
    reason = ctrl._unhealthy_reason(_HealthDeck(connected=False))
    assert reason is not None and "enumerated" in reason


def test_connected_raising_is_treated_as_unhealthy():
    ctrl = _controller()
    reason = ctrl._unhealthy_reason(_HealthDeck(connected_raises=True))
    assert reason is not None and "health check" in reason


def test_reader_alive_degrades_to_true_without_thread_attr():
    # A deck the library no longer exposes a read_thread for must not be flagged dead.
    ctrl = _controller()
    assert ctrl._reader_alive(_HealthDeck(reader_alive=None)) is True


def test_health_loop_raises_promptly_on_dead_reader():
    ctrl = _controller()
    with pytest.raises(DeckDisconnected, match="read thread"):
        ctrl._health_loop(_HealthDeck(reader_alive=False))


def test_health_loop_returns_when_shutdown_already_set():
    ctrl = _controller()
    ctrl.shutdown.set()
    # Healthy deck + shutdown already requested -> returns without raising.
    ctrl._health_loop(_HealthDeck())


# --- device discovery resilience ------------------------------------------

def test_find_deck_skips_unopenable_device():
    ctrl = _controller()
    bad = _OpenBoomDeck("BAD")
    good = _GoodDeck("GOOD")
    deck, serial = ctrl._find_deck(_FakeManager([bad, good]))
    assert deck is good
    assert serial == "GOOD"
    assert bad.closed is True  # the flaky device was cleaned up, not leaked


# --- animator: crash isolation --------------------------------------------

def test_animator_logs_and_stops_on_unexpected_error(caplog):
    clip = Clip([Frame(b"f0", 0.1), Frame(b"f1", 0.1)], loop=True)
    disconnects: list[int] = []

    def boom_state(_key):
        raise RuntimeError("state lookup blew up")

    anim = Animator(
        _AnimDeck(),
        {("main", 0, "idle"): clip},
        lambda: "main",
        boom_state,
        lambda: disconnects.append(1),
        clock=lambda: 0.0,
    )

    with caplog.at_level(logging.ERROR, logger="streamdeck_basic.animation"):
        anim._run()  # must return, not propagate

    assert anim._stop.is_set()
    assert "Animator thread crashed" in caplog.text
    # An unexpected bug logs and stops; it does not trigger a reconnect storm.
    assert disconnects == []


# --- command runner: always resolves the button ---------------------------

class _Outcome:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.returncode = None
        self.killed = None

    def on_done(self, returncode, killed) -> None:
        self.returncode = returncode
        self.killed = killed
        self.done.set()


def test_binary_output_still_completes():
    # Invalid-UTF-8 bytes on stdout previously crashed text-mode decoding in
    # communicate(), leaving the button stuck 'running'. errors="replace" fixes it.
    runner = ActionRunner()
    try:
        outcome = _Outcome()
        runner.run(r"printf '\377\376\300'", on_done=outcome.on_done)
        assert outcome.done.wait(5), "on_done never fired for binary output"
        assert outcome.returncode == 0
        assert outcome.killed is False
    finally:
        runner.shutdown()


def test_on_done_fires_even_if_communicate_raises(monkeypatch):
    import streamdeck_basic.actions as actions_mod

    class _BoomProc:
        returncode = None

        def communicate(self):
            raise OSError("pipe drained badly")

    monkeypatch.setattr(actions_mod.subprocess, "Popen", lambda *a, **k: _BoomProc())

    runner = ActionRunner()
    try:
        outcome = _Outcome()
        runner.run("anything", on_done=outcome.on_done)
        assert outcome.done.wait(5), "on_done never fired after an internal error"
        assert outcome.returncode == -1
        assert outcome.killed is False
    finally:
        runner.shutdown()


def test_submit_after_shutdown_still_resolves(monkeypatch):
    runner = ActionRunner()
    runner.shutdown()  # pool is now closed; submit() will raise RuntimeError
    outcome = _Outcome()
    runner.run("anything", on_done=outcome.on_done)
    assert outcome.done.wait(2), "on_done never fired for a dropped command"
    assert outcome.returncode == -1


def test_saturated_pool_warns(caplog):
    runner = ActionRunner(max_workers=1)
    try:
        with caplog.at_level(logging.WARNING, logger="streamdeck_basic.actions"):
            blocker = runner.run("sleep 5")    # occupies the only worker
            runner.run("true")                 # must queue -> warn synchronously
        assert any("queued" in r.getMessage() for r in caplog.records)
    finally:
        blocker.kill()
        runner.shutdown()
