"""Request and response models for the HTTP API (Pydantic, like zod schemas in Node)."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import WorkflowType
from app.domain.state_machine import MAX_ATTEMPTS_LIMIT

# --- Job options ----------------------------------------------------------------------------------
# One model per workflow. Unknown fields are rejected, so a typo can't silently be ignored.


class TransientFaults(BaseModel):
    """Fail the first provider call of attempts 1..failures with a transient error."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["transient"]
    failures: int = Field(ge=1, le=MAX_ATTEMPTS_LIMIT)


class PermanentFault(BaseModel):
    """Fail the first provider call with a permanent error (never retried)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["permanent"]


FaultInjection = Annotated[TransientFaults | PermanentFault, Field(discriminator="mode")]


class PdfIngestOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Development only, and refused unless the server has ENABLE_FAULT_INJECTION=true.
    fault_injection: FaultInjection | None = None


class RepoIndexOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Development only, and refused unless the server has ENABLE_FAULT_INJECTION=true.
    fault_injection: FaultInjection | None = None


OPTIONS_MODELS: dict[WorkflowType, type[BaseModel]] = {
    WorkflowType.PDF_INGEST: PdfIngestOptions,
    WorkflowType.REPO_INDEX: RepoIndexOptions,
}


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_type: WorkflowType
    upload_id: uuid.UUID
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Workflow-specific options. pdf_ingest and repo_index: `fault_injection` "
        "(development only).",
    )


# --- Responses ------------------------------------------------------------------------------------


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any]
    request_id: str | None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class UploadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    kind: str
    content_type: str
    size_bytes: int
    sha256: str
    status: str
    created_at: datetime
    expires_at: datetime


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_type: str
    status: str
    current_stage: str | None
    progress_percent: int
    upload_id: uuid.UUID
    input_sha256: str
    options: dict[str, Any]
    attempt_count: int
    max_attempts: int
    manual_retry_count: int
    retry_at: datetime | None
    cancel_requested_at: datetime | None
    error_class: str | None
    error_summary: str | None
    last_event_seq: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobUploadSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    kind: str
    size_bytes: int | None
    sha256: str | None
    status: str


class AttemptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attempt_no: int
    trigger: str
    worker_id: str
    task_id: str | None
    started_at: datetime
    heartbeat_at: datetime
    lease_expires_at: datetime
    ended_at: datetime | None
    outcome: str | None
    error_class: str | None
    error_summary: str | None


class ArtifactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    artifact_type: str
    attempt_no: int
    filename: str
    size_bytes: int
    content_type: str
    sha256: str
    created_at: datetime
    download_url: str = Field(description="GET this URL to download the file.")


class LeaseResponse(BaseModel):
    """The open attempt's lease (only while a worker holds the job)."""

    attempt_no: int
    worker_id: str
    heartbeat_at: datetime
    lease_expires_at: datetime
    expired: bool = Field(
        description="True when the lease ran out (by the database clock) and recovery has not "
        "acted yet: the worker is presumed dead."
    )


class JobDetailResponse(JobResponse):
    upload: JobUploadSummary
    attempts: list[AttemptResponse]
    artifacts: list[ArtifactResponse]
    lease: LeaseResponse | None
    can_cancel: bool = Field(description="POST .../cancel would change something.")
    cancel_blocked_reason: str | None = Field(
        description="When can_cancel is false: already_cancel_requested or already_finished."
    )
    can_retry: bool = Field(description="POST .../retry would be accepted.")
    retry_blocked_reason: str | None = Field(
        description="When can_retry is false: not_failed, input_error or retry_limit_reached."
    )


class JobListResponse(BaseModel):
    items: list[JobResponse]
    next_cursor: str | None = Field(
        description="Pass as ?cursor= to get the next page; null when there are no more."
    )
