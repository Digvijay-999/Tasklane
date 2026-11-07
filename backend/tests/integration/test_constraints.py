"""Every database constraint rejects the bad data it exists to stop.

Each case inserts (or updates) something that violates exactly one constraint and checks that
Postgres rejects it *with that constraint's name*. A meta-test at the bottom fails if a constraint
or unique index exists in the database without a case here.
"""

import uuid
from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Job, JobAttempt, JobEvent, OutboxMessage, Upload
from app.domain.enums import AttemptOutcome, JobStatus, UploadStatus
from app.domain.state_machine import MAX_ATTEMPTS_LIMIT
from tests.factories import (
    make_artifact,
    make_attempt,
    make_event,
    make_job,
    make_outbox,
    make_provider_call,
    make_upload,
    utcnow,
)

Case = Callable[[Session], object]


def _duplicate_storage_path(s: Session) -> None:
    first = make_upload(s)
    make_upload(s, storage_path=first.storage_path)


def _duplicate_idempotency_key(s: Session) -> None:
    first = make_job(s)
    make_job(s, idempotency_key=first.idempotency_key)


def _delete_referenced_upload(s: Session) -> None:
    job = make_job(s)
    s.execute(delete(Upload).where(Upload.id == job.upload_id))


def _two_open_attempts(s: Session) -> None:
    job = make_job(s, status=JobStatus.RUNNING)
    make_attempt(s, job, attempt_no=1)
    make_attempt(s, job, attempt_no=2)  # first one never ended


def _duplicate_attempt_no(s: Session) -> None:
    job = make_job(s, status=JobStatus.RUNNING)
    make_attempt(s, job, attempt_no=1, ended_at=utcnow(), outcome=AttemptOutcome.LEASE_EXPIRED)
    make_attempt(s, job, attempt_no=1)


def _duplicate_event_seq(s: Session) -> None:
    job = make_job(s)
    s.expunge(make_event(s, job, seq=1))
    make_event(s, job, seq=1)


def _duplicate_artifact_type(s: Session) -> None:
    job = make_job(s)
    make_artifact(s, job)
    make_artifact(s, job)


def _duplicate_artifact_path(s: Session) -> None:
    job = make_job(s)
    first = make_artifact(s, job, artifact_type="summary")
    make_artifact(s, job, artifact_type="manifest", storage_path=first.storage_path)


def _two_pending_outbox_rows(s: Session) -> None:
    job = make_job(s)
    make_outbox(s, job)
    make_outbox(s, job)


def _with_job(build: Callable[[Session, Job], object]) -> Case:
    return lambda s: build(s, make_job(s))


def _missing_job() -> Job:
    """An unsaved Job whose id exists nowhere, for foreign-key tests."""
    return Job(id=uuid.uuid4())


def _duplicate_pk(make_first: Case, make_second: Callable[[Session, object], object]) -> Case:
    """Insert a row, then another with the same primary key.

    The first object is detached from the session first; otherwise SQLAlchemy's identity map
    would refuse the duplicate before Postgres ever saw it.
    """

    def build(s: Session) -> None:
        first = make_first(s)
        s.expunge(first)
        make_second(s, first)

    return build


_ENDED = {"ended_at": utcnow(), "outcome": AttemptOutcome.SUCCEEDED}


