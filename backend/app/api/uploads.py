"""POST /v1/uploads: stream a PDF or ZIP to storage.

The request body is the raw file (not a multipart form), so the server can check the size and
signature while the bytes arrive instead of after the whole file has been buffered:

    POST /v1/uploads?filename=report.pdf
    Content-Type: application/pdf
    <file bytes>
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, Request
from starlette.requests import ClientDisconnect

from app.api.schemas import ErrorResponse, UploadResponse
from app.services.errors import ErrorCode, ServiceError
from app.services.uploads import UploadInterruptedError, UploadService

router = APIRouter(prefix="/v1", tags=["uploads"])

UPLOADS_PATH = "/v1/uploads"

_BINARY_BODY = {
    "requestBody": {
        "required": True,
        "description": "The raw file bytes (not a multipart form).",
        "content": {
            "application/pdf": {"schema": {"type": "string", "format": "binary"}},
            "application/zip": {"schema": {"type": "string", "format": "binary"}},
        },
    }
}


@router.post(
    "/uploads",
    status_code=201,
    response_model=UploadResponse,
    responses={
        400: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        415: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
    openapi_extra=_BINARY_BODY,
)
async def create_upload(
    request: Request,
    filename: Annotated[
        str, Query(description="Original filename, for display only; must end in .pdf or .zip.")
    ],
) -> UploadResponse:
    # `async def` because reading a streamed body is asynchronous. The service moves blocking
    # disk and database work onto worker threads.
    service: UploadService = request.app.state.upload_service
    try:
        upload = await service.receive(
            filename=filename,
            declared_content_type=request.headers.get("content-type"),
            content_length=_declared_length(request),
            chunks=_client_chunks(request),
        )
    except UploadInterruptedError:
        # The client is gone, so nobody will read this; respond anyway for completeness.
        raise ServiceError(
            ErrorCode.UPLOAD_INTERRUPTED, "The upload was interrupted before it finished."
        ) from None
    return UploadResponse.model_validate(upload)


async def _client_chunks(request: Request) -> AsyncIterator[bytes]:
    """The body as it arrives, translating Starlette's disconnect into a service-level error."""
    try:
        async for chunk in request.stream():
            yield chunk
    except ClientDisconnect:
        raise UploadInterruptedError from None


def _declared_length(request: Request) -> int | None:
    value = request.headers.get("content-length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None  # ignored: the limit is enforced on the bytes actually received anyway
