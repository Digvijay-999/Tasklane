"""Opaque cursors for keyset pagination of GET /v1/jobs.

A cursor encodes where the previous page ended (the last job's created_at and id) plus a
fingerprint of the filters it was made for. It is base64url JSON, not encrypted or signed:
tampering with it can only change what *you* see of your own data, and anything malformed is
rejected with 400 invalid_cursor.
"""

import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Iterable
from datetime import datetime

from app.domain.idempotency import canonical_json
from app.domain.listing import JobListPosition
from app.services.errors import ErrorCode, ServiceError


def filters_fingerprint(statuses: Iterable[str], workflow_types: Iterable[str]) -> str:
    """Short hash of the filters, so a cursor can't be reused with different filters."""
    filters = {"status": sorted(set(statuses)), "workflow_type": sorted(set(workflow_types))}
    return hashlib.sha256(canonical_json(filters).encode()).hexdigest()[:16]


def encode_cursor(position: JobListPosition, fingerprint: str) -> str:
    payload = {"t": position.created_at.isoformat(), "i": str(position.job_id), "f": fingerprint}
    return base64.urlsafe_b64encode(canonical_json(payload).encode()).decode().rstrip("=")


def decode_cursor(cursor: str, fingerprint: str) -> JobListPosition:
    malformed = _invalid("The cursor is malformed. Start again without a cursor.")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeEncodeError):
        raise malformed from None
    # Check the shape before parsing: e.g. uuid.UUID(1) raises AttributeError, not ValueError.
    if not isinstance(payload, dict) or not all(
        isinstance(payload.get(field), str) for field in ("t", "i", "f")
    ):
        raise malformed
    try:
        created_at = datetime.fromisoformat(payload["t"])
        job_id = uuid.UUID(payload["i"])
    except ValueError:
        raise malformed from None
    cursor_fingerprint = payload["f"]
    if created_at.tzinfo is None:
        raise malformed
    if cursor_fingerprint != fingerprint:
        raise _invalid(
            "This cursor was created with different filters. Use the same status and "
            "workflow_type filters as the first page, or start again without a cursor."
        )
    return JobListPosition(created_at=created_at, job_id=job_id)


def _invalid(message: str) -> ServiceError:
    return ServiceError(ErrorCode.INVALID_CURSOR, message)