CASES: dict[str, Case] = {
    # uploads
    "pk_uploads": _duplicate_pk(
        make_upload, lambda s, first: make_upload(s, id=first.id, storage_path="uploads/other")
    ),
    "uq_uploads_storage_path": _duplicate_storage_path,
    "ck_uploads_kind": lambda s: make_upload(s, kind="exe"),
    "ck_uploads_status": lambda s: make_upload(s, status="lost"),
    "ck_uploads_size_positive": lambda s: make_upload(s, size_bytes=0),
    "ck_uploads_sha256_format": lambda s: make_upload(s, sha256="ABC123"),
    "ck_uploads_filename_length": lambda s: make_upload(s, original_filename=""),
    "ck_uploads_expiry_after_creation": lambda s: make_upload(
        s, expires_at=utcnow() - timedelta(days=1)
    ),
    "ck_uploads_deleted_consistent": lambda s: make_upload(s, status=UploadStatus.DELETED),
    # jobs
    "pk_jobs": _duplicate_pk(make_job, lambda s, first: make_job(s, id=first.id)),
    "uq_jobs_idempotency_key": _duplicate_idempotency_key,
    "fk_jobs_upload_id_uploads": _delete_referenced_upload,
    "ck_jobs_status": lambda s: make_job(s, status="paused"),
    "ck_jobs_workflow_type": lambda s: make_job(s, workflow_type="video_transcode"),
    "ck_jobs_progress_range": lambda s: make_job(s, progress_percent=101),
    "ck_jobs_attempt_budget": lambda s: make_job(s, attempt_count=4, max_attempts=3),
    "ck_jobs_manual_retry_count_nonneg": lambda s: make_job(s, manual_retry_count=-1),
    "ck_jobs_finished_iff_terminal": lambda s: make_job(
        s, status=JobStatus.SUCCEEDED, finished_at=None
    ),
    "ck_jobs_failed_has_error": lambda s: make_job(s, status=JobStatus.FAILED, error_summary=None),
    "ck_jobs_retry_wait_has_retry_at": lambda s: make_job(
        s, status=JobStatus.RETRY_WAIT, retry_at=None
    ),
    "ck_jobs_cancel_has_timestamp": lambda s: make_job(
        s, status=JobStatus.CANCEL_REQUESTED, cancel_requested_at=None
    ),
    "ck_jobs_input_sha256_format": lambda s: make_job(s, input_sha256="not-a-hash"),
    "ck_jobs_request_hash_format": lambda s: make_job(s, request_hash="0" * 63),
    "ck_jobs_key_length": lambda s: make_job(s, idempotency_key=""),
    "ck_jobs_last_event_seq_nonneg": lambda s: make_job(s, last_event_seq=-1),
    "ck_jobs_options_object": lambda s: make_job(s, options=["not", "an", "object"]),
    # job_attempts
    "pk_job_attempts": _with_job(
        lambda s, j: _duplicate_pk(
            lambda s2: make_attempt(s2, j, **_ENDED),
            lambda s2, first: make_attempt(s2, j, id=first.id, attempt_no=2),
        )(s)
    ),
    "fk_job_attempts_job_id_jobs": lambda s: make_attempt(s, _missing_job()),
    "uq_job_attempts_job_id_attempt_no": _duplicate_attempt_no,
    "uq_job_attempts_one_open_per_job": _two_open_attempts,
    "ck_job_attempts_attempt_no_positive": _with_job(lambda s, j: make_attempt(s, j, attempt_no=0)),
    "ck_job_attempts_trigger": _with_job(lambda s, j: make_attempt(s, j, trigger="whim")),
    "ck_job_attempts_outcome": _with_job(
        lambda s, j: make_attempt(s, j, ended_at=utcnow(), outcome="exploded")
    ),
    "ck_job_attempts_ended_iff_outcome": _with_job(
        lambda s, j: make_attempt(s, j, ended_at=utcnow(), outcome=None)
    ),
    "ck_job_attempts_lease_after_start": _with_job(
        lambda s, j: make_attempt(s, j, lease_expires_at=utcnow() - timedelta(minutes=5))
    ),
    "ck_job_attempts_heartbeat_after_start": _with_job(
        lambda s, j: make_attempt(s, j, heartbeat_at=utcnow() - timedelta(minutes=5))
    ),
    # job_events
    "pk_job_events": _duplicate_event_seq,
    "fk_job_events_job_id_jobs": lambda s: make_event(s, _missing_job()),
    "ck_job_events_seq_positive": _with_job(lambda s, j: make_event(s, j, seq=0)),
    "ck_job_events_event_type_format": _with_job(
        lambda s, j: make_event(s, j, event_type="Job Created!")
    ),
    "ck_job_events_payload_object": _with_job(lambda s, j: make_event(s, j, payload=[1, 2])),
    # artifacts
    "pk_artifacts": _with_job(
        lambda s, j: _duplicate_pk(
            lambda s2: make_artifact(s2, j),
            lambda s2, first: make_artifact(s2, j, id=first.id, artifact_type="manifest"),
        )(s)
    ),
    "fk_artifacts_job_id_jobs": lambda s: make_artifact(s, _missing_job()),
    "uq_artifacts_job_id_artifact_type": _duplicate_artifact_type,
    "uq_artifacts_storage_path": _duplicate_artifact_path,
    "ck_artifacts_artifact_type": _with_job(lambda s, j: make_artifact(s, j, artifact_type="zip")),
    "ck_artifacts_size_nonneg": _with_job(lambda s, j: make_artifact(s, j, size_bytes=-1)),
    "ck_artifacts_sha256_format": _with_job(lambda s, j: make_artifact(s, j, sha256="xyz")),
    "ck_artifacts_attempt_no_positive": _with_job(lambda s, j: make_artifact(s, j, attempt_no=0)),
    # outbox
    "pk_outbox": _with_job(
        lambda s, j: _duplicate_pk(
            lambda s2: make_outbox(s2, j, published_at=utcnow()),
            lambda s2, first: make_outbox(s2, j, id=first.id),
        )(s)
    ),
    "fk_outbox_job_id_jobs": lambda s: make_outbox(s, _missing_job()),
    "uq_outbox_one_pending_per_job": _two_pending_outbox_rows,
    "ck_outbox_task_name": _with_job(lambda s, j: make_outbox(s, j, task_name="rm_rf")),
    "ck_outbox_dispatch_attempts_nonneg": _with_job(
        lambda s, j: make_outbox(s, j, dispatch_attempts=-1)
    ),
    "ck_outbox_payload_object": _with_job(lambda s, j: make_outbox(s, j, payload=[])),
}


