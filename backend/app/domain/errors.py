"""Errors that end a job attempt, with safe, user-facing messages (pure Python).

Every error class has a fixed message template. Templates take only numbers (limits, counts),
never exception text, file content, or paths, so `error_summary` and the event stream can't leak
internals. Tracebacks and raw exception text go to the logs only.

Phase 5 Part A: every class here fails the job permanently. Part B adds transient classes and
automatic retries.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorKind(StrEnum):
    TRANSIENT = "transient"  # might succeed if tried again (Part B: retried)
    PERMANENT = "permanent"  # will fail the same way again


@dataclass(frozen=True)
class ErrorSpec:
    kind: ErrorKind
    message: str  # str.format template; parameters are numbers only


# error_class -> spec. The keys are stored in jobs.error_class and job_attempts.error_class.
ERRORS: dict[str, ErrorSpec] = {
    # --- The input itself (permanent: the same file fails the same way) ---
    "pdf_encrypted": ErrorSpec(
        ErrorKind.PERMANENT, "The PDF is password-protected. Upload an unprotected copy."
    ),
    "pdf_corrupt": ErrorSpec(
        ErrorKind.PERMANENT, "The PDF is damaged or is not a valid PDF, so it could not be read."
    ),
    "pdf_no_text": ErrorSpec(
        ErrorKind.PERMANENT,
        "The PDF contains little or no extractable text (it may be scanned images). "
        "Scanned PDFs aren't supported yet.",
    ),
    "pdf_too_many_pages": ErrorSpec(
        ErrorKind.PERMANENT, "The PDF has {pages} pages; the limit is {limit}."
    ),
    "pdf_text_too_large": ErrorSpec(
        ErrorKind.PERMANENT,
        "The PDF contains more than {limit} characters of text, which is over the limit.",
    ),
    "pdf_too_complex": ErrorSpec(
        ErrorKind.PERMANENT,
        "The PDF took too long or needed too much memory to process.",
    ),
    "input_unavailable": ErrorSpec(
        ErrorKind.PERMANENT,
        "The uploaded file is no longer available. Upload it again and create a new job.",
    ),
    # --- Processing limits (Part B makes this transient: a retry resumes the work) ---
    "stage_timeout": ErrorSpec(
        ErrorKind.PERMANENT, "Processing took longer than the {limit}-second limit for this step."
    ),
    # --- Anything unexpected ---
    "internal_error": ErrorSpec(
        ErrorKind.PERMANENT, "An unexpected error occurred while running the job."
    ),
}

# Errors caused by the input file: a manual retry would fail the same way, so it isn't offered.
INPUT_ERROR_CLASSES = frozenset(
    {
        "invalid_input",
        "unsupported_input",
        "pdf_encrypted",
        "pdf_corrupt",
        "pdf_no_text",
        "pdf_too_many_pages",
        "pdf_text_too_large",
        "pdf_too_complex",
        "input_unavailable",
    }
)


class JobError(Exception):
    """An expected failure of a job attempt, carrying a stable class and a safe message."""

    def __init__(self, error_class: str, **params: Any) -> None:
        spec = ERRORS[error_class]
        self.error_class = error_class
        self.kind = spec.kind
        self.params = params
        self.safe_message = spec.message.format(**params)
        super().__init__(f"{error_class}: {self.safe_message}")

    @property
    def retryable(self) -> bool:
        return self.kind == ErrorKind.TRANSIENT
