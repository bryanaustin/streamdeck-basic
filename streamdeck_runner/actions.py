"""Execution of button bash commands.

Key callbacks fire on the Stream Deck's internal read thread, so commands must
never run inline there — a slow command would freeze all button input. Instead we
hand commands to a small thread pool and log their outcome.
"""

from __future__ import annotations

import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)


class ActionRunner:
    """Runs shell commands off the read thread and logs their exit status."""

    def __init__(self, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="action")

    def run(self, command: str) -> None:
        """Schedule *command* to run asynchronously; returns immediately."""
        self._pool.submit(self._run, command)

    @staticmethod
    def _run(command: str) -> None:
        log.info("Running: %s", command)
        try:
            result = subprocess.run(  # noqa: S602 - shell is intentional; config is user-owned
                command,
                shell=True,
                capture_output=True,
                text=True,
            )
        except Exception:  # never let a bad command take down the worker thread
            log.exception("Failed to launch command: %s", command)
            return
        if result.returncode != 0:
            log.warning(
                "Command exited %d: %s%s",
                result.returncode,
                command,
                f"\n{result.stderr.strip()}" if result.stderr.strip() else "",
            )

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
