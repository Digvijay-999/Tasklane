"""Unit tests for the pure state machine (no database)."""

import ast
import itertools
import sys
from pathlib import Path

import pytest

from app.domain.enums import ErrorClass, JobStatus
from app.domain.state_machine import (
    ATTEMPTS_PER_RUN,
    CLAIMABLE_STATUSES,
    MAX_ATTEMPTS_LIMIT,
    TERMINAL_STATUSES,
    TRANSITIONS,
    CancelDecision,
    FailureKind,
    IllegalTransitionError,
    ManualRetryBlock,
    UnexpectedStatusError,
    can_manual_retry,
    cancel_decision,
    cancel_supersedes,
    ensure_transition,
    is_legal_transition,
    status_after_failure,
    status_after_lease_expired,
    status_after_success,
)

S = JobStatus
TRANSIENT, PERMANENT = FailureKind.TRANSIENT, FailureKind.PERMANENT

# The agreed table, written out independently of the module so a change to TRANSITIONS has to be
# made deliberately in both places.
EXPECTED_LEGAL = {
    (S.QUEUED, S.RUNNING),
    (S.QUEUED, S.CANCELLED),
    (S.RUNNING, S.SUCCEEDED),
    (S.RUNNING, S.RETRY_WAIT),
    (S.RUNNING, S.FAILED),
    (S.RUNNING, S.CANCEL_REQUESTED),
    (S.RETRY_WAIT, S.RUNNING),
    (S.RETRY_WAIT, S.CANCELLED),
    (S.CANCEL_REQUESTED, S.CANCELLED),
    (S.CANCEL_REQUESTED, S.SUCCEEDED),
    (S.CANCEL_REQUESTED, S.FAILED),
    (S.FAILED, S.QUEUED),
}
ALL_PAIRS = list(itertools.product(JobStatus, repeat=2))
ILLEGAL = [pair for pair in ALL_PAIRS if pair not in EXPECTED_LEGAL]


def test_transition_table_is_exactly_the_agreed_one():
    assert set(TRANSITIONS) == EXPECTED_LEGAL
    assert len(ALL_PAIRS) == 49
    assert len(ILLEGAL) == 37


@pytest.mark.parametrize(("from_status", "to_status"), sorted(EXPECTED_LEGAL))
def test_legal_transition_is_accepted(from_status, to_status):
    assert is_legal_transition(from_status, to_status)
    ensure_transition(from_status, to_status)  # does not raise


@pytest.mark.parametrize(("from_status", "to_status"), ILLEGAL)
def test_illegal_transition_is_rejected(from_status, to_status):
    assert not is_legal_transition(from_status, to_status)
    with pytest.raises(IllegalTransitionError) as exc:
        ensure_transition(from_status, to_status)
    assert exc.value.from_status == from_status
    assert exc.value.to_status == to_status


def test_every_legal_transition_has_a_description():
    assert all(description.strip() for description in TRANSITIONS.values())


# --- Shape of the graph --------------------------------------------------------------------------


def _reachable(start: JobStatus) -> set[JobStatus]:
    seen, frontier = {start}, [start]
    while frontier:
        current = frontier.pop()
        for from_status, to_status in TRANSITIONS:
            if from_status == current and to_status not in seen:
                seen.add(to_status)
                frontier.append(to_status)
    return seen


def test_every_status_is_reachable_from_queued():
    assert _reachable(S.QUEUED) == set(JobStatus)


@pytest.mark.parametrize("status", list(JobStatus))
def test_every_status_can_reach_a_terminal_status(status):
    assert _reachable(status) & TERMINAL_STATUSES


def test_only_manual_retry_leaves_a_terminal_status():
    leaving_terminal = {pair for pair in TRANSITIONS if pair[0] in TERMINAL_STATUSES}
    assert leaving_terminal == {(S.FAILED, S.QUEUED)}


def test_no_status_transitions_to_itself():
    assert all(from_status != to_status for from_status, to_status in TRANSITIONS)


# --- Decision functions --------------------------------------------------------------------------


@pytest.mark.parametrize("current", [S.RUNNING, S.CANCEL_REQUESTED])
def test_success_ends_succeeded_even_if_cancel_was_requested(current):
    assert status_after_success(current) == S.SUCCEEDED


