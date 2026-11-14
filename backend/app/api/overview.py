"""GET /v1/overview (dashboard summary) and GET /v1/meta (limits and rules the UI must not
hardcode)."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.health import (
    CheckResult,
    get_broker_check,
    get_check_executor,
    get_check_timeout,
    run_checks,
)
from app.api.schemas import ErrorResponse
from app.domain.enums import WorkflowType
from app.domain.state_machine import MAX_ATTEMPTS_LIMIT
from app.domain.uploads import (
    ACCEPTED_DECLARED_TYPES,
    DETECTED_CONTENT_TYPE,
    KIND_BY_EXTENSION,
    MAX_FILENAME_LENGTH,
    SIGNATURES,
    WORKFLOW_UPLOAD_KIND,
)
from app.domain.workflows import WORKFLOW_STAGES
from app.services.health import DependencyCheck
from app.services.overview import OverviewService

router = APIRouter(prefix="/v1", tags=["dashboard"])


# --- Overview -------------------------------------------------------------------------------------


class OldestQueuedResponse(BaseModel):
    job_id: uuid.UUID
    queued_seconds: float = Field(description="Time since it entered `queued` (database clock).")


class QueueResponse(BaseModel):
    broker: CheckResult = Field(description="Whether Redis answered PING within the deadline.")
    pending_messages: int = Field(
        description="Unpublished outbox messages that are due, or whose publishing already failed."
    )
    scheduled_messages: int = Field(
        description="Unpublished messages, never tried, waiting for a later retry time."
    )
    failing_messages: int = Field(description="Unpublished messages whose last publish failed.")
    max_dispatch_attempts: int
    last_dispatch_error: str | None = Field(
        description="Error type (never a message) of the most-retried failing message."
    )
    oldest_pending_seconds: float | None = Field(
        description="How long the oldest pending message has waited to be published (a failing "
        "one since it was written)."
    )


class OverviewResponse(BaseModel):
    counts: dict[str, int] = Field(description="Jobs per status; every status is present.")
    total: int
    oldest_queued: OldestQueuedResponse | None
    queue: QueueResponse
    database_time: datetime


@router.get(
    "/overview",
    response_model=OverviewResponse,
    responses={503: {"model": ErrorResponse}},
)
def overview(
    request: Request,
    broker_check: Annotated[DependencyCheck, Depends(get_broker_check)],
    timeout: Annotated[float, Depends(get_check_timeout)],
    executor: Annotated[ThreadPoolExecutor, Depends(get_check_executor)],
) -> OverviewResponse:
    # The database read raises (-> 503 service_unavailable) when PostgreSQL is down: without it
    # there is nothing current to show. Redis being down is reported, not an error.
    service: OverviewService = request.app.state.overview_service
    found = service.read()
    broker = run_checks({"queue": broker_check}, timeout, executor)["queue"]
    outbox = found.outbox
    return OverviewResponse(
        counts=found.counts,
        total=sum(found.counts.values()),
        oldest_queued=found.oldest_queued
        and OldestQueuedResponse(
            job_id=found.oldest_queued.job_id, queued_seconds=found.oldest_queued.queued_seconds
        ),
        queue=QueueResponse(
            broker=broker,
            pending_messages=outbox.pending,
            scheduled_messages=outbox.scheduled,
            failing_messages=outbox.failing,
            max_dispatch_attempts=outbox.max_dispatch_attempts,
            last_dispatch_error=outbox.last_dispatch_error,
            oldest_pending_seconds=outbox.oldest_pending_seconds,
        ),
        database_time=found.database_time,
    )


# --- Metadata -------------------------------------------------------------------------------------


class StageInfo(BaseModel):
    name: str
    weight: int = Field(description="Share of the job's progress bar, in percent.")


class WorkflowInfo(BaseModel):
    workflow_type: str
    upload_kind: str
    extensions: list[str]
    upload_content_type: str = Field(description="The Content-Type to send when uploading.")
    accepted_content_types: list[str]
    max_upload_bytes: int
    signature_hex: str = Field(description="The file must start with these bytes.")
    stages: list[StageInfo]


class MetaResponse(BaseModel):
    workflows: list[WorkflowInfo]
    max_filename_length: int
    upload_ttl_hours: int
    fault_injection_enabled: bool
    max_fault_failures: int


@router.get("/meta", response_model=MetaResponse)
def meta(request: Request) -> MetaResponse:
    """Upload limits, accepted types and stages per workflow, read from the running server's
    settings, so the dashboard validates against the real limits."""
    settings = request.app.state.settings
    max_bytes = {"pdf": settings.max_pdf_upload_bytes, "zip": settings.max_zip_upload_bytes}
    workflows = []
    for workflow_type in WorkflowType:
        kind = WORKFLOW_UPLOAD_KIND[workflow_type]
        workflows.append(
            WorkflowInfo(
                workflow_type=workflow_type.value,
                upload_kind=kind.value,
                extensions=sorted(ext for ext, k in KIND_BY_EXTENSION.items() if k == kind),
                upload_content_type=DETECTED_CONTENT_TYPE[kind],
                accepted_content_types=sorted(ACCEPTED_DECLARED_TYPES[kind]),
                max_upload_bytes=max_bytes[kind.value],
                signature_hex=SIGNATURES[kind].hex(),
                stages=[StageInfo(name=n, weight=w) for n, w in WORKFLOW_STAGES[workflow_type]],
            )
        )
    return MetaResponse(
        workflows=workflows,
        max_filename_length=MAX_FILENAME_LENGTH,
        upload_ttl_hours=settings.upload_ttl_hours,
        fault_injection_enabled=settings.enable_fault_injection,
        max_fault_failures=MAX_ATTEMPTS_LIMIT,
    )
