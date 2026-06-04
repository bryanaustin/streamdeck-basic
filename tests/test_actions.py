"""Tests for the command runner and its killable process handle.

These run real (tiny, fast) subprocesses — ``true``/``exit``/``sleep`` — so they
exercise launch, exit-code reporting and kill behaviour without any hardware.
"""

import threading

from streamdeck_basic.actions import ActionRunner


class Outcome:
    """Captures a single on_done callback and lets a test wait for it."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.returncode: int | None = None
        self.killed: bool | None = None

    def on_done(self, returncode: int, killed: bool) -> None:
        self.returncode = returncode
        self.killed = killed
        self.done.set()

    def wait(self, timeout: float = 5.0) -> bool:
        return self.done.wait(timeout)


def test_successful_command_reports_zero():
    runner = ActionRunner()
    try:
        outcome = Outcome()
        runner.run("true", on_done=outcome.on_done)
        assert outcome.wait(), "on_done was never called"
        assert outcome.returncode == 0
        assert outcome.killed is False
    finally:
        runner.shutdown()


def test_failing_command_reports_nonzero():
    runner = ActionRunner()
    try:
        outcome = Outcome()
        runner.run("exit 3", on_done=outcome.on_done)
        assert outcome.wait(), "on_done was never called"
        assert outcome.returncode == 3
        assert outcome.killed is False
    finally:
        runner.shutdown()


def test_kill_stops_running_command():
    runner = ActionRunner()
    try:
        outcome = Outcome()
        handle = runner.run("sleep 30", on_done=outcome.on_done)

        # Give the process a moment to start, then stop it.
        assert not outcome.wait(0.3), "command finished before it could be killed"
        handle.kill()

        assert outcome.wait(), "on_done was never called after kill"
        assert outcome.killed is True
        assert handle.killed is True
        # the process is really gone
        assert handle._proc is not None and handle._proc.poll() is not None
    finally:
        runner.shutdown()


def test_run_without_on_done_does_not_raise():
    runner = ActionRunner()
    try:
        handle = runner.run("true")  # on_done is optional
        # nothing to await; just make sure the handle is usable and the worker is fine
        assert handle is not None
        handle.kill()  # killing an already-finished/short command is a no-op
    finally:
        runner.shutdown()
