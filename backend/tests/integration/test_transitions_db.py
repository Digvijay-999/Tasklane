"""transition_job against the real database: bookkeeping, cancel superseding, and races."""

import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, JobEvent
from app.db.repositories.jobs import JobNotFoundError, TransitionResult, transition_job
from app.domain.enums import ErrorClass, EventType, JobStatus
from app.domain.state_machine import (
    MAX_ATTEMPTS_LIMIT,
    TRANSITIONS,
    IllegalTransitionError,
    can_manual_retry,
    cancel_decision,
)
from tests.db_utils import backend_pid, wait_until_blocked_by
from tests.factories import make_job, make_upload, sha256_hex, utcnow

S = JobStatus


def _required_changes(to_status: JobStatus) -> dict:
    if to_status == S.RETRY_WAIT:
        return {"retry_at": utcnow() + timedelta(seconds=30)}
    if to_status == S.FAILED:
        return {"error_class": "provider_timeout", "error_summary": "The summarizer timed out."}
    if to_status == S.QUEUED:  # only reachable by manual retry, which must grant a new budget
        return {"max_attempts": 4}
    return {}


def _events(session: Session, job_id: uuid.UUID) -> list[JobEvent]:
    return list(
        session.scalars(select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.seq))
    )


@pytest.mark.parametrize(("from_status", "to_status"), sorted(TRANSITIONS))
def test_every_legal_transition_satisfies_the_database_constraints(session, from_status, to_status):
    job = make_job(session, status=from_status)
    session.commit()

    result = transition_job(
        session,
        job.id,
        from_status=from_status,
        to_status=to_status,
        changes=_required_changes(to_status),
    )
    session.commit()  # would raise if the bookkeeping broke a CHECK constraint

    assert result.applied
    assert result.status == to_status
    session.refresh(job)
    assert job.status == to_status
    status_event = _events(session, job.id)[0]
    assert status_event.seq == result.event_seq == 1
    assert status_event.event_type == EventType.JOB_STATUS_CHANGED
    assert status_event.payload["from"] == from_status
    assert status_event.payload["to"] == to_status


def test_transition_returns_not_applied_when_status_already_moved(session):
    job = make_job(session, status=S.RUNNING)
    session.commit()

    result = transition_job(session, job.id, from_status=S.QUEUED, to_status=S.RUNNING)

    assert result == TransitionResult(applied=False, status=S.RUNNING)
    assert _events(session, job.id) == []


def test_illegal_transition_raises_before_touching_the_database(session):
    job = make_job(session)
    session.commit()

    with pytest.raises(IllegalTransitionError):
        transition_job(session, job.id, from_status=S.QUEUED, to_status=S.SUCCEEDED)

    session.refresh(job)
    assert job.status == S.QUEUED
    assert job.last_event_seq == 0


def test_missing_job_raises_not_found(session):
    with pytest.raises(JobNotFoundError):
        transition_job(session, uuid.uuid4(), from_status=S.QUEUED, to_status=S.RUNNING)


@pytest.mark.parametrize(
    ("to_status", "changes", "message"),
    [
        (S.RETRY_WAIT, {}, "retry_at"),
        (S.FAILED, {"error_class": "x"}, "error_summary"),
        (S.SUCCEEDED, {"status": "failed"}, "cannot set"),
        (S.SUCCEEDED, {"last_event_seq": 99}, "cannot set"),
    ],
)
def test_invalid_changes_are_rejected_before_sql(session, to_status, changes, message):
    job = make_job(session, status=S.RUNNING)
    with pytest.raises(ValueError, match=message):
        transition_job(session, job.id, from_status=S.RUNNING, to_status=to_status, changes=changes)


