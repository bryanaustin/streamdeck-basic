"""Tests for the YAML config loader/validator (no hardware required)."""

import textwrap

import pytest

from streamdeck_runner.config import ConfigError, load_config


def write(tmp_path, text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(text))
    return str(path)


def test_valid_config(tmp_path):
    path = write(
        tmp_path,
        """
        brightness: 40
        start_page: main
        pages:
          main:
            - {key: 0, label: A, action: {goto: other}}
            - {key: 1, image: icons/x.png, command: "true"}
          other:
            - {key: 0, label: Back, action: {goto: main}}
        """,
    )
    cfg = load_config(path)
    assert cfg.brightness == 40
    assert cfg.start_page == "main"
    assert set(cfg.pages) == {"main", "other"}
    assert cfg.pages["main"][0].goto == "other"
    # relative image paths are resolved against the config file directory
    assert cfg.pages["main"][1].image == str(tmp_path / "icons" / "x.png")


def test_defaults_applied(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, command: "true"}
        """,
    )
    cfg = load_config(path)
    assert cfg.brightness == 50
    assert cfg.start_page == "main"
    assert cfg.timing.poll_interval == 1.0
    assert cfg.defaults.margins == (0, 0, 20, 0)


def test_invalid_goto_target(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, action: {goto: nowhere}}
        """,
    )
    with pytest.raises(ConfigError, match="unknown page"):
        load_config(path)


def test_brightness_out_of_range(tmp_path):
    path = write(
        tmp_path,
        """
        brightness: 150
        pages:
          main:
            - {key: 0, command: "true"}
        """,
    )
    with pytest.raises(ConfigError, match="brightness"):
        load_config(path)


def test_missing_start_page(tmp_path):
    path = write(
        tmp_path,
        """
        start_page: home
        pages:
          main:
            - {key: 0, command: "true"}
        """,
    )
    with pytest.raises(ConfigError, match="start_page"):
        load_config(path)


def test_duplicate_key_rejected(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, command: "true"}
            - {key: 0, command: "false"}
        """,
    )
    with pytest.raises(ConfigError, match="more than once"):
        load_config(path)


def test_unknown_button_key_rejected(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, commnd: "typo"}
        """,
    )
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(path)


def test_invalid_trigger_rejected(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, command: "true", trigger: hold}
        """,
    )
    with pytest.raises(ConfigError, match="trigger"):
        load_config(path)


def test_animation_defaults(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, image: spinner.gif}
        """,
    )
    button = load_config(path).pages["main"][0]
    assert button.animate is True
    assert button.animation.fps is None
    assert button.animation.loop is True


def test_animation_block_parsed(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, image: spinner.gif, animation: {fps: 15, loop: false}}
        """,
    )
    button = load_config(path).pages["main"][0]
    assert button.animation.fps == 15.0
    assert button.animation.loop is False


def test_animate_false_parsed(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, image: spinner.gif, animate: false}
        """,
    )
    assert load_config(path).pages["main"][0].animate is False


def test_animation_fps_must_be_positive(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, image: spinner.gif, animation: {fps: 0}}
        """,
    )
    with pytest.raises(ConfigError, match="fps"):
        load_config(path)


def test_animation_unknown_key_rejected(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, image: spinner.gif, animation: {speed: 5}}
        """,
    )
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(path)


def test_animate_must_be_bool(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, image: spinner.gif, animate: "yes"}
        """,
    )
    with pytest.raises(ConfigError, match="true or false"):
        load_config(path)


def test_states_parsed_and_paths_resolved(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - key: 0
              command: "make"
              image: idle.png
              states:
                running:   {image: spin.gif}
                errored:   {image: icons/err.png}
                completed: {image: ok.png}
        """,
    )
    button = load_config(path).pages["main"][0]
    assert button.states.running == str(tmp_path / "spin.gif")
    assert button.states.errored == str(tmp_path / "icons" / "err.png")
    assert button.states.completed == str(tmp_path / "ok.png")


def test_states_defaults_to_empty(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, command: "make"}
        """,
    )
    states = load_config(path).pages["main"][0].states
    assert states.running is None
    assert states.errored is None
    assert states.completed is None


def test_states_without_command_rejected(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, image: idle.png, states: {running: {image: spin.gif}}}
        """,
    )
    with pytest.raises(ConfigError, match="no 'command'"):
        load_config(path)


def test_states_unknown_state_rejected(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, command: "make", states: {paused: {image: p.png}}}
        """,
    )
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(path)


def test_states_unknown_inner_key_rejected(tmp_path):
    path = write(
        tmp_path,
        """
        pages:
          main:
            - {key: 0, command: "make", states: {running: {icon: p.png}}}
        """,
    )
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(path)
