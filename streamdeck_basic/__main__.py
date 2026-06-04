"""Command line entry point for Stream Deck Basic."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from .actions import ActionRunner
from .config import ConfigError, load_config
from .controller import DeckController
from .renderer import KeyRenderer

log = logging.getLogger("streamdeck_basic")

# Searched in order when --config is not given.
_CONFIG_SEARCH = [
    os.environ.get("STREAMDECK_CONFIG"),
    "config.yaml",
    os.path.expanduser("~/.config/streamdeck/config.yaml"),
]


def _resolve_config(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for candidate in _CONFIG_SEARCH:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="streamdeck-basic",
        description="Drive an Elgato Stream Deck from a YAML config (Linux).",
    )
    parser.add_argument("-c", "--config", help="Path to the YAML config file")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    config_path = _resolve_config(args.config)
    if not config_path:
        parser.error(
            "No config file found. Pass --config PATH, or create ./config.yaml "
            "or ~/.config/streamdeck/config.yaml"
        )

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    log.info("Loaded config from %s (%d page(s))", config_path, len(config.pages))

    shutdown = threading.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        log.info("Received %s; shutting down", signal.Signals(signum).name)
        shutdown.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    renderer = KeyRenderer(config.defaults)
    actions = ActionRunner()
    controller = DeckController(config, renderer, actions, shutdown)

    try:
        controller.run()
    finally:
        actions.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