# Phase 3 (migration 0002) upload constraints.
CASES["ck_uploads_ready_has_content"] = lambda s: make_upload(
    s, status=UploadStatus.READY, sha256=None
)
CASES["ck_uploads_rejected_has_reason"] = lambda s: make_upload(s, status=UploadStatus.REJECTED)
CASES["ck_uploads_reason_only_when_rejected"] = lambda s: make_upload(
    s, status=UploadStatus.READY, rejection_reason="too_large"
)
CASES["ck_uploads_rejection_reason"] = lambda s: make_upload(
    s, status=UploadStatus.REJECTED, rejection_reason="bored"
)

# Phase 5 (migration 0003): provider call records.
CASES["pk_provider_calls"] = _with_job(
    lambda s, j: _duplicate_pk(
        lambda s2: make_provider_call(s2, j),
        lambda s2, first: make_provider_call(s2, j, id=first.id),
    )(s)
)
CASES["fk_provider_calls_job_id_jobs"] = lambda s: make_provider_call(s, _missing_job())
CASES["ck_provider_calls_attempt_no_positive"] = _with_job(
    lambda s, j: make_provider_call(s, j, attempt_no=0)
)
CASES["ck_provider_calls_purpose"] = _with_job(
    lambda s, j: make_provider_call(s, j, purpose="vibes")
)
CASES["ck_provider_calls_latency_nonneg"] = _with_job(
    lambda s, j: make_provider_call(s, j, latency_ms=-1)
)
CASES["ck_provider_calls_chars_nonneg"] = _with_job(
    lambda s, j: make_provider_call(s, j, input_chars=-1)
)
CASES["ck_provider_calls_tokens_nonneg"] = _with_job(
    lambda s, j: make_provider_call(s, j, total_tokens=-5)
)
CASES["ck_provider_calls_success_iff_no_error"] = _with_job(
    lambda s, j: make_provider_call(s, j, success=True, error_class="provider_timeout")
)

CASES["ck_jobs_no_cancel_while_active"] = lambda s: make_job(
    s, status=JobStatus.QUEUED, cancel_requested_at=utcnow()
)

# Extra cases for constraints with more than one way to break them, e.g. both directions of an
# "if and only if" CHECK.
MORE_CASES: list[tuple[str, str, Case]] = [
    (
        "ck_provider_calls_success_iff_no_error",
        "failure without an error class",
        _with_job(lambda s, j: make_provider_call(s, j, success=False, error_class=None)),
    ),
    (
        "ck_provider_calls_chars_nonneg",
        "negative output characters",
        _with_job(lambda s, j: make_provider_call(s, j, output_chars=-1)),
    ),
    (
        "ck_uploads_ready_has_content",
        "ready without a size",
        lambda s: make_upload(s, status=UploadStatus.READY, size_bytes=None),
    ),
    (
        "ck_uploads_deleted_consistent",
        "deleted_at set but status ready",
        lambda s: make_upload(s, status=UploadStatus.READY, deleted_at=utcnow()),
    ),
    (
        "ck_jobs_finished_iff_terminal",
        "finished_at set on an active job",
        lambda s: make_job(s, status=JobStatus.RUNNING, finished_at=utcnow()),
    ),
    (
        "ck_job_attempts_ended_iff_outcome",
        "outcome set but not ended",
        _with_job(lambda s, j: make_attempt(s, j, outcome=AttemptOutcome.SUCCEEDED)),
    ),
    (
        "ck_jobs_failed_has_error",
        "empty error summary",
        lambda s: make_job(s, status=JobStatus.FAILED, error_summary=""),
    ),
    (
        "ck_jobs_no_cancel_while_active",
        "running job with a cancel timestamp",
        lambda s: make_job(s, status=JobStatus.RUNNING, cancel_requested_at=utcnow()),
    ),
    (
        "ck_jobs_no_cancel_while_active",
        "retry_wait job with a cancel timestamp",
        lambda s: make_job(s, status=JobStatus.RETRY_WAIT, cancel_requested_at=utcnow()),
    ),
]

