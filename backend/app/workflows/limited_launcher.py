"""Apply operating-system resource limits to this process, then run a script. Started ONLY by
app/workflows/sandbox.py as:

    python -I limited_launcher.py <memory_bytes> <cpu_seconds> <file_size_bytes> <script> [args...]

Why a launcher instead of `subprocess.Popen(preexec_fn=...)`: preexec_fn runs Python code
between fork and exec, which Python documents as unsafe in a process that has threads (a Celery
worker has several). Here the limits are set by the new process itself, after it has started
cleanly, and before the script imports anything that parses untrusted input.

Standard library only (a test enforces it).
"""

import resource
import runpy
import sys


def main(argv: list[str]) -> None:
    memory_bytes, cpu_seconds, file_size_bytes = (int(value) for value in argv[:3])
    script, script_args = argv[3], argv[4:]
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_size_bytes, file_size_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # no core dumps of untrusted content
    sys.argv = [script, *script_args]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main(sys.argv[1:])
