"""Errors raised by services. Messages must be safe to show to the user."""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # Uploads
    INVALID_FILENAME = "invalid_filename"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    CONTENT_TYPE_MISMATCH = "content_type_mismatch"
    FILE_SIGNATURE_MISMATCH = "file_signature_mismatch"
    UPLOAD_TOO_LARGE = "upload_too_large"
    EMPTY_FILE = "empty_file"
    UPLOAD_INTERRUPTED = "upload_interrupted"
    # Jobs
    IDEMPOTENCY_KEY_MISSING = "idempotency_key_missing"
    IDEMPOTENCY_KEY_INVALID = "idempotency_key_invalid"
    IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
    UPLOAD_NOT_FOUND = "upload_not_found"
    UPLOAD_NOT_READY = "upload_not_ready"
    UPLOAD_EXPIRED = "upload_expired"
    UPLOAD_KIND_MISMATCH = "upload_kind_mismatch"
    JOB_NOT_FOUND = "job_not_found"
    FAULT_INJECTION_DISABLED = "fault_injection_disabled"
    JOB_NOT_RETRYABLE = "job_not_retryable"  # details.reason says why (ManualRetryBlock)
    # Artifacts
    ARTIFACT_NOT_FOUND = "artifact_not_found"
    ARTIFACT_GONE = "artifact_gone"
    # Listing
    INVALID_CURSOR = "invalid_cursor"


class ServiceError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
