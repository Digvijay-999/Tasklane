import json
import logging

import pytest

from app.observability.logging import (
    JsonFormatter,
    RequestIdFilter,
    _incoming_request_id,
    configure_logging,
    request_id_var,
)


def test_generates_request_id_header(make_client):
    response = make_client().get("/health/live")
    assert len(response.headers["X-Request-ID"]) == 32


def test_echoes_valid_incoming_request_id(make_client):
    response = make_client().get("/health/live", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"


@pytest.mark.parametrize("bad_id", ["x" * 65, "has spaces", "semi;colon", ""])
def test_replaces_invalid_incoming_request_id(make_client, bad_id):
    response = make_client().get("/health/live", headers={"X-Request-ID": bad_id})
    assert response.headers["X-Request-ID"] != bad_id
    assert len(response.headers["X-Request-ID"]) == 32


def test_logs_one_json_line_per_request(make_client, caplog):
    caplog.set_level(logging.INFO, logger="tasklane.request")
    make_client().get("/health/live?token=secret-value", headers={"X-Request-ID": "req-1"})

    records = [r for r in caplog.records if r.name == "tasklane.request"]
    assert len(records) == 1
    line = json.loads(JsonFormatter().format(records[0]))
    assert line["message"] == "request"
    assert line["method"] == "GET"
    assert line["path"] == "/health/live"
    assert line["status"] == 200
    assert line["request_id"] == "req-1"
    assert "secret-value" not in json.dumps(line)  # query strings are not logged


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"abc-123", "abc-123"),
        (b"abc\n", None),  # a trailing newline must not slip through
        (b"a" * 65, None),
        (b"", None),
    ],
)
def test_incoming_request_id_validation(raw, expected):
    scope = {"type": "http", "headers": [(b"x-request-id", raw)]}
    assert _incoming_request_id(scope) == expected


def test_configure_logging_is_idempotent():
    configure_logging("INFO")
    configure_logging("INFO")
    ours = [h for h in logging.getLogger().handlers if h.get_name() == "tasklane-json"]
    assert len(ours) == 1


def test_formatter_includes_request_id_from_context():
    record = logging.makeLogRecord({"name": "t", "levelname": "INFO", "msg": "hello"})
    token = request_id_var.set("ctx-42")
    try:
        RequestIdFilter().filter(record)
    finally:
        request_id_var.reset(token)
    line = json.loads(JsonFormatter().format(record))
    assert line["message"] == "hello"
    assert line["level"] == "INFO"
    assert line["request_id"] == "ctx-42"