def test_bookkeeping_for_a_full_lifecycle(session):
    job = make_job(session)
    session.commit()

    transition_job(
        session, job.id, from_status=S.QUEUED, to_status=S.RUNNING, changes={"attempt_count": 1}
    )
    session.commit()
    session.refresh(job)
    first_started = job.started_at
    assert first_started is not None and job.finished_at is None

    transition_job(
        session,
        job.id,
        from_status=S.RUNNING,
        to_status=S.RETRY_WAIT,
        changes=_required_changes(S.RETRY_WAIT),
    )
    session.commit()
    session.refresh(job)
    assert job.retry_at is not None

    transition_job(
        session, job.id, from_status=S.RETRY_WAIT, to_status=S.RUNNING, changes={"attempt_count": 2}
    )
    session.commit()
    session.refresh(job)
    assert job.retry_at is None  # cleared when leaving retry_wait
    assert job.started_at == first_started  # first start time is kept

    transition_job(
        session,
        job.id,
        from_status=S.RUNNING,
        to_status=S.FAILED,
        changes=_required_changes(S.FAILED),
    )
    session.commit()
    session.refresh(job)
    assert job.finished_at is not None
    assert job.error_summary

    decision = can_manual_retry(S.FAILED, job.error_class, job.attempt_count)
    transition_job(
        session,
        job.id,
        from_status=S.FAILED,
        to_status=S.QUEUED,
        changes={
            "max_attempts": decision.new_max_attempts,
            "manual_retry_count": job.manual_retry_count + 1,
        },
    )
    session.commit()
    session.refresh(job)
    # Reopened: no longer finished, error moved to history, fresh budget.
    assert job.finished_at is None
    assert job.error_class is None and job.error_summary is None
    assert job.max_attempts == 2 + 3
    assert job.manual_retry_count == 1
    assert [e.seq for e in _events(session, job.id)] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("to_status", [S.CANCEL_REQUESTED, S.CANCELLED])
def test_cancel_records_when_it_was_requested(session, to_status):
    from_status = S.RUNNING if to_status == S.CANCEL_REQUESTED else S.QUEUED
    job = make_job(session, status=from_status)
    session.commit()
    transition_job(session, job.id, from_status=from_status, to_status=to_status)
    session.commit()
    session.refresh(job)
    assert job.cancel_requested_at is not None


@pytest.mark.parametrize(
    ("to_status", "reason"),
    [(S.SUCCEEDED, "completed_before_cancel"), (S.FAILED, "failed_before_cancel")],
)
def test_cancel_superseded_by_definite_outcome_records_an_explanation(session, to_status, reason):
    job = make_job(session, status=S.CANCEL_REQUESTED)
    session.commit()

    result = transition_job(
        session,
        job.id,
        from_status=S.CANCEL_REQUESTED,
        to_status=to_status,
        changes=_required_changes(to_status),
        attempt_no=1,
    )
    session.commit()

    events = _events(session, job.id)
    assert [(e.seq, e.event_type) for e in events] == [
        (1, EventType.JOB_STATUS_CHANGED),
        (2, EventType.JOB_CANCEL_SUPERSEDED),
    ]
    assert result.event_seq == 1
    assert events[1].payload == {"outcome": to_status, "reason": reason}
    assert events[1].attempt_no == 1
    session.refresh(job)
    assert job.last_event_seq == 2
    assert job.cancel_requested_at is not None  # the request itself stays on record


def test_cancel_reaching_cancelled_records_no_superseded_event(session):
    job = make_job(session, status=S.CANCEL_REQUESTED)
    session.commit()
    transition_job(session, job.id, from_status=S.CANCEL_REQUESTED, to_status=S.CANCELLED)
    session.commit()
    assert [e.event_type for e in _events(session, job.id)] == [EventType.JOB_STATUS_CHANGED]


@pytest.mark.parametrize("attempt_count", range(1, MAX_ATTEMPTS_LIMIT + 1))
def test_manual_retry_decision_never_hits_the_database_cap(session, attempt_count):
    """Whenever can_manual_retry allows a retry, the database accepts the new budget; when the
    budget would exceed the cap, the decision refuses first."""
    job = make_job(
        session, status=S.FAILED, attempt_count=attempt_count, max_attempts=attempt_count
    )
    session.commit()

    decision = can_manual_retry(S.FAILED, job.error_class, attempt_count)
    if not decision.allowed:
        assert attempt_count + 3 > MAX_ATTEMPTS_LIMIT
        return
    result = transition_job(
        session,
        job.id,
        from_status=S.FAILED,
        to_status=S.QUEUED,
        changes={"max_attempts": decision.new_max_attempts},
    )
    session.commit()
    assert result.applied


