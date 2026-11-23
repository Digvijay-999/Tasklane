"""Provider call records. Functions never commit; the caller owns the transaction."""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, insert, select
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


@dataclass(frozen=True)
class ModelUsage:
    """One job's provider calls with one provider and model (all attempts)."""

    provider: str
    model: str
    calls: int
    failed_calls: int
    prompt_tokens: int | None  # sums of what the provider reported; None if it reported none
    completion_tokens: int | None
    total_tokens: int | None


def usage_by_model(session: Session, job_id: uuid.UUID) -> list[ModelUsage]:
    """Calls and reported tokens per provider and model, in the order they were first used."""
    calls = ProviderCall.__table__.c
    rows = session.execute(
        select(
            calls.provider,
            calls.model,
            func.count(),
            func.count().filter(calls.success.is_(False)),
            func.sum(calls.prompt_tokens),
            func.sum(calls.completion_tokens),
            func.sum(calls.total_tokens),
        )
        .where(calls.job_id == job_id)
        .group_by(calls.provider, calls.model)
        .order_by(func.min(calls.id))
    )
    return [ModelUsage(*row) for row in rows]
