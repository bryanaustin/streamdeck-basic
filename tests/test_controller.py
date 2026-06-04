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
    States,
    Timing,
)
from streamdeck_runner.controller import (
    COMPLETED,
    ERRORED,
    IDLE,
    RUNNING,
    DeckController,
)
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


class FakeHandle:
    def __init__(self) -> None:
        self.killed = False

    def kill(self) -> None:
        self.killed = True


class FakeActions:
    """Records launches and captures the on_done callbacks so tests can drive them."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.handles: list[FakeHandle] = []
        self.on_dones: list = []

    def run(self, command: str, on_done=None) -> FakeHandle:
        self.commands.append(command)
        self.on_dones.append(on_done)
        handle = FakeHandle()
        self.handles.append(handle)
        return handle

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


def test_image_without_label_fills_button(tmp_path, monkeypatch):
    from PIL import Image as PILImage

    from streamdeck_runner import renderer as renderer_mod

    icon_path = tmp_path / "icon.png"
    PILImage.new("RGBA", (200, 200), (255, 0, 0, 255)).save(icon_path)

    captured: list[list[int]] = []
    original = renderer_mod.PILHelper.create_scaled_key_image

    def spy(deck, image, margins):
        captured.append(list(margins))
        return original(deck, image, margins=margins)

    monkeypatch.setattr(renderer_mod.PILHelper, "create_scaled_key_image", spy)

    renderer = KeyRenderer(Defaults())  # default margins reserve 20px at the bottom
    deck = FakeDeck()

    # No label -> icon uses the whole button (zero margins).
    renderer.render(deck, Button(key=0, image=str(icon_path)))
    assert captured[-1] == [0, 0, 0, 0]

    # With a label -> configured margins are kept so the text has room.
    renderer.render(deck, Button(key=0, image=str(icon_path), label="Hi"))
    assert captured[-1] == [0, 0, 20, 0]


def test_setup_applies_brightness_and_renders_start_page():
    deck = FakeDeck(key_count=15)
    controller, _ = build(make_config(), deck)
    controller._setup(deck)
    try:
        assert deck.brightness == 42
        assert deck.callback is not None
        # every key on the device is written (configured buttons + blanks)
        assert set(deck.images) == set(range(15))
    finally:
        controller._stop_animator()


def test_press_goto_button_switches_page():
    deck = FakeDeck()
    controller, _ = build(make_config(), deck)
    controller._setup(deck)
    try:
        assert controller._current_page == "main"
        deck.callback(deck, 0, True)  # press "Apps" -> goto apps
        assert controller._current_page == "apps"
    finally:
        controller._stop_animator()


def test_release_trigger_not_fired_on_press():
    config = make_config(
        pages={"main": [Button(key=0, command="boom", trigger="release")]}
    )
    deck = FakeDeck()
    controller, actions = build(config, deck)
    controller._setup(deck)
    try:
        deck.callback(deck, 0, True)  # press: should NOT fire
        assert actions.commands == []
        deck.callback(deck, 0, False)  # release: should fire
        assert actions.commands == ["boom"]
    finally:
        controller._stop_animator()


def test_out_of_range_key_is_skipped():
    config = make_config(
        pages={"main": [Button(key=0, command="ok"), Button(key=99, command="nope")]}
    )
    deck = FakeDeck(key_count=6)
    controller, _ = build(config, deck)
    controller._build_cache(deck)
    assert ("main", 0, IDLE) in controller._cache
    assert ("main", 99, IDLE) not in controller._cache


def _write_gif(path, count=3):
    from PIL import Image as PILImage

    frames = [PILImage.new("RGBA", (32, 32), (i * 60, 0, 0, 255)) for i in range(count)]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=80, loop=0)
    return str(path)


def test_static_config_has_no_animator():
    # A config with no command buttons and no animated images stays animator-free.
    config = make_config(
        pages={
            "main": [Button(key=0, label="Apps", goto="apps")],
            "apps": [Button(key=0, label="Back", goto="main")],
        }
    )
    deck = FakeDeck()
    controller, _ = build(config, deck)
    controller._setup(deck)
    assert controller._animations == {}
    assert controller._animator is None


def test_animated_button_builds_clip_and_starts_animator(tmp_path):
    gif = _write_gif(tmp_path / "spin.gif", count=3)
    config = make_config(pages={"main": [Button(key=0, image=gif)]})
    deck = FakeDeck()
    controller, _ = build(config, deck)

    controller._build_cache(deck)
    assert ("main", 0, IDLE) in controller._cache       # first frame is the static entry
    assert len(controller._animations[("main", 0, IDLE)].frames) == 3

    controller._setup(deck)
    try:
        assert controller._animator is not None
    finally:
        controller._stop_animator()
    assert controller._animator is None


# --- per-button execution states -----------------------------------------

def _png(path, color) -> str:
    from PIL import Image as PILImage

    PILImage.new("RGBA", (32, 32), color).save(path)
    return str(path)


def _prep(controller, deck, page="main"):
    """Build the cache and wire up the deck without starting the animator."""
    controller._build_cache(deck)
    controller._deck = deck
    controller._current_page = page


def test_press_command_button_dispatches():
    deck = FakeDeck()
    controller, actions = build(make_config(), deck)
    _prep(controller, deck)
    controller._on_key(deck, 1, True)  # press the "Run" button
    assert actions.commands == ["echo hi"]


def test_default_running_spinner_generated_when_no_image():
    deck = FakeDeck()
    controller, _ = build(make_config(), deck)  # key 1 has a command, no running image
    controller._build_cache(deck)
    assert ("main", 1, RUNNING) in controller._cache
    clip = controller._animations.get(("main", 1, RUNNING))
    assert clip is not None and len(clip.frames) > 1  # spinner is animated


def test_errored_and_completed_fall_back_to_idle_image_when_unset():
    deck = FakeDeck()
    controller, _ = build(make_config(), deck)  # key 1 has no state images
    controller._build_cache(deck)
    idle = controller._cache[("main", 1, IDLE)]
    assert controller._cache[("main", 1, ERRORED)] == idle
    assert controller._cache[("main", 1, COMPLETED)] == idle


def test_command_press_shows_running_then_completed(tmp_path):
    config = make_config(
        pages={
            "main": [
                Button(
                    key=0,
                    command="run",
                    states=States(
                        errored=_png(tmp_path / "err.png", (255, 0, 0, 255)),
                        completed=_png(tmp_path / "ok.png", (0, 255, 0, 255)),
                    ),
                )
            ]
        }
    )
    deck = FakeDeck()
    controller, actions = build(config, deck)
    _prep(controller, deck)

    controller._on_key(deck, 0, True)  # press -> running
    assert actions.commands == ["run"]
    assert ("main", 0) in controller._running
    assert deck.images[0] == controller._cache[("main", 0, RUNNING)]

    actions.on_dones[0](0, False)  # command exits 0 -> completed
    assert ("main", 0) not in controller._running
    assert controller._key_state[("main", 0)] == COMPLETED
    assert deck.images[0] == controller._cache[("main", 0, COMPLETED)]


def test_command_nonzero_exit_shows_errored(tmp_path):
    config = make_config(
        pages={
            "main": [
                Button(
                    key=0,
                    command="run",
                    states=States(errored=_png(tmp_path / "err.png", (255, 0, 0, 255))),
                )
            ]
        }
    )
    deck = FakeDeck()
    controller, actions = build(config, deck)
    _prep(controller, deck)

    controller._on_key(deck, 0, True)
    actions.on_dones[0](1, False)  # non-zero exit -> errored
    assert controller._key_state[("main", 0)] == ERRORED
    assert deck.images[0] == controller._cache[("main", 0, ERRORED)]


def test_second_press_kills_running_and_does_not_relaunch():
    deck = FakeDeck()
    controller, actions = build(make_config(), deck)
    _prep(controller, deck)

    controller._on_key(deck, 1, True)  # start
    handle = actions.handles[0]
    assert ("main", 1) in controller._running
    assert handle.killed is False

    controller._on_key(deck, 1, True)  # second press -> kill, no relaunch
    assert handle.killed is True
    assert len(actions.commands) == 1

    actions.on_dones[0](-15, True)  # worker reports it was killed -> back to idle
    assert ("main", 1) not in controller._running
    assert controller._key_state[("main", 1)] == IDLE
    assert deck.images[1] == controller._cache[("main", 1, IDLE)]


def test_completed_state_survives_page_navigation(tmp_path):
    config = make_config(
        pages={
            "main": [
                Button(key=0, label="Apps", goto="apps"),
                Button(
                    key=1,
                    command="run",
                    states=States(completed=_png(tmp_path / "ok.png", (0, 255, 0, 255))),
                ),
            ],
            "apps": [Button(key=0, label="Back", goto="main")],
        }
    )
    deck = FakeDeck()
    controller, actions = build(config, deck)
    _prep(controller, deck)

    controller._on_key(deck, 1, True)
    actions.on_dones[0](0, False)  # -> completed
    controller._show_page(deck, "apps")  # navigate away
    controller._show_page(deck, "main")  # ...and back
    assert deck.images[1] == controller._cache[("main", 1, COMPLETED)]