def test_manual_retry_refused_for_input_errors_even_with_budget(session):
    job = make_job(
        session,
        status=S.FAILED,
        error_class=ErrorClass.INVALID_INPUT,
        error_summary="The PDF is corrupt.",
    )
    assert not can_manual_retry(S.FAILED, job.error_class, job.attempt_count).allowed


def test_manual_retry_without_a_new_budget_is_rejected(session):
    job = make_job(session, status=S.FAILED, attempt_count=3, max_attempts=3)
    with pytest.raises(ValueError, match="max_attempts"):
        transition_job(session, job.id, from_status=S.FAILED, to_status=S.QUEUED)


def test_manual_retry_after_superseded_cancel_starts_a_clean_run(session):
    """running -> cancel_requested -> failed (cancel superseded) -> queued (manual retry): the
    reopened job must not carry the old cancel request, stage, or progress."""
    job = make_job(session, status=S.RUNNING, current_stage="summarize", progress_percent=80)
    session.commit()
    transition_job(session, job.id, from_status=S.RUNNING, to_status=S.CANCEL_REQUESTED)
    session.commit()
    first_cancel_at = job.cancel_requested_at
    assert first_cancel_at is not None
    transition_job(
        session,
        job.id,
        from_status=S.CANCEL_REQUESTED,
        to_status=S.FAILED,
        changes=_required_changes(S.FAILED),
    )
    session.commit()

    decision = can_manual_retry(S.FAILED, "provider_timeout", job.attempt_count)
    transition_job(
        session,
        job.id,
        from_status=S.FAILED,
        to_status=S.QUEUED,
        changes={"max_attempts": decision.new_max_attempts},
    )
    session.commit()
    assert job.status == S.QUEUED  # the cached object was expired, so this reloads
    assert job.cancel_requested_at is None
    assert job.current_stage is None
    assert job.progress_percent == 0

    # The new run can be claimed and cancelled again, with a fresh request time.
    transition_job(session, job.id, from_status=S.QUEUED, to_status=S.RUNNING)
    session.commit()
    transition_job(session, job.id, from_status=S.RUNNING, to_status=S.CANCEL_REQUESTED)
    session.commit()
    assert job.cancel_requested_at > first_cancel_at


def test_success_clears_errors_from_earlier_attempts(session):
    job = make_job(
        session,
        status=S.RUNNING,
        error_class="provider_timeout",
        error_summary="Attempt 1 timed out.",
    )
    session.commit()
    transition_job(session, job.id, from_status=S.RUNNING, to_status=S.SUCCEEDED)
    session.commit()
    assert job.error_class is None and job.error_summary is None


def test_event_payload_cannot_override_from_and_to(session):
    job = make_job(session, status=S.RUNNING)
    session.commit()
    transition_job(
        session,
        job.id,
        from_status=S.RUNNING,
        to_status=S.SUCCEEDED,
        event_payload={"from": "queued", "to": "failed", "stages": 5},
    )
    session.commit()
    payload = _events(session, job.id)[0].payload
    assert payload == {"from": "running", "to": "succeeded", "stages": 5}


def test_pending_orm_changes_are_saved_not_discarded(session):
    """transition_job bypasses the ORM; it must flush pending edits first, not drop them."""
    job = make_job(session, status=S.RUNNING)
    session.commit()
    job.current_stage = "extract_text"  # unflushed edit
    transition_job(session, job.id, from_status=S.RUNNING, to_status=S.CANCEL_REQUESTED)
    session.commit()
    session.expire_all()
    assert job.current_stage == "extract_text"
    assert job.status == S.CANCEL_REQUESTED


def test_unflushed_new_job_with_explicit_id_can_be_transitioned(session):
    upload = make_upload(session)
    job = Job(
        id=uuid.uuid4(),
        workflow_type="pdf_ingest",
        upload_id=upload.id,
        input_sha256=upload.sha256,
        idempotency_key="key-unflushed",
        request_hash=sha256_hex(2),
    )
    session.add(job)  # not flushed yet: the INSERT is still pending
    result = transition_job(session, job.id, from_status=S.QUEUED, to_status=S.CANCELLED)
    session.commit()
    assert result.applied


