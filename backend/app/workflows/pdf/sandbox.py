"""Run the PDF parser (parse_child.py) in the shared sandbox (app/workflows/sandbox.py).

Pages and characters are checked inside the child (cheap, and it stops early); time, memory, CPU
and output size are enforced by the sandbox. A parser that is killed or runs out of resources
fails the job with `pdf_too_complex`.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.workflows.sandbox import SandboxLimits, run_sandboxed

CHILD_SCRIPT = Path(__file__).with_name("parse_child.py")

# The only error classes the PDF child may report (anything else becomes internal_error).
PDF_CHILD_ERRORS = frozenset(
    {
        "pdf_encrypted",
        "pdf_corrupt",
        "pdf_no_text",
        "pdf_too_many_pages",
        "pdf_text_too_large",
        "pdf_too_complex",
    }
)


@dataclass(frozen=True)
class ParserLimits:
    max_pages: int
    max_chars: int
    memory_bytes: int
    output_max_bytes: int


def run_parser(
    mode: str,
    input_path: Path,
    output_path: Path,
    *,
    limits: ParserLimits,
    timeout_seconds: float,
    keep_alive: Callable[[], None],
    keep_alive_interval: float,
    on_progress: Callable[[int, int], None] | None = None,
    child_script: Path = CHILD_SCRIPT,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run the child and return its result. Raises JobError for every expected failure."""
    return run_sandboxed(
        child_script,
        [mode, str(input_path), str(output_path), str(limits.max_pages), str(limits.max_chars)],
        limits=SandboxLimits(limits.memory_bytes, limits.output_max_bytes),
        timeout_seconds=timeout_seconds,
        keep_alive=keep_alive,
        keep_alive_interval=keep_alive_interval,
        too_complex_error="pdf_too_complex",
        allowed_errors=PDF_CHILD_ERRORS,
        on_progress=on_progress,
        clock=clock,
    )
