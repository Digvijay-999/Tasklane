"""Upload rows. Functions never commit; the caller owns the transaction."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import Upload
from app.domain.enums import UploadRejectionReason, UploadStatus


def insert_receiving(
    session: Session,
    *,
    upload_id: uuid.UUID,
    storage_path: str,
    original_filename: str,
    kind: str,
    content_type: str,
    expires_at: datetime,
) -> Upload:
    """Record an upload that is about to stream in. Size and hash are filled in when ready."""
    upload = Upload(
        id=upload_id,
        storage_path=storage_path,
        original_filename=original_filename,
        kind=kind,
        content_type=content_type,
        status=UploadStatus.RECEIVING,
        expires_at=expires_at,
    )
    session.add(upload)
    session.flush()
    return upload


def mark_ready(session: Session, upload_id: uuid.UUID, *, sha256: str, size_bytes: int) -> bool:
    """receiving -> ready (conditional, like job transitions). Returns False if not receiving."""
    result = session.execute(
        update(Upload)
        .where(Upload.id == upload_id, Upload.status == UploadStatus.RECEIVING)
        .values(status=UploadStatus.READY, sha256=sha256, size_bytes=size_bytes)
        .returning(Upload.id)
        .execution_options(synchronize_session=False)
    )
    return result.scalar_one_or_none() is not None


def mark_rejected(session: Session, upload_id: uuid.UUID, reason: UploadRejectionReason) -> bool:
    """receiving -> rejected with a reason. Returns False if the upload wasn't receiving."""
    result = session.execute(
        update(Upload)
        .where(Upload.id == upload_id, Upload.status == UploadStatus.RECEIVING)
        .values(status=UploadStatus.REJECTED, rejection_reason=reason)
        .returning(Upload.id)
        .execution_options(synchronize_session=False)
    )
    return result.scalar_one_or_none() is not None


@dataclass(frozen=True)
class UploadForJob:
    upload: Upload
    expired: bool  # computed with the database clock


def lock_upload_for_job(session: Session, upload_id: uuid.UUID) -> UploadForJob | None:
    """Load an upload for job creation, holding a share lock until the transaction ends.

    FOR SHARE lets any number of job creations read the row at once but blocks a concurrent
    UPDATE/DELETE (e.g. retention marking it deleted) until this transaction finishes.
    """
    row = session.execute(
        select(Upload, (Upload.expires_at <= func.now()).label("expired"))
        .where(Upload.id == upload_id)
        .with_for_update(read=True)
    ).one_or_none()
    if row is None:
        return None
    return UploadForJob(upload=row.Upload, expired=bool(row.expired))


def original_filenames(session: Session, upload_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Display filenames for a page of jobs, in one query (primary-key lookups)."""
    if not upload_ids:
        return {}
    rows = session.execute(
        select(Upload.id, Upload.original_filename).where(Upload.id.in_(upload_ids))
    )
    return {upload_id: filename for upload_id, filename in rows}