ALL_CASES = [(name, "", case) for name, case in sorted(CASES.items())] + MORE_CASES


@pytest.mark.parametrize(
    ("constraint_name", "variant", "case"),
    ALL_CASES,
    ids=[f"{name}{'-' + variant if variant else ''}" for name, variant, _ in ALL_CASES],
)
def test_constraint_rejects_bad_data(session, constraint_name, variant, case):
    with pytest.raises(IntegrityError) as exc:
        case(session)
        session.flush()
    assert exc.value.orig.diag.constraint_name == constraint_name


def test_every_constraint_and_unique_index_has_a_test(engine):
    """Adding a constraint without a test case here fails this test."""
    with engine.connect() as connection:
        constraints = connection.execute(
            text(
                # contype 'n' = NOT NULL constraints, listed here from PostgreSQL 18 onward.
                "SELECT conname FROM pg_constraint WHERE connamespace = 'public'::regnamespace "
                "AND conrelid <> 'alembic_version'::regclass AND contype <> 'n'"
            )
        ).scalars()
        unique_indexes = connection.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND indexdef LIKE 'CREATE UNIQUE INDEX%' AND indexname LIKE 'uq_%'"
            )
        ).scalars()
        names = set(constraints) | set(unique_indexes)
    assert names - set(CASES) == set(), "constraints without a test case"
    assert set(CASES) - names == set(), "test cases for constraints that don't exist"


# --- Valid edge cases are accepted ---------------------------------------------------------------


def test_attempt_budget_accepts_the_cap(session):
    make_job(session, max_attempts=MAX_ATTEMPTS_LIMIT, attempt_count=MAX_ATTEMPTS_LIMIT)


def test_attempt_budget_rejects_one_above_the_cap(session):
    with pytest.raises(IntegrityError) as exc:
        make_job(session, max_attempts=MAX_ATTEMPTS_LIMIT + 1)
    assert exc.value.orig.diag.constraint_name == "ck_jobs_attempt_budget"


@pytest.mark.parametrize("status", list(JobStatus))
def test_every_status_is_storable_with_its_consistent_fields(session, status):
    make_job(session, status=status)


def test_a_job_can_have_many_finished_attempts_and_one_open_one(session):
    job = make_job(session, status=JobStatus.RUNNING, attempt_count=3)
    for attempt_no in (1, 2):
        make_attempt(
            session,
            job,
            attempt_no=attempt_no,
            ended_at=utcnow(),
            outcome=AttemptOutcome.FAILED_TRANSIENT,
        )
    make_attempt(session, job, attempt_no=3)


def test_published_outbox_rows_do_not_block_a_new_pending_row(session):
    job = make_job(session)
    make_outbox(session, job, published_at=utcnow())
    make_outbox(session, job, published_at=utcnow())
    make_outbox(session, job)  # the one pending row


def test_other_jobs_may_reuse_artifact_types(session):
    make_artifact(session, make_job(session))
    make_artifact(session, make_job(session))


def test_upload_marked_deleted_with_timestamp_is_accepted(session):
    make_upload(session, status=UploadStatus.DELETED, deleted_at=utcnow())


def test_deleting_a_job_removes_its_dependent_rows(session):
    job = make_job(session, status=JobStatus.RUNNING)
    make_attempt(session, job)
    make_event(session, job)
    make_artifact(session, job)
    make_outbox(session, job)
    upload_id = job.upload_id

    session.execute(delete(Job).where(Job.id == job.id))
    for model in (JobAttempt, JobEvent, OutboxMessage):
        assert session.query(model).filter_by(job_id=job.id).count() == 0
    # ...but the upload stays (RESTRICT protects it while jobs exist; it is not cascaded).
    assert session.get(Upload, upload_id) is not None


def test_status_update_to_unknown_value_is_rejected(session):
    job = make_job(session)
    with pytest.raises(IntegrityError) as exc:
        session.execute(update(Job).where(Job.id == job.id).values(status="done"))
    assert exc.value.orig.diag.constraint_name == "ck_jobs_status"
