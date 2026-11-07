"""Run a parser for untrusted input in a separate, limited process (shared by the PDF and ZIP
workflows) and enforce its limits.

Which mechanism enforces which limit:
- Wall-clock time: THIS process waits with a deadline and kills the child's whole process group
  (SIGKILL) when it passes. A stuck parser can't stop itself, but it can be stopped from outside.
- Memory: RLIMIT_AS. Exceeding it makes allocations fail (MemoryError) or kills the child.
- CPU: RLIMIT_CPU (deadline + a few seconds), a kernel backstop in case this process stalls.
- The RLIMITs (and "no core dumps") are set by limited_launcher.py inside the child, before the
  child script is loaded.
- Output size: RLIMIT_FSIZE, so a crafted file can't fill the disk.
- Isolation: `python -I` (the application's code isn't importable, PYTHON* variables are ignored)
  and an environment with only PATH and LANG (no database password, no API keys).

While waiting, the parent wakes at least every `keep_alive_interval` seconds to renew the job's
lease, so a long parse can't let the lease lapse (Phase 7 adds independent heartbeats).

Protocol: the child writes one JSON object per line on stdout.
    {"type": "progress", "done": 3, "total": 10}
    {"type": "result", ...}                                  then exit 0
    {"type": "error", "error_class": "...", "params": {...}} then exit 0
The parent trusts only error classes the caller allows and integer parameters. Any other exit
(a crash, or being killed) is interpreted here.
"""

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.errors import ERRORS, JobError

logger = logging.getLogger(__name__)

LAUNCHER = Path(__file__).with_name("limited_launcher.py")
_STDERR_TAIL_BYTES = 4096


@dataclass(frozen=True)
class SandboxLimits:
    memory_bytes: int
    output_max_bytes: int  # the largest file the child may write


def run_sandboxed(
    child_script: Path,
    args: Sequence[str],
    *,
    limits: SandboxLimits,
    timeout_seconds: float,
    keep_alive: Callable[[], None],
    keep_alive_interval: float,
    too_complex_error: str,
    allowed_errors: Collection[str],
    on_progress: Callable[[int, int], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run the child and return its result. Raises JobError for every expected failure:
    `too_complex_error` when it is killed (deadline, memory, CPU, output size), or the class it
    reported if that class is in `allowed_errors`."""
    command = [
        sys.executable,
        "-I",  # isolated: no app imports, no PYTHON* environment variables, no user site
        str(LAUNCHER),  # sets the resource limits, then runs the child script
        str(limits.memory_bytes),
        str(int(timeout_seconds) + 5),  # CPU seconds: a backstop behind the wall-clock deadline
        str(limits.output_max_bytes),
        str(child_script),
        *args,
    ]
    label = child_script.name
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        # The child gets no secrets: not the database password, not API keys.
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
        start_new_session=True,  # its own process group, so it can be killed as a whole
    )
    messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
    stderr_tail = bytearray()
    readers = [
        threading.Thread(target=_read_messages, args=(process.stdout, messages), daemon=True),
        threading.Thread(target=_read_tail, args=(process.stderr, stderr_tail), daemon=True),
    ]
    for reader in readers:
        reader.start()

    deadline = clock() + timeout_seconds
    next_keep_alive = clock() + keep_alive_interval
    try:
        while True:
            now = clock()
            if now >= deadline:
                _kill(process)
                logger.warning(
                    "sandboxed parser timed out",
                    extra={"child": label, "timeout": timeout_seconds},
                )
                raise JobError(too_complex_error)
            if now >= next_keep_alive:
                keep_alive()  # raises if this worker no longer owns the job
                next_keep_alive = now + keep_alive_interval
            try:
                message = messages.get(timeout=max(0.01, min(deadline, next_keep_alive) - now))
            except queue.Empty:
                continue
            if message is None:  # stdout closed: the child exited without a final message
                process.wait(timeout=10)
                readers[1].join(timeout=5)  # let the stderr reader finish before reading it
                return _interpret_exit(process, bytes(stderr_tail), label, too_complex_error)
            kind = message.get("type")
            if kind == "progress" and on_progress is not None:
                on_progress(int(message.get("done", 0)), int(message.get("total", 0)))
            elif kind == "result":
                process.wait(timeout=10)
                return {k: v for k, v in message.items() if k != "type"}
            elif kind == "error":
                process.wait(timeout=10)
                raise _job_error(message, allowed_errors)
    finally:
        _kill(process)
        for reader in readers:
            reader.join(timeout=2)


def _read_messages(stream: Any, messages: "queue.Queue[dict[str, Any] | None]") -> None:
    try:
        for raw in stream:
            try:
                message = json.loads(raw)
            except ValueError:
                continue  # ignore anything that isn't a protocol line
            if isinstance(message, dict):
                messages.put(message)
    finally:
        messages.put(None)


def _read_tail(stream: Any, tail: bytearray) -> None:
    for chunk in iter(lambda: stream.read(1024), b""):
        tail.extend(chunk)
        del tail[:-_STDERR_TAIL_BYTES]


def _job_error(message: dict[str, Any], allowed_errors: Collection[str]) -> JobError:
    """Turn the child's error message into a JobError, trusting only the allowed classes and
    numeric parameters (the child processed untrusted input)."""
    error_class = message.get("error_class")
    params = message.get("params") or {}
    if error_class not in allowed_errors or error_class not in ERRORS:
        return JobError("internal_error")
    if not isinstance(params, dict):
        return JobError("internal_error")
    try:
        return JobError(error_class, **{str(k): int(v) for k, v in params.items()})
    except (KeyError, TypeError, ValueError):  # parameters don't fit the message template
        return JobError("internal_error")


def _interpret_exit(
    process: subprocess.Popen, stderr_tail: bytes, label: str, too_complex_error: str
) -> dict[str, Any]:
    returncode = process.wait(timeout=10)
    # Killed by a signal (out of memory, CPU limit, file-size limit) or ran out of memory:
    # the input needs more resources than we allow.
    # (Python ignores the "file too large" signal, so going over RLIMIT_FSIZE shows up as an
    # EFBIG OSError instead: "[Errno 27] File too large".)
    exhausted = (b"MemoryError", b"[Errno 27]", b"File too large")
    if returncode < 0 or any(marker in stderr_tail for marker in exhausted):
        logger.warning(
            "sandboxed parser was killed or ran out of memory or disk allowance",
            extra={"child": label, "returncode": returncode},
        )
        raise JobError(too_complex_error)
    # Any other exit without a protocol message is a bug in our child code, not the input.
    logger.error(
        "sandboxed parser exited unexpectedly",
        extra={
            "child": label,
            "returncode": returncode,
            "stderr": stderr_tail.decode(errors="replace"),
        },
    )
    raise RuntimeError(f"sandboxed parser {label} exited with code {returncode}")


def _kill(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)