@pytest.mark.parametrize(
    "current", [s for s in JobStatus if s not in (S.RUNNING, S.CANCEL_REQUESTED)]
)
def test_success_is_unexpected_when_no_attempt_is_running(current):
    with pytest.raises(UnexpectedStatusError):
        status_after_success(current)


@pytest.mark.parametrize(
    ("current", "kind", "attempt_count", "max_attempts", "expected"),
    [
        # Transient errors retry while the budget lasts (edges: one left, none left).
        (S.RUNNING, TRANSIENT, 1, 3, S.RETRY_WAIT),
        (S.RUNNING, TRANSIENT, 2, 3, S.RETRY_WAIT),
        (S.RUNNING, TRANSIENT, 3, 3, S.FAILED),
        (S.RUNNING, TRANSIENT, 1, 1, S.FAILED),
        # Permanent errors never retry, even with budget left.
        (S.RUNNING, PERMANENT, 1, 3, S.FAILED),
        # Cancel requested: a definite outcome (permanent failure) wins; a retryable one yields.
        (S.CANCEL_REQUESTED, PERMANENT, 1, 3, S.FAILED),
        (S.CANCEL_REQUESTED, TRANSIENT, 1, 3, S.CANCELLED),
        (S.CANCEL_REQUESTED, TRANSIENT, 3, 3, S.CANCELLED),
    ],
)
def test_status_after_failure(current, kind, attempt_count, max_attempts, expected):
    result = status_after_failure(current, kind, attempt_count, max_attempts)
    assert result == expected
    assert is_legal_transition(current, result)


@pytest.mark.parametrize(
    "current", [s for s in JobStatus if s not in (S.RUNNING, S.CANCEL_REQUESTED)]
)
@pytest.mark.parametrize("kind", list(FailureKind))
def test_failure_is_unexpected_when_no_attempt_is_running(current, kind):
    with pytest.raises(UnexpectedStatusError):
        status_after_failure(current, kind, 1, 3)


@pytest.mark.parametrize(
    ("current", "attempt_count", "max_attempts", "expected"),
    [
        (S.RUNNING, 1, 3, S.RETRY_WAIT),
        (S.RUNNING, 2, 3, S.RETRY_WAIT),
        (S.RUNNING, 3, 3, S.FAILED),
        (S.CANCEL_REQUESTED, 1, 3, S.CANCELLED),
        (S.CANCEL_REQUESTED, 3, 3, S.CANCELLED),
    ],
)
def test_status_after_lease_expired(current, attempt_count, max_attempts, expected):
    result = status_after_lease_expired(current, attempt_count, max_attempts)
    assert result == expected
    assert is_legal_transition(current, result)


@pytest.mark.parametrize(
    "current", [s for s in JobStatus if s not in (S.RUNNING, S.CANCEL_REQUESTED)]
)
def test_lease_expiry_is_unexpected_without_a_running_attempt(current):
    with pytest.raises(UnexpectedStatusError):
        status_after_lease_expired(current, 1, 3)


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (S.QUEUED, CancelDecision(S.CANCELLED)),  # no worker holds it: cancel directly
        (S.RETRY_WAIT, CancelDecision(S.CANCELLED)),
        (S.RUNNING, CancelDecision(S.CANCEL_REQUESTED)),  # worker stops at a stage boundary
        (S.CANCEL_REQUESTED, CancelDecision(None)),  # repeating the request is a no-op
        (S.SUCCEEDED, CancelDecision(None)),  # finished jobs report their state unchanged
        (S.FAILED, CancelDecision(None)),
        (S.CANCELLED, CancelDecision(None)),
    ],
)
def test_cancel_decision(current, expected):
    decision = cancel_decision(current)
    assert decision == expected
    if decision.target is not None:
        assert is_legal_transition(current, decision.target)
    assert decision.is_noop == (decision.target is None)


def test_cancel_decision_covers_claimable_statuses():
    assert {s for s in CLAIMABLE_STATUSES if cancel_decision(s).target == S.CANCELLED} == {
        S.QUEUED,
        S.RETRY_WAIT,
    }


@pytest.mark.parametrize(("from_status", "to_status"), ALL_PAIRS)
def test_cancel_supersedes_only_for_definite_outcomes_after_cancel_request(from_status, to_status):
    expected = from_status == S.CANCEL_REQUESTED and to_status in (S.SUCCEEDED, S.FAILED)
    assert cancel_supersedes(from_status, to_status) == expected


