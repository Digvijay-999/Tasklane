"""The job state machine: which status changes are legal, and which status an event leads to.

This module is pure Python. It never reads or writes the database; the db layer asks it
"is queued -> running legal?" and then performs the change with a conditional UPDATE.

Two kinds of functions live here:

1. The transition table (`TRANSITIONS`, `ensure_transition`): the complete list of legal
   from -> to pairs. Everything not listed is illegal.
2. Decision functions (`status_after_failure`, `cancel_decision`, ...): given what just happened,
   which status should the job move to? They encode the rules agreed in planning:
   - A queued or retry_wait job is cancelled directly (no worker holds it).
   - A *definite* outcome that happens before the next stage boundary (success or permanent
     failure) wins over a pending cancel request. A *non-definite* one (a transient error,
     which would only lead to a retry) gives way to the cancel request.
   - "Terminal" means no automatic process (worker, recovery, dispatcher) will ever move the job
     again. Only an explicit user action, manual retry, can reopen a `failed` job.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.domain.enums import JobStatus
from app.domain.errors import INPUT_ERROR_CLASSES

# --- Status groups -------------------------------------------------------------------------------

TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})

# Statuses a worker may claim (move to running).
CLAIMABLE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RETRY_WAIT})

# --- Retry budget ------------------------------------------------------------------------------

# Automatic attempts granted to a new job, and again by each manual retry.
ATTEMPTS_PER_RUN = 3

# Hard ceiling on max_attempts. The database enforces the same number (ck_jobs_attempt_budget);
# can_manual_retry() refuses before that constraint could ever be hit.
MAX_ATTEMPTS_LIMIT = 20

# Errors caused by the input itself (rerunning would fail the same way) are listed in
# app.domain.errors.INPUT_ERROR_CLASSES (imported above); can_manual_retry() refuses them.

# --- Transition table ----------------------------------------------------------------------------

S = JobStatus
TRANSITIONS: dict[tuple[JobStatus, JobStatus], str] = {
    (S.QUEUED, S.RUNNING): "a worker claimed the job",
    (S.QUEUED, S.CANCELLED): "cancelled before any worker started it",
    (S.RUNNING, S.SUCCEEDED): "all stages finished",
    (S.RUNNING, S.RETRY_WAIT): "transient error or expired lease, attempts remain",
    (S.RUNNING, S.FAILED): "permanent error, or no attempts remain",
    (S.RUNNING, S.CANCEL_REQUESTED): "user asked to cancel; the worker stops at a stage boundary",
    (S.RETRY_WAIT, S.RUNNING): "a worker claimed the due retry",
    (S.RETRY_WAIT, S.CANCELLED): "cancelled while waiting to retry (no worker involved)",
    (
        S.CANCEL_REQUESTED,
        S.CANCELLED,
    ): "worker reached a stage boundary, or stopped before finishing",
    (S.CANCEL_REQUESTED, S.SUCCEEDED): "the job finished before the worker saw the cancel request",
    (S.CANCEL_REQUESTED, S.FAILED): "a permanent error happened before the cancel took effect",
    (S.FAILED, S.QUEUED): "manual retry",
}
del S


class StateMachineError(Exception):
    """Base class for state-machine errors. These indicate a programming bug, not user error."""


class IllegalTransitionError(StateMachineError):
    def __init__(self, from_status: JobStatus, to_status: JobStatus) -> None:
        super().__init__(f"illegal job transition: {from_status} -> {to_status}")
        self.from_status = from_status
        self.to_status = to_status


class UnexpectedStatusError(StateMachineError):
    """A decision function was asked about a status where its event cannot happen."""

    def __init__(self, event: str, status: JobStatus) -> None:
        super().__init__(f"{event} cannot happen while a job is {status}")
        self.event = event
        self.status = status


def is_legal_transition(from_status: JobStatus, to_status: JobStatus) -> bool:
    return (from_status, to_status) in TRANSITIONS


def ensure_transition(from_status: JobStatus, to_status: JobStatus) -> None:
    """Raise IllegalTransitionError unless from_status -> to_status is in the table."""
    if not is_legal_transition(from_status, to_status):
        raise IllegalTransitionError(from_status, to_status)


def is_terminal(status: JobStatus) -> bool:
    return status in TERMINAL_STATUSES


# --- Decision functions --------------------------------------------------------------------------


class FailureKind(StrEnum):
    TRANSIENT = "transient"  # might work if tried again (timeout, provider 5xx/429)
    PERMANENT = "permanent"  # will fail the same way again (corrupt input, unsupported file)


def has_attempts_left(attempt_count: int, max_attempts: int) -> bool:
    return attempt_count < max_attempts


def status_after_success(current: JobStatus) -> JobStatus:
    """The last stage finished. A pending cancel request is superseded (decided in Phase 0)."""
    if current in (JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED):
        return JobStatus.SUCCEEDED
    raise UnexpectedStatusError("success", current)


def status_after_failure(
    current: JobStatus, kind: FailureKind, attempt_count: int, max_attempts: int
) -> JobStatus:
    """A stage raised an error during the current attempt."""
    if current == JobStatus.RUNNING:
        if kind == FailureKind.PERMANENT:
            return JobStatus.FAILED
        return (
            JobStatus.RETRY_WAIT
            if has_attempts_left(attempt_count, max_attempts)
            else (JobStatus.FAILED)
        )
    if current == JobStatus.CANCEL_REQUESTED:
        # Permanent failure is a definite outcome and wins; a transient one would only lead to a
        # retry the user no longer wants, so the cancel request wins.
        return JobStatus.FAILED if kind == FailureKind.PERMANENT else JobStatus.CANCELLED
    raise UnexpectedStatusError("failure", current)


def status_after_lease_expired(
    current: JobStatus, attempt_count: int, max_attempts: int
) -> JobStatus:
    """Recovery found an attempt whose worker stopped heartbeating."""
    if current == JobStatus.RUNNING:
        return (
            JobStatus.RETRY_WAIT
            if has_attempts_left(attempt_count, max_attempts)
            else (JobStatus.FAILED)
        )
    if current == JobStatus.CANCEL_REQUESTED:
        return JobStatus.CANCELLED  # nobody is left to finish it, and the user wanted it stopped
    raise UnexpectedStatusError("lease expiry", current)


def cancel_supersedes(from_status: JobStatus, to_status: JobStatus) -> bool:
    """True when a cancel request was overtaken by a definite outcome.

    The db layer records a `job.cancel_superseded` event whenever this is true, so the UI can
    explain why a job the user cancelled ended up succeeded or failed.
    """
    return from_status == JobStatus.CANCEL_REQUESTED and to_status in (
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
    )


@dataclass(frozen=True)
class CancelDecision:
    """`target` is the status to move to, or None when cancelling is a no-op."""

    target: JobStatus | None

    @property
    def is_noop(self) -> bool:
        return self.target is None


def cancel_decision(current: JobStatus) -> CancelDecision:
    """What a user's cancel request does in each status."""
    if current in CLAIMABLE_STATUSES:
        return CancelDecision(JobStatus.CANCELLED)
    if current == JobStatus.RUNNING:
        return CancelDecision(JobStatus.CANCEL_REQUESTED)
    # Already requested, or already finished: report the current state, change nothing.
    return CancelDecision(None)