def test_job_id_none_is_rejected_with_a_clear_message(session):
    with pytest.raises(ValueError, match="job_id is None"):
        transition_job(session, None, from_status=S.QUEUED, to_status=S.RUNNING)


def test_cached_job_is_refreshed_after_losing_a_race(engine):
    with Session(engine, expire_on_commit=False) as mine:
        job = make_job(mine)
        mine.commit()
        assert job.status == S.QUEUED  # loaded and cached in this session

        with Session(engine) as other:  # someone else claims it first
            transition_job(other, job.id, from_status=S.QUEUED, to_status=S.RUNNING)
            other.commit()

        result = transition_job(mine, job.id, from_status=S.QUEUED, to_status=S.CANCELLED)
        assert not result.applied
        assert job.status == S.RUNNING  # no manual refresh needed


# --- Races ---------------------------------------------------------------------------------------


class BlockedTransition:
    """Runs a transition in another thread/connection. The constructor returns only once that
    transition is provably blocked on the row lock held by `holder`'s open transaction."""

    def __init__(self, engine, holder: Session, job_id, from_status, to_status) -> None:
        self._outcome: dict = {}
        self._thread = threading.Thread(
            target=self._run, args=(engine, job_id, from_status, to_status)
        )
        self._thread.start()
        wait_until_blocked_by(engine, backend_pid(holder))

    def _run(self, engine, job_id, from_status, to_status) -> None:
        with Session(engine) as other:
            try:
                self._outcome["result"] = transition_job(
                    other, job_id, from_status=from_status, to_status=to_status
                )
                other.commit()
            except Exception as exc:  # re-raised in the test thread by result()
                self._outcome["error"] = exc

    def result(self) -> TransitionResult:
        self._thread.join(timeout=5)
        assert not self._thread.is_alive(), "blocked transition never finished"
        if "error" in self._outcome:
            raise self._outcome["error"]
        return self._outcome["result"]


def test_two_workers_claiming_the_same_job_only_one_wins(engine):
    with Session(engine) as setup:
        job_id = make_job(setup).id
        setup.commit()

    with Session(engine) as first:
        won = transition_job(first, job_id, from_status=S.QUEUED, to_status=S.RUNNING)
        assert won.applied
        # The second claim is now provably blocked behind our uncommitted update.
        second = BlockedTransition(engine, first, job_id, S.QUEUED, S.RUNNING)
        first.commit()

    assert second.result() == TransitionResult(applied=False, status=S.RUNNING)
    with Session(engine) as check:
        assert check.get(Job, job_id).last_event_seq == 1  # exactly one status event


def test_cancel_loses_to_claim_then_requests_cancellation(engine):
    with Session(engine) as setup:
        job_id = make_job(setup).id
        setup.commit()

    with Session(engine) as worker:
        transition_job(worker, job_id, from_status=S.QUEUED, to_status=S.RUNNING)
        api = BlockedTransition(engine, worker, job_id, S.QUEUED, S.CANCELLED)
        worker.commit()

    lost = api.result()
    assert not lost.applied and lost.status == S.RUNNING
    # The API re-evaluates with the status it now sees, as Phase 7's cancel endpoint will.
    decision = cancel_decision(lost.status)
    assert decision.target == S.CANCEL_REQUESTED
    with Session(engine) as retry:
        result = transition_job(retry, job_id, from_status=lost.status, to_status=decision.target)
        retry.commit()
    assert result.applied


def test_claim_loses_to_cancel_and_the_worker_drops_the_message(engine):
    with Session(engine) as setup:
        job_id = make_job(setup).id
        setup.commit()

    with Session(engine) as api:
        transition_job(api, job_id, from_status=S.QUEUED, to_status=S.CANCELLED)
        worker = BlockedTransition(engine, api, job_id, S.QUEUED, S.RUNNING)
        api.commit()

    assert worker.result() == TransitionResult(applied=False, status=S.CANCELLED)
    with Session(engine) as check:
        job = check.get(Job, job_id)
        assert job.status == S.CANCELLED
        assert job.started_at is None  # the job never ran