# --- Manual retry --------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [s for s in JobStatus if s != S.FAILED])
def test_manual_retry_only_for_failed_jobs(status):
    decision = can_manual_retry(status, None, 1)
    assert not decision.allowed
    assert decision.reason == ManualRetryBlock.NOT_FAILED
    assert decision.message


@pytest.mark.parametrize("error_class", [ErrorClass.INVALID_INPUT, ErrorClass.UNSUPPORTED_INPUT])
def test_manual_retry_not_offered_for_input_errors(error_class):
    decision = can_manual_retry(S.FAILED, error_class, 1)
    assert not decision.allowed
    assert decision.reason == ManualRetryBlock.INPUT_ERROR
    assert "input" in decision.message


@pytest.mark.parametrize("error_class", [None, ErrorClass.LEASE_EXPIRED, "provider_timeout"])
def test_manual_retry_grants_a_fresh_budget(error_class):
    decision = can_manual_retry(S.FAILED, error_class, 3)
    assert decision.allowed
    assert decision.reason is None
    assert decision.message is None
    assert decision.new_max_attempts == 3 + ATTEMPTS_PER_RUN


def test_manual_retry_allowed_when_new_budget_lands_exactly_on_the_cap():
    attempt_count = MAX_ATTEMPTS_LIMIT - ATTEMPTS_PER_RUN  # 17
    decision = can_manual_retry(S.FAILED, None, attempt_count)
    assert decision.allowed
    assert decision.new_max_attempts == MAX_ATTEMPTS_LIMIT


@pytest.mark.parametrize(
    "attempt_count",
    [MAX_ATTEMPTS_LIMIT - ATTEMPTS_PER_RUN + 1, MAX_ATTEMPTS_LIMIT - 1, MAX_ATTEMPTS_LIMIT],
)
def test_manual_retry_refused_when_it_would_exceed_the_cap(attempt_count):
    decision = can_manual_retry(S.FAILED, None, attempt_count)
    assert not decision.allowed
    assert decision.reason == ManualRetryBlock.RETRY_LIMIT_REACHED
    assert decision.new_max_attempts is None
    assert str(MAX_ATTEMPTS_LIMIT) in decision.message


@pytest.mark.parametrize("attempt_count", range(MAX_ATTEMPTS_LIMIT + 1))
def test_manual_retry_never_produces_a_budget_above_the_cap(attempt_count):
    decision = can_manual_retry(S.FAILED, None, attempt_count)
    if decision.allowed:
        assert attempt_count < decision.new_max_attempts <= MAX_ATTEMPTS_LIMIT


# --- Purity --------------------------------------------------------------------------------------

DOMAIN_FILES = sorted((Path(__file__).resolve().parents[2] / "app" / "domain").glob("*.py"))


def _imported_modules(path: Path) -> list[str]:
    """Absolute names of every module a file imports, with relative imports resolved."""
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: "." is app.domain, ".." is app, ...
                base = ["app", "domain"][: 2 - (node.level - 1)] if node.level <= 2 else []
                names.append(".".join([*base, *(node.module or "").split(".")]).strip("."))
            else:
                names.append(node.module or "")
    return names


def _allowed_in_domain(module: str) -> bool:
    """The domain may import only the standard library and other domain modules."""
    top_level = module.split(".")[0]
    return (module == "app.domain" or module.startswith("app.domain.")) or (
        top_level in sys.stdlib_module_names and top_level != "app"
    )


def test_domain_files_were_found():
    assert len(DOMAIN_FILES) >= 3


@pytest.mark.parametrize("path", DOMAIN_FILES, ids=lambda p: p.name)
def test_domain_imports_only_stdlib_and_domain(path):
    disallowed = [m for m in _imported_modules(path) if not _allowed_in_domain(m)]
    assert disallowed == []


@pytest.mark.parametrize(
    ("module", "allowed"),
    [
        ("dataclasses", True),
        ("app.domain.enums", True),
        ("app.db.models", False),
        ("app.config", False),
        ("app.queue", False),
        ("sqlalchemy.orm", False),
        ("pydantic", False),
        ("celery", False),
    ],
)
def test_purity_allowlist(module, allowed):
    assert _allowed_in_domain(module) == allowed


def test_relative_imports_are_resolved(tmp_path):
    source = tmp_path / "x.py"
    source.write_text("from ..db import models\nfrom . import enums\nfrom .enums import A\n")
    assert _imported_modules(source) == ["app.db", "app.domain", "app.domain.enums"]
