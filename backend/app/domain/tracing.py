"""W3C traceparent values as stored in the database (jobs.traceparent, outbox.traceparent).

Pure parsing only; creating and continuing spans is app/observability/tracing.py.
"""

import re

# version 00 - 32 hex trace id - 16 hex parent span id - 2 hex flags (W3C Trace Context).
_TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}")
_ZERO_TRACE = "0" * 32


def trace_id_from_traceparent(traceparent: str | None) -> str | None:
    """The 32-hex-digit trace id, or None if the value is missing or not a valid traceparent."""
    match = _TRACEPARENT.fullmatch(traceparent or "")
    if match is None or match.group(1) == _ZERO_TRACE:
        return None
    return match.group(1)
