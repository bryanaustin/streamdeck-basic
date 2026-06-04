"""Hardware-free tests for rendering and button dispatch using a fake deck.

A ``FakeDeck`` implements just enough of the StreamDeck interface for the
renderer (``key_image_format``) and the controller (``set_key_image``,
``key_count``, callbacks, ``with deck:`` ...) so the rendering pipeline and the
press/goto dispatch logic can be exercised without a physical device.
"""

import threading

from streamdeck_runner.config import (
    AppConfig,
    Button,
    Defaults,
    DeviceSel,
    Timing,
)
from streamdeck_runner.controller import DeckController
from streamdeck_runner.renderer import KeyRenderer


class FakeDeck:
    def __init__(self, key_count: int = 15, serial: str = "FAKE123") -> None:
        self._key_count = key_count
        self._serial = serial
        self.images: dict[int, bytes] = {}
        self.brightness: int | None = None
        self.callback = None
        self.reset_calls = 0

    # the renderer needs this; mirrors a Stream Deck MK.2 / Original V2
    def key_image_format(self):
        return {"size": (72, 72), "format": "JPEG", "flip": (True, True), "rotation": 0}

    def key_count(self) -> int:
        return self._key_count

    def key_layout(self):
        return (3, 5)

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def reset(self) -> None:
        self.reset_calls += 1

    def set_brightness(self, percent) -> None:
        self.brightness = percent

    def set_key_callback(self, cb) -> None:
        self.callback = cb

    def set_key_image(self, key, image) -> None:
        self.images[key] = image

    def connected(self) -> bool:
        return True

    def deck_type(self) -> str:
        return "Fake Stream Deck"

    def get_serial_number(self) -> str:
        return self._serial

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeActions:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str) -> None:
        self.commands.append(command)

    def shutdown(self) -> None:
        pass


def make_config(**overrides) -> AppConfig:
    base = dict(
        brightness=42,
        device=DeviceSel(),
        timing=Timing(),
        defaults=Defaults(),
        start_page="main",
        pages={
            "main": [
                Button(key=0, label="Apps", goto="apps"),
                Button(key=1, label="Run", command="echo hi"),
            ],
            "apps": [Button(key=0, label="Back", goto="main")],
        },
    )
    base.update(overrides)
    return AppConfig(**base)


def build(config, deck, actions=None):
    actions = actions or FakeActions()
    controller = DeckController(config, KeyRenderer(config.defaults), actions, threading.Event())
    return controller, actions


def test_renderer_produces_bytes():
    renderer = KeyRenderer(Defaults())
    deck = FakeDeck()
    assert isinstance(renderer.blank(deck), (bytes, bytearray))
    assert isinstance(renderer.render(deck, Button(key=0, label="Hi")), (bytes, bytearray))


def test_setup_applies_brightness_and_renders_start_page():
    deck = FakeDeck(key_count=15)
    controller, _ = build(make_config(), deck)
    controller._setup(deck)
    assert deck.brightness == 42
    assert deck.callback is not None
    # every key on the device is written (configured buttons + blanks)
    assert set(deck.images) == set(range(15))


def test_press_command_button_dispatches():
    deck = FakeDeck()
    controller, actions = build(make_config(), deck)
    controller._setup(deck)
    deck.callback(deck, 1, True)  # press the "Run" button
    assert actions.commands == ["echo hi"]


def test_press_goto_button_switches_page():
    deck = FakeDeck()
    controller, _ = build(make_config(), deck)
    controller._setup(deck)
    assert controller._current_page == "main"
    deck.callback(deck, 0, True)  # press "Apps" -> goto apps
    assert controller._current_page == "apps"


def test_release_trigger_not_fired_on_press():
    config = make_config(
        pages={"main": [Button(key=0, command="boom", trigger="release")]}
    )
    deck = FakeDeck()
    controller, actions = build(config, deck)
    controller._setup(deck)
    deck.callback(deck, 0, True)  # press: should NOT fire
    assert actions.commands == []
    deck.callback(deck, 0, False)  # release: should fire
    assert actions.commands == ["boom"]


def test_out_of_range_key_is_skipped():
    config = make_config(
        pages={"main": [Button(key=0, command="ok"), Button(key=99, command="nope")]}
    )
    deck = FakeDeck(key_count=6)
    controller, _ = build(config, deck)
    controller._build_cache(deck)
    assert ("main", 0) in controller._cache
    assert ("main", 99) not in controller._cache
