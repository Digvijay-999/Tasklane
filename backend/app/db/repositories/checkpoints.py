"""Stage checkpoints: which stages of a job finished, and the files they wrote. Never commits."""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import StageCheckpoint
from app.domain.checkpoints import Checkpoint, OutputFile

checkpoints = StageCheckpoint.__table__


def get_checkpoints(session: Session, job_id: uuid.UUID) -> dict[str, Checkpoint]:
    """Every checkpoint of a job, by stage name."""
    rows = session.execute(
        select(
            checkpoints.c.stage,
            checkpoints.c.stage_version,
            checkpoints.c.input_fingerprint,
            checkpoints.c.outputs,
        ).where(checkpoints.c.job_id == job_id)
    ).all()
    return {
        row.stage: Checkpoint(
            stage=row.stage,
            stage_version=row.stage_version,
            input_fingerprint=row.input_fingerprint,
            outputs=tuple(OutputFile.from_record(record) for record in row.outputs),
        )
        for row in rows
    }


def upsert_checkpoint(
    session: Session,
    *,
    job_id: uuid.UUID,
    stage: str,
    stage_version: int,
    attempt_no: int,
    input_fingerprint: str,
    outputs: Sequence[OutputFile],
) -> None:
    """Record (or replace) the checkpoint of one stage: one row per (job, stage)."""
    values = {
        "stage_version": stage_version,
        "attempt_no": attempt_no,
        "input_fingerprint": input_fingerprint,
        "outputs": [f.to_record() for f in outputs],
    }
    statement = insert(checkpoints).values(job_id=job_id, stage=stage, **values)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[checkpoints.c.job_id, checkpoints.c.stage],
            set_={**values, "updated_at": func.now()},
        )
    )
