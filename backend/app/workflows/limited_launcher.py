"""Apply operating-system resource limits to this process, then run a script. Started ONLY by
app/workflows/sandbox.py as:

    python -I limited_launcher.py <parent_pid> <memory_bytes> <cpu_seconds> <file_size_bytes>
                                  <script> [args...]

Why a launcher instead of `subprocess.Popen(preexec_fn=...)`: preexec_fn runs Python code
between fork and exec, which Python documents as unsafe in a process that has threads (a Celery
worker has several). Here the limits are set by the new process itself, after it has started
cleanly, and before the script imports anything that parses untrusted input.

Dying with the worker (Phase 7): the child runs in its own session (so the worker can kill it as a
group), which also means nothing kills it if the *worker* is killed (kill -9, out of memory).
`prctl(PR_SET_PDEATHSIG, SIGKILL)` asks the Linux kernel to kill this process the moment its
parent dies. If the parent died before that call (a tiny window right after the fork), our
parent is no longer `<parent_pid>`, so we exit at once. Linux only, like the resource limits.

Standard library only (a test enforces it).
"""

import ctypes
import os
import resource
import runpy
import signal
import sys

PR_SET_PDEATHSIG = 1  # from <linux/prctl.h>


def die_with_parent(parent_pid: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if os.getppid() != parent_pid:
        os._exit(1)  # the parent is already gone: nobody is waiting for our result


def main(argv: list[str]) -> None:
    parent_pid, memory_bytes, cpu_seconds, file_size_bytes = (int(value) for value in argv[:4])
    script, script_args = argv[4], argv[5:]
    die_with_parent(parent_pid)
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_size_bytes, file_size_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # no core dumps of untrusted content
    sys.argv = [script, *script_args]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main(sys.argv[1:])
