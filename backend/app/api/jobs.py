"""Job routes: create (idempotent), list (cursor pagination), and detail."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError

from app.api.pagination import decode_cursor, encode_cursor, filters_fingerprint
from app.api.schemas import (
    OPTIONS_MODELS,
    CreateJobRequest,
    ErrorResponse,
    JobDetailResponse,
    JobListResponse,
    JobResponse,
)
from app.domain.enums import JobStatus, WorkflowType
from app.domain.listing import JobListPosition
from app.services.errors import ErrorCode, ServiceError
from app.services.jobs import CreateJobCommand, JobService

router = APIRouter(prefix="/v1", tags=["jobs"])

REPLAY_HEADER = "Idempotent-Replayed"
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def _service(request: Request) -> JobService:
    return request.app.state.job_service


@router.post(
    "/jobs",
    status_code=201,
    response_model=JobResponse,
    responses={
        200: {"model": JobResponse, "description": "Idempotent replay of an earlier request"},
        400: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
def create_job(
    body: CreateJobRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description="Required. 16-128 characters of letters, digits, and . _ : - "
            "(a UUID works). Reuse it when retrying the same request.",
        ),
    ] = None,
) -> JobResponse:
    options = _validate_options(body)
    sent_fields = body.model_dump(mode="json", exclude_unset=True)
    if "options" in sent_fields:
        # Normalized option values, still without defaults the client didn't send.
        sent_fields["options"] = options.model_dump(mode="json", exclude_unset=True)

    result = _service(request).create(
        CreateJobCommand(
            idempotency_key=idempotency_key,
            workflow_type=body.workflow_type,
            upload_id=body.upload_id,
            # Options that are off (None) are left out of what the job stores.
            resolved_options=options.model_dump(mode="json", exclude_none=True),
            sent_fields=sent_fields,
        )
    )
    if result.created:
        response.status_code = 201
        response.headers["Location"] = f"/v1/jobs/{result.job.id}"
        response.headers[REPLAY_HEADER] = "false"
    else:
        response.status_code = 200
        response.headers[REPLAY_HEADER] = "true"
    return JobResponse.model_validate(result.job)


@router.get(
    "/jobs",
    response_model=JobListResponse,
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def list_jobs(
    request: Request,
    status: Annotated[list[JobStatus] | None, Query(description="Repeatable.")] = None,
    workflow_type: Annotated[list[WorkflowType] | None, Query(description="Repeatable.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(description="next_cursor from the previous page.")] = None,
) -> JobListResponse:
    statuses = [s.value for s in status or []]
    workflow_types = [w.value for w in workflow_type or []]
    fingerprint = filters_fingerprint(statuses, workflow_types)
    after = decode_cursor(cursor, fingerprint) if cursor else None

    page = _service(request).list(
        statuses=statuses, workflow_types=workflow_types, after=after, limit=limit
    )
    next_cursor = None
    if page.has_more:
        last = page.items[-1]
        next_cursor = encode_cursor(JobListPosition(last.created_at, last.id), fingerprint)
    return JobListResponse(
        items=[JobResponse.model_validate(job) for job in page.items], next_cursor=next_cursor
    )


@router.get(
    "/jobs/{job_id}",
    response_model=JobDetailResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_job(job_id: str, request: Request) -> JobDetailResponse:
    try:
        parsed_id = uuid.UUID(job_id)
    except ValueError:
        # A malformed id can't name an existing job, so this is a plain "not found".
        raise ServiceError(ErrorCode.JOB_NOT_FOUND, "No job exists with this id.") from None
    detail = _service(request).detail(parsed_id)
    return JobDetailResponse.model_validate(
        {
            **JobResponse.model_validate(detail.job).model_dump(),
            "upload": detail.upload,
            "attempts": detail.attempts,
            "artifacts": [
                {
                    "id": a.id,
                    "artifact_type": a.artifact_type,
                    "attempt_no": a.attempt_no,
                    "filename": a.filename,
                    "size_bytes": a.size_bytes,
                    "content_type": a.content_type,
                    "sha256": a.sha256,
                    "created_at": a.created_at,
                    "download_url": f"/v1/jobs/{parsed_id}/artifacts/{a.id}",
                }
                for a in detail.artifacts
            ],
        }
    )


@router.get(
    "/jobs/{job_id}/artifacts/{artifact_id}",
    response_class=FileResponse,
    responses={
        200: {"description": "The artifact file", "content": {"application/octet-stream": {}}},
        404: {"model": ErrorResponse},
        410: {"model": ErrorResponse},
    },
)
def download_artifact(job_id: str, artifact_id: str, request: Request) -> FileResponse:
    try:
        parsed_job, parsed_artifact = uuid.UUID(job_id), uuid.UUID(artifact_id)
    except ValueError:
        raise ServiceError(
            ErrorCode.ARTIFACT_NOT_FOUND, "No artifact with this id for this job."
        ) from None
    artifact = request.app.state.artifact_service.open(parsed_job, parsed_artifact)
    return FileResponse(
        artifact.path,
        media_type=artifact.content_type,
        filename=artifact.filename,  # Content-Disposition: attachment (RFC 5987 if non-ASCII)
        headers={
            "X-Content-Type-Options": "nosniff",  # browsers must not guess another type
            "ETag": f'"{artifact.sha256}"',
            "Cache-Control": "private, no-cache",
        },
    )


def _validate_options(body: CreateJobRequest) -> BaseModel:
    """Validate `options` with the model for this workflow, reporting errors at body.options."""
    model = OPTIONS_MODELS[body.workflow_type]
    try:
        return model.model_validate(body.options)
    except ValidationError as exc:
        raise RequestValidationError(
            [{**error, "loc": ("body", "options", *error["loc"])} for error in exc.errors()]
        ) from None
