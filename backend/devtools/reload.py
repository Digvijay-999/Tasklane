"""Dev only: run a command, restart it when Python files change, stop it gracefully.

    python -m devtools.reload --path app --grace 25 -- celery -A app.queue.worker_main worker

Why not the `watchfiles` CLI: in watchfiles 1.3.0, when the CLI itself receives SIGTERM (which is
what `docker compose stop` sends), it stops the child with a hard-coded 5-second limit,
ignoring --sigint-timeout. A Celery worker in the middle of a job would be killed after 5 s
instead of finishing (a "warm shutdown"). This runner uses watchfiles only to watch files and
manages the child itself:

- On SIGTERM/SIGINT: send the child SIGTERM (Celery: warm shutdown; the dispatcher: finish the
  current pass), wait up to --grace seconds, then SIGKILL. Then exit.
- On a Python file change: the same graceful stop, then start the command again.
- If the child exits on its own (a crash, bad settings): exit with its exit code, so Docker's
  restart policy restarts the container and the problem is visible in `docker compose ps`.

Set WATCHFILES_FORCE_POLLING=true where file events don't arrive (Docker Desktop on Windows).
"""

import argparse
import logging
import signal
import subprocess
import sys
import threading

from watchfiles import PythonFilter, watch

logger = logging.getLogger("tasklane.devtools.reload")


def stop_child(process: subprocess.Popen, grace: float) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        logger.warning("child did not stop within %ss; killing it", grace)
        process.kill()
        process.wait()


def run(command: list[str], paths: list[str], grace: float, stop: threading.Event) -> int:
    process = subprocess.Popen(command)
    try:
        # yield_on_timeout: wake up every second (with no changes) to check whether the child
        # exited on its own.
        for changes in watch(
            *paths,
            watch_filter=PythonFilter(),
            stop_event=stop,
            yield_on_timeout=True,
            rust_timeout=1000,
        ):
            if changes:
                logger.warning("%d file(s) changed; restarting", len(changes))
                stop_child(process, grace)
                process = subprocess.Popen(command)
            elif process.poll() is not None:
                logger.warning("child exited with code %s", process.returncode)
                return process.returncode or 1
    finally:
        stop_child(process, grace)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", action="append", default=[], help="directory to watch")
    parser.add_argument("--grace", type=float, default=10.0, help="seconds to wait on stop")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- command [args...]")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")

    logging.basicConfig(level=logging.INFO, format="[reload] %(message)s")
    stop = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logger.warning("received %s; stopping child gracefully", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    return run(command, args.path or ["."], args.grace, stop)


if __name__ == "__main__":
    sys.exit(main())
