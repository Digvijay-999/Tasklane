"""The dev reload runner (devtools/reload.py), run as a real process with a real child."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]

# A child that records its lifecycle in a log file. On SIGTERM it takes `TERM_DELAY` seconds to
# finish (like a Celery worker completing its current task), then exits cleanly.
CHILD = """
import os, signal, sys, time
log = sys.argv[1]
delay = float(sys.argv[2])
exit_code = int(sys.argv[3])
def write(line):
    with open(log, "a") as f:
        f.write(line + "\\n")
def on_term(signum, frame):
    write("term")
    time.sleep(delay)
    write("clean-exit")
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
write("started")
if exit_code:
    sys.exit(exit_code)
while True:
    time.sleep(0.1)
"""


def wait_for(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError(what)


@pytest.fixture
def runner(tmp_path):
    child = tmp_path / "child.py"
    child.write_text(CHILD)
    watched = tmp_path / "watched"
    watched.mkdir()
    log = tmp_path / "child.log"
    processes = []

    def start(*, grace: float, term_delay: float = 0.0, exit_code: int = 0):
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "devtools.reload",
                "--path",
                str(watched),
                "--grace",
                str(grace),
                "--",
                sys.executable,
                str(child),
                str(log),
                str(term_delay),
                str(exit_code),
            ],
            cwd=BACKEND,
            env={**os.environ, "WATCHFILES_FORCE_POLLING": "true"},
        )
        processes.append(process)
        return process

    def lines() -> list[str]:
        return log.read_text().split() if log.exists() else []

    yield start, lines, watched
    for process in processes:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_sigterm_lets_the_child_finish_for_longer_than_five_seconds(runner):
    """Regression test for the watchfiles CLI, which kills the child after 5 s on SIGTERM."""
    start, lines, _ = runner
    process = start(grace=15, term_delay=6)
    wait_for(lambda: lines() == ["started"], 15, "child started")

    started = time.monotonic()
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=20) == 0
    assert time.monotonic() - started >= 6
    assert lines() == ["started", "term", "clean-exit"]  # finished, not killed


def test_child_that_ignores_the_grace_period_is_killed(runner):
    start, lines, _ = runner
    process = start(grace=1, term_delay=30)
    wait_for(lambda: lines() == ["started"], 15, "child started")

    started = time.monotonic()
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)
    assert time.monotonic() - started < 5
    assert lines() == ["started", "term"]  # killed before finishing


def test_runner_exits_when_the_child_exits_on_its_own(runner):
    """So Docker's restart policy restarts the container instead of it idling silently."""
    start, lines, _ = runner
    process = start(grace=5, exit_code=3)
    assert process.wait(timeout=15) == 3


def test_python_file_change_restarts_the_child_gracefully(runner):
    start, lines, watched = runner
    process = start(grace=5)
    wait_for(lambda: lines() == ["started"], 15, "child started")

    (watched / "module.py").write_text("x = 1\n")
    wait_for(lambda: lines().count("started") == 2, 20, "child restarted")
    assert lines()[:3] == ["started", "term", "clean-exit"]  # the old child stopped cleanly

    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=15) == 0
