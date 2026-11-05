"""Idempotency-Key rules and request fingerprinting (pure functions).

A client sends the same Idempotency-Key when it retries a request, so a retry after a timeout or
dropped connection returns the original job instead of creating a second one.

- The key must match KEY_PATTERN (16-128 characters; a UUID fits).
- The request is fingerprinted with request_hash(): SHA-256 of a canonical JSON form of the
  fields the client actually sent. Key order and whitespace don't matter. Fields the client left
  out are NOT filled with defaults first, so a later server version adding a defaulted option
  doesn't change the fingerprint of an old request. The flip side: sending a field explicitly
  with its default value and omitting it are different requests.
- Keys live as long as their job row (no separate expiry).
"""

import hashlib
import json
import re
from typing import Any

KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{16,128}")
KEY_FORMAT_DESCRIPTION = "16 to 128 characters: letters, digits, and . _ : - (a UUID works well)"


def is_valid_key(key: str) -> bool:
    return KEY_PATTERN.fullmatch(key) is not None


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace, non-ASCII escaped as \\uXXXX.

    Escaping keeps the output pure ASCII, so it always encodes, even for strings Python allows but
    UTF-8 can't represent (lone surrogates such as "\\ud800").
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def request_hash(sent_fields: dict[str, Any]) -> str:
    """SHA-256 (hex) of the canonical form of the fields the client sent."""
    return hashlib.sha256(canonical_json(sent_fields).encode("utf-8")).hexdigest()
