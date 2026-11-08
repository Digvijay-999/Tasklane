"""List the processes in this container (the slim image has no `ps`). Development helper.

    docker compose exec -T worker python -m devtools.ps

Reads /proc (Linux). Read-only.
"""

import os
from pathlib import Path


def processes() -> list[tuple[int, int, str, str]]:
    """(pid, parent pid, state, command line) for every process, by pid."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue  # it exited while we were looking
        state, ppid = stat.rsplit(")", 1)[1].split()[:2]
        found.append((int(entry.name), int(ppid), state, cmdline.strip()))
    return sorted(found)


def main() -> None:
    print(f"{'PID':>6} {'PPID':>6} S COMMAND")
    for pid, ppid, state, command in processes():
        if pid != os.getpid():
            print(f"{pid:>6} {ppid:>6} {state} {command[:150]}")


if __name__ == "__main__":
    main()
