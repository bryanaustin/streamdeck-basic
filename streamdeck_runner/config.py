"""YAML configuration loading and validation for Stream Deck Runner.

The whole configuration is parsed up front into frozen dataclasses so that the
rest of the application works with validated, typed data and never has to second
guess the YAML. Image paths are resolved relative to the config file's directory
so a config keeps working regardless of the current working directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

_TOP_KEYS = {"brightness", "device", "timing", "defaults", "start_page", "pages"}
_BUTTON_KEYS = {"key", "image", "label", "command", "action", "trigger", "animate", "animation"}


class ConfigError(ValueError):
    """Raised when a configuration file is malformed or internally inconsistent."""


@dataclass(frozen=True)
class Timing:
    poll_interval: float = 1.0       # seconds between connected() health checks
    reconnect_interval: float = 2.0  # seconds between enumerate() retries when no deck


@dataclass(frozen=True)
class DeviceSel:
    serial: str | None = None        # target a specific deck by serial; None = first found


@dataclass(frozen=True)
class Defaults:
    font: str = DEFAULT_FONT
    font_size: int = 14
    text_color: str = "white"
    background: str = "black"
    margins: tuple[int, int, int, int] = (0, 0, 20, 0)  # top, right, bottom, left


@dataclass(frozen=True)
class Animation:
    fps: float | None = None     # None -> use the image's embedded per-frame durations
    loop: bool = True            # repeat forever; False stops on the last frame


@dataclass(frozen=True)
class Button:
    key: int
    image: str | None = None     # absolute path (resolved at load time)
    label: str | None = None
    command: str | None = None   # bash command run via the shell
    goto: str | None = None      # target page name (from `action: {goto: ...}`)
    trigger: str = "press"       # "press" | "release"
    animate: bool = True         # multi-frame images animate unless this is False
    animation: Animation = field(default_factory=Animation)


@dataclass(frozen=True)
class AppConfig:
    brightness: int = 50
    device: DeviceSel = field(default_factory=DeviceSel)
    timing: Timing = field(default_factory=Timing)
    defaults: Defaults = field(default_factory=Defaults)
    start_page: str = "main"
    pages: dict[str, list[Button]] = field(default_factory=dict)
    source_path: str | None = None


def load_config(path: str | os.PathLike) -> AppConfig:
    """Load, validate and return the configuration at *path*.

    Raises :class:`ConfigError` (a ``ValueError`` subclass) with a human readable
    message for any structural problem so the CLI can report it cleanly.
    """
    path = os.fspath(path)
    config_dir = os.path.dirname(os.path.abspath(path))

    try:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping, got {type(raw).__name__}")
    _reject_unknown(raw, _TOP_KEYS, "top level", path)

    brightness = _as_int(raw.get("brightness", 50), "brightness", path)
    if not 0 <= brightness <= 100:
        raise ConfigError(f"{path}: brightness must be between 0 and 100, got {brightness}")

    device = _parse_device(raw.get("device"), path)
    timing = _parse_timing(raw.get("timing"), path)
    defaults = _parse_defaults(raw.get("defaults"), path)

    pages_raw = raw.get("pages")
    if not isinstance(pages_raw, dict) or not pages_raw:
        raise ConfigError(f"{path}: 'pages' must be a non-empty mapping of page name -> button list")

    pages: dict[str, list[Button]] = {}
    for name, buttons in pages_raw.items():
        pages[str(name)] = _parse_page(str(name), buttons, config_dir, path)

    start_page = str(raw.get("start_page", next(iter(pages))))
    if start_page not in pages:
        raise ConfigError(f"{path}: start_page '{start_page}' is not defined in pages")

    for name, buttons in pages.items():
        for button in buttons:
            if button.goto is not None and button.goto not in pages:
                raise ConfigError(
                    f"{path}: page '{name}' key {button.key} navigates to unknown page '{button.goto}'"
                )

    return AppConfig(
        brightness=brightness,
        device=device,
        timing=timing,
        defaults=defaults,
        start_page=start_page,
        pages=pages,
        source_path=path,
    )


# --- helpers --------------------------------------------------------------

def _reject_unknown(mapping: dict, allowed: set[str], where: str, path: str) -> None:
    extra = set(map(str, mapping)) - allowed
    if extra:
        raise ConfigError(f"{path}: unknown key(s) at {where}: {', '.join(sorted(extra))}")


def _as_int(value: object, name: str, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path}: '{name}' must be a number, got {value!r}")
    return int(value)


def _as_float(value: object, name: str, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path}: '{name}' must be a number, got {value!r}")
    return float(value)


def _as_bool(value: object, name: str, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{path}: '{name}' must be true or false, got {value!r}")
    return value


def _parse_device(value: object, path: str) -> DeviceSel:
    if value is None:
        return DeviceSel()
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: 'device' must be a mapping")
    _reject_unknown(value, {"serial"}, "device", path)
    serial = value.get("serial")
    if serial is not None and not isinstance(serial, str):
        raise ConfigError(f"{path}: device.serial must be a string or null")
    return DeviceSel(serial=serial)


def _parse_timing(value: object, path: str) -> Timing:
    if value is None:
        return Timing()
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: 'timing' must be a mapping")
    _reject_unknown(value, {"poll_interval", "reconnect_interval"}, "timing", path)
    poll = _as_float(value.get("poll_interval", 1.0), "timing.poll_interval", path)
    reconnect = _as_float(value.get("reconnect_interval", 2.0), "timing.reconnect_interval", path)
    if poll <= 0 or reconnect <= 0:
        raise ConfigError(f"{path}: timing intervals must be greater than 0")
    return Timing(poll_interval=poll, reconnect_interval=reconnect)


def _parse_defaults(value: object, path: str) -> Defaults:
    if value is None:
        return Defaults()
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: 'defaults' must be a mapping")
    _reject_unknown(
        value, {"font", "font_size", "text_color", "background", "margins"}, "defaults", path
    )
    base = Defaults()
    return Defaults(
        font=str(value.get("font", base.font)),
        font_size=_as_int(value.get("font_size", base.font_size), "defaults.font_size", path),
        text_color=str(value.get("text_color", base.text_color)),
        background=str(value.get("background", base.background)),
        margins=_parse_margins(value.get("margins", list(base.margins)), path),
    )


def _parse_margins(value: object, path: str) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(isinstance(m, bool) or not isinstance(m, int) for m in value)
    ):
        raise ConfigError(
            f"{path}: defaults.margins must be a list of 4 integers [top, right, bottom, left]"
        )
    top, right, bottom, left = (int(m) for m in value)
    return (top, right, bottom, left)


def _parse_page(name: str, buttons: object, config_dir: str, path: str) -> list[Button]:
    if not isinstance(buttons, list):
        raise ConfigError(f"{path}: page '{name}' must be a list of buttons")
    seen: set[int] = set()
    parsed: list[Button] = []
    for entry in buttons:
        button = _parse_button(name, entry, config_dir, path)
        if button.key in seen:
            raise ConfigError(f"{path}: page '{name}' defines key {button.key} more than once")
        seen.add(button.key)
        parsed.append(button)
    return parsed


def _parse_button(page: str, entry: object, config_dir: str, path: str) -> Button:
    if not isinstance(entry, dict):
        raise ConfigError(f"{path}: page '{page}' has a button that is not a mapping: {entry!r}")
    _reject_unknown(entry, _BUTTON_KEYS, f"page '{page}' button", path)

    if "key" not in entry:
        raise ConfigError(f"{path}: page '{page}' has a button without a 'key'")
    key = _as_int(entry["key"], f"page '{page}' button key", path)
    if key < 0:
        raise ConfigError(f"{path}: page '{page}' has a negative key {key}")

    trigger = str(entry.get("trigger", "press"))
    if trigger not in ("press", "release"):
        raise ConfigError(
            f"{path}: page '{page}' key {key} has invalid trigger '{trigger}' (expected press|release)"
        )

    goto = None
    action = entry.get("action")
    if action is not None:
        if not isinstance(action, dict):
            raise ConfigError(f"{path}: page '{page}' key {key} 'action' must be a mapping")
        _reject_unknown(action, {"goto"}, f"page '{page}' key {key} action", path)
        target = action.get("goto")
        goto = None if target is None else str(target)

    image = entry.get("image")
    if image is not None:
        image = str(image)
        if not os.path.isabs(image):
            image = os.path.normpath(os.path.join(config_dir, image))

    animate = _as_bool(entry.get("animate", True), f"page '{page}' key {key} animate", path)
    animation = _parse_animation(entry.get("animation"), page, key, path)

    label = entry.get("label")
    label = None if label is None else str(label)

    command = entry.get("command")
    command = None if command is None else str(command)

    return Button(
        key=key,
        image=image,
        label=label,
        command=command,
        goto=goto,
        trigger=trigger,
        animate=animate,
        animation=animation,
    )


def _parse_animation(value: object, page: str, key: int, path: str) -> Animation:
    if value is None:
        return Animation()
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: page '{page}' key {key} 'animation' must be a mapping")
    _reject_unknown(value, {"fps", "loop"}, f"page '{page}' key {key} animation", path)
    fps = value.get("fps")
    if fps is not None:
        fps = _as_float(fps, f"page '{page}' key {key} animation.fps", path)
        if fps <= 0:
            raise ConfigError(f"{path}: page '{page}' key {key} animation.fps must be greater than 0")
    loop = _as_bool(value.get("loop", True), f"page '{page}' key {key} animation.loop", path)
    return Animation(fps=fps, loop=loop)
