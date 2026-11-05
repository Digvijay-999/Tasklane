"""Idempotency-Key format and request fingerprinting."""

import pytest

from app.domain.idempotency import canonical_json, is_valid_key, request_hash


@pytest.mark.parametrize(
    ("key", "valid"),
    [
        ("a" * 16, True),  # shortest allowed
        ("a" * 128, True),  # longest allowed
        ("a" * 15, False),
        ("a" * 129, False),
        ("3f2504e0-4f89-11d3-9a0c-0305e82c3301", True),  # a UUID
        ("order-2026.11.03:retry_1", True),
        ("has spaces in the key", False),
        ("emoji-key-🙂-0000000", False),
        ("semicolon;key;000000", False),
        ("trailing-newline-key\n", False),
        ("", False),
    ],
)
def test_key_format(key, valid):
    assert is_valid_key(key) == valid


def test_canonical_json_ignores_key_order_and_whitespace():
    assert canonical_json({"b": 1, "a": {"y": 2, "x": 3}}) == '{"a":{"x":3,"y":2},"b":1}'


def test_hash_is_stable_across_key_order():
    first = {"workflow_type": "pdf_ingest", "upload_id": "abc", "options": {}}
    second = {"options": {}, "upload_id": "abc", "workflow_type": "pdf_ingest"}
    assert request_hash(first) == request_hash(second)


def test_hash_differs_when_a_value_differs():
    assert request_hash({"upload_id": "a"}) != request_hash({"upload_id": "b"})


def test_omitting_a_field_and_sending_its_default_are_different_requests():
    """Documented behavior: only fields the client sent are hashed, defaults are not filled in."""
    assert request_hash({"upload_id": "a"}) != request_hash({"upload_id": "a", "options": {}})


def test_hash_is_hex_sha256():
    digest = request_hash({})
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
