"""Backoff, checkpoint reuse rules, fault plans and error classification: pure logic, no I/O."""

import errno

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.domain.checkpoints import (
    Checkpoint,
    OutputFile,
    ReuseDecision,
    input_fingerprint,
    reuse_decision,
)
from app.domain.errors import JobError
from app.domain.retry import RetryPolicy, retry_delay_seconds
from app.providers.base import SummarizeOptions, SummaryResult
from app.providers.faults import (
    FaultInjectingProvider,
    InjectedPermanentFault,
    InjectedTransientFault,
    fault_plan,
    with_fault_injection,
)
from app.services.execution import _classify

# --- Backoff with equal jitter --------------------------------------------------------------------


def _delay(attempt_no: int, r: float, base: float = 5, maximum: float = 300) -> float:
    return retry_delay_seconds(attempt_no, base_seconds=base, max_seconds=maximum, random=lambda: r)


@pytest.mark.parametrize(
    ("attempt_no", "low", "high"),
    [(1, 2.5, 5), (2, 5, 10), (3, 10, 20), (4, 20, 40), (7, 150, 300), (50, 150, 300)],
)
def test_jitter_bounds(attempt_no, low, high):
    """First retry 2.5-5 s, second 5-10 s, ... never more than the 300 s cap."""
    assert _delay(attempt_no, 0.0) == low  # random() == 0: the lower bound, half the ceiling
    assert low <= _delay(attempt_no, 0.5) < high
    assert _delay(attempt_no, 0.999999) < high  # random() < 1: never reaches the ceiling


def test_jitter_is_spread_by_the_random_source():
    delays = {_delay(1, r) for r in (0.0, 0.25, 0.5, 0.75)}
    assert delays == {2.5, 3.125, 3.75, 4.375}


def test_real_random_stays_in_bounds():
    for _ in range(1000):
        assert 5 <= retry_delay_seconds(2, base_seconds=5, max_seconds=300) < 10


def test_policy_uses_its_settings():
    policy = RetryPolicy(base_seconds=1, max_seconds=4, random=lambda: 0.5)
    assert [policy.delay_seconds(n) for n in (1, 2, 3, 4)] == [0.75, 1.5, 3.0, 3.0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"attempt_no": 0, "base_seconds": 5, "max_seconds": 300},
        {"attempt_no": 1, "base_seconds": 0, "max_seconds": 300},
        {"attempt_no": 1, "base_seconds": 10, "max_seconds": 5},
    ],
)
def test_invalid_backoff_arguments(kwargs):
    with pytest.raises(ValueError):
        retry_delay_seconds(**kwargs)


# --- Checkpoint reuse rules -----------------------------------------------------------------------

A = OutputFile("work/j/a.json", 10, "a" * 64)
B = OutputFile("work/j/b.json", 20, "b" * 64)


def _fingerprint(**changes):
    values = {"stage": "s", "stage_version": 1, "params": {"x": 1}, "upstream": [A]}
    values.update(changes)
    return input_fingerprint(**values)


def test_fingerprint_is_deterministic_and_covers_every_input():
    base = _fingerprint()
    assert base == _fingerprint()
    assert len(base) == 64
    assert _fingerprint(params={"x": 2}) != base
    assert _fingerprint(stage_version=2) != base
    assert _fingerprint(stage="other") != base
    assert _fingerprint(upstream=[OutputFile(A.path, A.size_bytes, "c" * 64)]) != base
    assert _fingerprint(upstream=[]) != base


def _decide(checkpoint, *, files=None, **overrides):
    files = {B.path: B} if files is None else files
    values = {
        "stage_version": 1,
        "fingerprint": "f" * 64,
        "expected_paths": [B.path],
        "earlier_stage_ran": False,
        "describe": files.get,
    }
    values.update(overrides)
    return reuse_decision(checkpoint, **values)


CHECKPOINT = Checkpoint("s", 1, "f" * 64, (B,))


def test_a_verified_checkpoint_is_reused():
    assert _decide(CHECKPOINT) == ReuseDecision.REUSE


