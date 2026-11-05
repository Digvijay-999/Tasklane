"""Cursor encoding for keyset pagination."""

import base64
import uuid
from datetime import UTC, datetime

import pytest

from app.api.pagination import decode_cursor, encode_cursor, filters_fingerprint
from app.domain.listing import JobListPosition
from app.services.errors import ErrorCode, ServiceError

POSITION = JobListPosition(datetime(2026, 11, 3, 12, 30, 45, 123456, tzinfo=UTC), uuid.uuid4())


def test_round_trip_keeps_microseconds_and_id():
    fingerprint = filters_fingerprint(["queued"], [])
    assert decode_cursor(encode_cursor(POSITION, fingerprint), fingerprint) == POSITION


def test_cursor_is_url_safe():
    cursor = encode_cursor(POSITION, filters_fingerprint([], []))
    assert all(c.isalnum() or c in "-_" for c in cursor)


def test_fingerprint_ignores_filter_order_and_duplicates():
    assert filters_fingerprint(["failed", "queued"], ["pdf_ingest"]) == filters_fingerprint(
        ["queued", "failed", "queued"], ["pdf_ingest"]
    )


def test_cursor_made_for_other_filters_is_rejected():
    cursor = encode_cursor(POSITION, filters_fingerprint(["queued"], []))
    with pytest.raises(ServiceError) as exc:
        decode_cursor(cursor, filters_fingerprint(["failed"], []))
    assert exc.value.code == ErrorCode.INVALID_CURSOR
    assert "different filters" in exc.value.message


ZERO_UUID = b"00000000-0000-0000-0000-000000000000"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64-!!!",
        _b64(b"not json"),
        _b64(b'"a string"'),
        _b64(b"[1, 2]"),
        _b64(b'{"t": "2026-11-03T12:00:00+00:00", "i": "not-a-uuid", "f": "x"}'),
        _b64(b'{"t": "yesterday", "i": "00000000-0000-0000-0000-000000000000", "f": "x"}'),
        _b64(
            b'{"t": "2026-11-03T12:00:00", "i": "00000000-0000-0000-0000-000000000000", "f": "x"}'
        ),
        _b64(b'{"i": "00000000-0000-0000-0000-000000000000"}'),
        # Fields of the wrong type (uuid.UUID(1) raises AttributeError, which must not be a 500)
        _b64(b'{"t": "2026-01-01T00:00:00+00:00", "i": 1, "f": "x"}'),
        _b64(b'{"t": "2026-01-01T00:00:00+00:00", "i": ["a"], "f": "x"}'),
        _b64(b'{"t": 5, "i": "00000000-0000-0000-0000-000000000000", "f": "x"}'),
        _b64(b'{"t": "2026-01-01T00:00:00+00:00", "i": "' + ZERO_UUID + b'", "f": 7}'),
        _b64(b"null"),
        "é",
    ],
)
def test_malformed_cursors_are_rejected(cursor):
    with pytest.raises(ServiceError) as exc:
        decode_cursor(cursor, "x")
    assert exc.value.code == ErrorCode.INVALID_CURSOR
