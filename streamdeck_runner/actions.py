"""Execution of button bash commands.

Key callbacks fire on the Stream Deck's internal read thread, so commands must
never run inline there — a slow command would freeze all button input. Instead we
hand commands to a small thread pool and report their outcome through a callback.

Each launch returns a :class:`CommandHandle` so the controller can stop a running
command (a second press of the same key): the command is started in its own process
group (``start_new_session=True``) and killed with ``SIGTERM`` to the whole group so
child processes go down with it.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

log = logging.getLogger(__name__)

# Called with (returncode, killed): the exit status, and whether the command was
# stopped via CommandHandle.kill() rather than finishing on its own.
OnDone = Callable[[int, bool], None]


class CommandHandle:
    """A handle to one running command, allowing it to be killed before it finishes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._killed = False

    def _attach(self, proc: subprocess.Popen) -> bool:
        """Record the process; return ``False`` if ``kill`` already raced ahead of us."""
        with self._lock:
            if self._killed:
                return False
            self._proc = proc
            return True

    def kill(self) -> None:
        """Request termination of the command (and any children) if still running."""
        with self._lock:
            self._killed = True
            proc = self._proc
        _terminate(proc)

    @property
    def killed(self) -> bool:
        with self._lock:
            return self._killed


def _terminate(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class ActionRunner:
    """Runs shell commands off the read thread and reports their exit status."""

    def __init__(self, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="action")

    def run(self, command: str, on_done: OnDone | None = None) -> CommandHandle:
        """Schedule *command* to run asynchronously; returns its handle immediately.

        *on_done* (if given) is invoked from a worker thread once the command ends
        with ``(returncode, killed)``.
        """
        handle = CommandHandle()
        self._pool.submit(self._run, command, handle, on_done)
        return handle

    @staticmethod
    def _run(command: str, handle: CommandHandle, on_done: OnDone | None) -> None:
        log.info("Running: %s", command)
        try:
            proc = subprocess.Popen(  # noqa: S602 - shell is intentional; config is user-owned
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,  # own process group so kill() can take down children
            )
        except Exception:  # never let a bad command take down the worker thread
            log.exception("Failed to launch command: %s", command)
            _notify(on_done, -1, False)
            return

        if not handle._attach(proc):
            # kill() was called before the process was attached; stop it now.
            _terminate(proc)
            proc.communicate()
            _notify(on_done, proc.returncode, True)
            return

        _, stderr = proc.communicate()
        killed = handle.killed
        if proc.returncode != 0 and not killed:
            log.warning(
                "Command exited %d: %s%s",
                proc.returncode,
                command,
                f"\n{stderr.strip()}" if stderr and stderr.strip() else "",
            )
        _notify(on_done, proc.returncode, killed)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def _notify(on_done: OnDone | None, returncode: int, killed: bool) -> None:
    if on_done is None:
        return
    try:
        on_done(returncode, killed)
    except Exception:  # a callback error must never kill the worker thread
        log.exception("on_done callback failed (rc=%d, killed=%s)", returncode, killed)