@pytest.mark.parametrize(
    ("checkpoint", "overrides", "files", "expected"),
    [
        (None, {}, None, ReuseDecision.NO_CHECKPOINT),
        (CHECKPOINT, {"earlier_stage_ran": True}, None, ReuseDecision.EARLIER_STAGE_RAN),
        (CHECKPOINT, {"stage_version": 2}, None, ReuseDecision.VERSION_CHANGED),
        (CHECKPOINT, {"fingerprint": "e" * 64}, None, ReuseDecision.INPUT_CHANGED),
        (CHECKPOINT, {"expected_paths": [B.path, A.path]}, None, ReuseDecision.OUTPUTS_CHANGED),
        (CHECKPOINT, {}, {}, ReuseDecision.OUTPUTS_CHANGED),  # file deleted
        (
            CHECKPOINT,
            {},
            {B.path: OutputFile(B.path, 20, "0" * 64)},  # same size, edited content
            ReuseDecision.OUTPUTS_CHANGED,
        ),
        (
            CHECKPOINT,
            {},
            {B.path: OutputFile(B.path, 21, B.sha256)},  # size differs
            ReuseDecision.OUTPUTS_CHANGED,
        ),
    ],
    ids=[
        "none",
        "earlier-stage-ran",
        "version",
        "fingerprint",
        "file-set",
        "deleted",
        "edited",
        "resized",
    ],
)
def test_anything_unverified_reruns_the_stage(checkpoint, overrides, files, expected):
    assert _decide(checkpoint, files=files, **overrides) == expected


def test_files_are_not_hashed_when_cheaper_checks_already_fail():
    def describe(path):
        raise AssertionError("must not read files")

    assert _decide(CHECKPOINT, stage_version=2, describe=describe) == ReuseDecision.VERSION_CHANGED


# --- Fault injection ------------------------------------------------------------------------------


class _Echo:
    name, model = "echo", "echo-1"

    def summarize(self, text, options):
        return SummaryResult(text=text, provider=self.name, model=self.model, latency_ms=0)


OPTIONS = SummarizeOptions(purpose="chunk", max_output_chars=100, timeout_seconds=1)


def test_no_option_means_no_wrapper():
    provider = _Echo()
    assert with_fault_injection(provider, {}, 1) is provider
    assert fault_plan({}) is None


@pytest.mark.parametrize(("attempt_no", "fails"), [(1, True), (2, True), (3, False)])
def test_transient_faults_fail_the_first_call_of_the_first_n_attempts(attempt_no, fails):
    options = {"fault_injection": {"mode": "transient", "failures": 2}}
    provider = with_fault_injection(_Echo(), options, attempt_no)
    if fails:
        with pytest.raises(InjectedTransientFault) as exc:
            provider.summarize("x", OPTIONS)
        assert exc.value.error_class == "injected_transient"
    assert provider.summarize("x", OPTIONS).text == "x"  # later calls go through


def test_permanent_fault_fails_once_on_every_attempt():
    provider = FaultInjectingProvider(
        _Echo(), fault_plan({"fault_injection": {"mode": "permanent"}}), 5
    )
    with pytest.raises(InjectedPermanentFault) as exc:
        provider.summarize("x", OPTIONS)
    assert exc.value.error_class == "injected_permanent"
    assert (provider.name, provider.model) == ("echo", "echo-1")


# --- Classification -------------------------------------------------------------------------------


class _PgError(Exception):
    def __init__(self, sqlstate):
        super().__init__("driver error")
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    ("exc", "error_class", "transient"),
    [
        (JobError("pdf_encrypted"), "pdf_encrypted", False),
        (JobError("provider_timeout"), "provider_timeout", True),
        (JobError("injected_transient"), "injected_transient", True),
        (OperationalError("SELECT 1", {}, _PgError("08006")), "database_unavailable", True),
        (OperationalError("UPDATE", {}, _PgError("40P01")), "database_conflict", True),  # deadlock
        (
            OperationalError("UPDATE", {}, _PgError("40001")),
            "database_conflict",
            True,
        ),  # serialization
        (OSError(errno.ENOSPC, "No space left on device"), "storage_unavailable", True),
        (OSError(errno.EIO, "I/O error"), "storage_unavailable", True),
        (FileNotFoundError(errno.ENOENT, "gone"), "internal_error", False),
        (IntegrityError("INSERT", {}, _PgError("23505")), "internal_error", False),
        (ValueError("a bug"), "internal_error", False),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, BaseException) else str(value),
)
def test_classification(exc, error_class, transient):
    failure = _classify(exc, "summarize")
    assert (failure.error_class, failure.transient, failure.stage) == (
        error_class,
        transient,
        "summarize",
    )
    assert "driver error" not in failure.summary and "bug" not in failure.summary
