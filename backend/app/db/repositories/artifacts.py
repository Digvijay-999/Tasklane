"""Artifact rows. Functions never commit; the caller owns the transaction."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Artifact

artifacts = Artifact.__table__


def upsert_artifact(
    session: Session,
    *,
    job_id: uuid.UUID,
    attempt_no: int,
    artifact_type: str,
    storage_path: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    sha256: str,
) -> None:
    """Insert the artifact, or replace the existing one of the same type for this job.

    The unique constraint (job_id, artifact_type) plus ON CONFLICT ... DO UPDATE means rerunning
    a stage replaces its artifact row instead of adding a second one.
    """
    values = {
        "storage_path": storage_path,
        "filename": filename,
        "size_bytes": size_bytes,
        "content_type": content_type,
        "sha256": sha256,
        "attempt_no": attempt_no,
    }
    statement = insert(artifacts).values(
        id=uuid.uuid4(), job_id=job_id, artifact_type=artifact_type, **values
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[artifacts.c.job_id, artifacts.c.artifact_type],
            set_={**values, "created_at": func.now()},
        )
    )


def get_artifact(session: Session, job_id: uuid.UUID, artifact_id: uuid.UUID) -> Artifact | None:
    """An artifact, only if it belongs to the given job."""
    return session.scalars(
        select(Artifact).where(Artifact.id == artifact_id, Artifact.job_id == job_id)
    ).one_or_none()
