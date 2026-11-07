"""Provider call records. Functions never commit; the caller owns the transaction."""

import uuid

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.db.models import ProviderCall


def insert_provider_call(
    session: Session,
    *,
    job_id: uuid.UUID,
    attempt_no: int,
    stage: str,
    purpose: str,
    provider: str,
    model: str,
    latency_ms: int,
    success: bool,
    input_chars: int,
    output_chars: int | None = None,
    chunk_id: str | None = None,
    error_class: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
) -> None:
    session.execute(
        insert(ProviderCall.__table__).values(
            job_id=job_id,
            attempt_no=attempt_no,
            stage=stage,
            chunk_id=chunk_id,
            purpose=purpose,
            provider=provider,
            model=model,
            latency_ms=latency_ms,
            success=success,
            error_class=error_class,
            input_chars=input_chars,
            output_chars=output_chars,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
    )
