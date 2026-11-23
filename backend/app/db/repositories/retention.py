"""Rows for the cleanup command (app/maintenance/cleanup.py). Functions never commit.

Every `select_*` function returns at most `limit` candidates ordered by id, after `after` (a
keyset, so a dry run can page through rows it doesn't change). With `lock=True` the rows are
locked FOR UPDATE SKIP LOCKED: two cleanups running at once each take different rows, and a row
another transaction is busy with (a job being retried, an upload a job is being created from) is
simply left for the next run. Every decision uses the database clock.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.orm import Session

from app.db.models import (
    Artifact,
    Job,
    OutboxMessage,
    StageCheckpoint,
    Upload,
    WorkerHeartbeat,
)
from app.db.repositories.uploads import mark_rejected
from app.domain.enums import UploadRejectionReason, UploadStatus
from app.domain.state_machine import TERMINAL_STATUSES

FINISHED = sorted(status.value for status in TERMINAL_STATUSES)


def _page(statement: Any, id_column: Any, after: Any, limit: int, lock: bool) -> Any:
    if after is not None:
        statement = statement.where(id_column > after)
    statement = statement.order_by(id_column).limit(limit)
    return statement.with_for_update(skip_locked=True) if lock else statement


# --- Jobs -----------------------------------------------------------------------------------------


def select_old_finished_jobs(
    session: Session, older_than: timedelta, *, limit: int, after: Any = None, lock: bool
) -> list[uuid.UUID]:
    statement = select(Job.id).where(
        Job.status.in_(FINISHED), Job.finished_at < func.now() - older_than
    )
    return list(session.scalars(_page(statement, Job.id, after, limit, lock)))


def delete_jobs(session: Session, job_ids: Sequence[uuid.UUID]) -> int:
    """Delete finished jobs; the database cascades to attempts, events, artifacts, provider
    calls, checkpoints and outbox rows (ON DELETE CASCADE). Unfinished jobs are never matched."""
    if not job_ids:
        return 0
    result = session.execute(
        delete(Job).where(Job.id.in_(job_ids), Job.status.in_(FINISHED)).returning(Job.id)
    )
    return len(result.all())


@dataclass(frozen=True)
class JobFiles:
    """What the cleanup needs to know about a job that has a folder on disk."""

    status: str
    finished: bool
    work_expired: bool  # finished longer ago than the work-folder retention
    artifact_paths: frozenset[str]  # files registered in the artifacts table


def job_files(
    session: Session, job_ids: Sequence[uuid.UUID], work_older_than: timedelta, *, lock: bool
) -> dict[uuid.UUID, JobFiles]:
    """Status and registered artifact paths of the given jobs (missing ids are absent). With
    `lock`, the job rows are locked (SKIP LOCKED: a locked job is absent too, i.e. left alone)."""
    if not job_ids:
        return {}
    statement = select(
        Job.id,
        Job.status,
        Job.finished_at.is_not(None) & (Job.finished_at < func.now() - work_older_than),
    ).where(Job.id.in_(job_ids))
    if lock:
        statement = statement.with_for_update(skip_locked=True)
    rows = session.execute(statement).all()
    paths: dict[uuid.UUID, set[str]] = {row.id: set() for row in rows}
    for job_id, path in session.execute(
        select(Artifact.job_id, Artifact.storage_path).where(Artifact.job_id.in_(list(paths)))
    ):
        paths[job_id].add(path)
    return {
        row[0]: JobFiles(
            status=row[1],
            finished=row[1] in FINISHED,
            work_expired=bool(row[2]),
            artifact_paths=frozenset(paths[row[0]]),
        )
        for row in rows
    }


def delete_checkpoints(session: Session, job_id: uuid.UUID) -> int:
    result = session.execute(
        delete(StageCheckpoint)
        .where(StageCheckpoint.job_id == job_id)
        .returning(StageCheckpoint.stage)
    )
    return len(result.all())


# --- Uploads --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpiredUpload:
    id: uuid.UUID
    referenced: bool  # finished jobs still point at it: keep the row (marked deleted)


def select_expired_uploads(
    session: Session, *, limit: int, after: Any = None, lock: bool
) -> list[ExpiredUpload]:
    """Expired uploads that no unfinished job uses. Rows already marked deleted come back only
    once no job references them at all (then the row itself can go)."""
    jobs = Job.__table__
    referenced = exists().where(jobs.c.upload_id == Upload.id)
    unfinished = exists().where(jobs.c.upload_id == Upload.id, jobs.c.status.not_in(FINISHED))
    statement = select(Upload.id, referenced.label("referenced")).where(
        Upload.expires_at < func.now(),
        Upload.status != UploadStatus.RECEIVING,  # the interrupted-upload rule handles those
        ~unfinished,
        (Upload.status != UploadStatus.DELETED) | ~referenced,
    )
    if lock:
        statement = statement.with_for_update(of=Upload, skip_locked=True)
    if after is not None:
        statement = statement.where(Upload.id > after)
    rows = session.execute(statement.order_by(Upload.id).limit(limit)).all()
    return [ExpiredUpload(row.id, bool(row.referenced)) for row in rows]


def mark_upload_deleted(session: Session, upload_id: uuid.UUID) -> None:
    session.execute(
        update(Upload)
        .where(Upload.id == upload_id)
        .values(status=UploadStatus.DELETED, deleted_at=func.now())
        .execution_options(synchronize_session=False)
    )


def lock_jobs_of_upload(session: Session, upload_id: uuid.UUID) -> list[str]:
    """Statuses of every job using the upload, share-locked until the transaction ends. A
    manual retry (which locks the job row) either committed before (we see it unfinished) or
    waits until we have committed (and then finds the upload deleted)."""
    return list(
        session.scalars(
            select(Job.status).where(Job.upload_id == upload_id).with_for_update(read=True)
        )
    )


def delete_unreferenced_upload(session: Session, upload_id: uuid.UUID) -> bool:
    """Delete the row if no job references it (re-checked here, under our row lock)."""
    jobs = Job.__table__
    result = session.execute(
        delete(Upload)
        .where(Upload.id == upload_id, ~exists().where(jobs.c.upload_id == Upload.id))
        .returning(Upload.id)
    )
    return result.scalar_one_or_none() is not None


def select_interrupted_uploads(
    session: Session, older_than: timedelta, *, limit: int, after: Any = None, lock: bool
) -> list[uuid.UUID]:
    statement = select(Upload.id).where(
        Upload.status == UploadStatus.RECEIVING, Upload.created_at < func.now() - older_than
    )
    return list(session.scalars(_page(statement, Upload.id, after, limit, lock)))


def mark_upload_interrupted(session: Session, upload_id: uuid.UUID) -> bool:
    """receiving -> rejected (interrupted). Conditional: an upload that finished meanwhile
    stays ready."""
    return mark_rejected(session, upload_id, UploadRejectionReason.INTERRUPTED)


def upload_statuses(session: Session, upload_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not upload_ids:
        return {}
    rows = session.execute(select(Upload.id, Upload.status).where(Upload.id.in_(upload_ids)))
    return {upload_id: status for upload_id, status in rows}


# --- Outbox and worker rows -----------------------------------------------------------------------


def select_published_outbox(
    session: Session, older_than: timedelta, *, limit: int, after: Any = None, lock: bool
) -> list[int]:
    statement = select(OutboxMessage.id).where(OutboxMessage.published_at < func.now() - older_than)
    return list(session.scalars(_page(statement, OutboxMessage.id, after, limit, lock)))


def delete_outbox(session: Session, ids: Sequence[int]) -> int:
    if not ids:
        return 0
    result = session.execute(
        delete(OutboxMessage)
        .where(OutboxMessage.id.in_(ids), OutboxMessage.published_at.is_not(None))
        .returning(OutboxMessage.id)
    )
    return len(result.all())


def select_old_worker_rows(
    session: Session, older_than: timedelta, *, limit: int, after: Any = None, lock: bool
) -> list[str]:
    """Worker processes not seen for `older_than`: stopped cleanly or died. (A live worker
    refreshes last_seen_at every few seconds, and would re-create its row anyway.)"""
    statement = select(WorkerHeartbeat.worker_id).where(
        WorkerHeartbeat.last_seen_at < func.now() - older_than
    )
    return list(session.scalars(_page(statement, WorkerHeartbeat.worker_id, after, limit, lock)))


def delete_worker_rows(session: Session, worker_ids: Sequence[str]) -> int:
    if not worker_ids:
        return 0
    result = session.execute(
        delete(WorkerHeartbeat)
        .where(WorkerHeartbeat.worker_id.in_(worker_ids))
        .returning(WorkerHeartbeat.worker_id)
    )
    return len(result.all())