class ManualRetryBlock(StrEnum):
    """Why a manual retry is not offered."""

    NOT_FAILED = "not_failed"
    INPUT_ERROR = "input_error"
    RETRY_LIMIT_REACHED = "retry_limit_reached"


_BLOCK_MESSAGES = {
    ManualRetryBlock.NOT_FAILED: "Only failed jobs can be retried.",
    ManualRetryBlock.INPUT_ERROR: (
        "The job failed because of its input; retrying would fail the same way. "
        "Upload a corrected file and submit a new job."
    ),
    ManualRetryBlock.RETRY_LIMIT_REACHED: (
        f"This job has reached the limit of {MAX_ATTEMPTS_LIMIT} attempts and cannot be retried."
    ),
}


@dataclass(frozen=True)
class ManualRetryDecision:
    allowed: bool
    reason: ManualRetryBlock | None = None
    new_max_attempts: int | None = None  # set when allowed: attempt_count + ATTEMPTS_PER_RUN

    @property
    def message(self) -> str | None:
        return _BLOCK_MESSAGES[self.reason] if self.reason else None


def can_manual_retry(
    status: JobStatus, error_class: str | None, attempt_count: int
) -> ManualRetryDecision:
    """Decide whether a user may retry this job, and with what new attempt budget.

    A manual retry grants a fresh budget of ATTEMPTS_PER_RUN automatic attempts. It is refused
    when that budget would push max_attempts past MAX_ATTEMPTS_LIMIT, so the database
    constraint is never what stops it.
    """
    if status != JobStatus.FAILED:
        return ManualRetryDecision(False, ManualRetryBlock.NOT_FAILED)
    if error_class in INPUT_ERROR_CLASSES:
        return ManualRetryDecision(False, ManualRetryBlock.INPUT_ERROR)
    new_max_attempts = attempt_count + ATTEMPTS_PER_RUN
    if new_max_attempts > MAX_ATTEMPTS_LIMIT:
        return ManualRetryDecision(False, ManualRetryBlock.RETRY_LIMIT_REACHED)
    return ManualRetryDecision(True, new_max_attempts=new_max_attempts)
