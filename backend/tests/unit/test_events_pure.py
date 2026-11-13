"""Event stream pieces that need no database: resume position, SSE text, the parser, limits."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.events import (
    StreamSlots,
    format_event,
    format_stream_end,
    parse_resume_position,
)
from app.services.errors import ErrorCode, ServiceError
from tests.event_utils import parse_sse

HOSTILE = [
    "evil\n\nid: 999\nevent: job.hacked\ndata: {}\n\n",
    "carriage\rreturn\r\rid: 998\r",
    "crlf\r\n\r\nid: 997\r\n",
    "line separator paragraph\x85next-line",
    "nul\x00byte",
    ": comment-looking",
    "data: fake",
    "</script><img src=x onerror=alert(1)>",
]


def _event(seq=5, event_type="stage.started", payload=None, attempt_no=1):
    return SimpleNamespace(
        seq=seq,
        event_type=event_type,
        attempt_no=attempt_no,
        created_at=datetime(2026, 11, 10, 12, 0, tzinfo=UTC),
        payload=payload if payload is not None else {"stage": "extract_text"},
    )


# --- Resume position ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "query", "expected"),
    [
        (None, None, 0),  # missing: from the beginning
        ("", "", 0),  # empty counts as absent
        ("0", None, 0),
        (None, "0", 0),
        ("7", None, 7),
        (None, "7", 7),
        ("7", "3", 7),  # the header wins
        ("", "3", 3),  # an empty header falls back to the query
        ("123456789012345678", None, 123456789012345678),
    ],
)
def test_resume_position(header, query, expected):
    assert parse_resume_position(header, query) == expected


@pytest.mark.parametrize(
    ("header", "query", "source"),
    [
        ("-1", None, "Last-Event-ID"),
        ("abc", None, "Last-Event-ID"),
        ("1.5", None, "Last-Event-ID"),
        (" 5", None, "Last-Event-ID"),
        ("5\n", None, "Last-Event-ID"),
        ("٥", None, "Last-Event-ID"),  # an Arabic-Indic digit: not ASCII
        ("1234567890123456789", None, "Last-Event-ID"),  # 19 digits: may overflow bigint
        ("x", "3", "Last-Event-ID"),  # the header wins even when it's the bad one
        (None, "-3", "after"),
        (None, "1e3", "after"),
    ],
)
def test_malformed_resume_position_is_invalid_cursor(header, query, source):
    with pytest.raises(ServiceError) as caught:
        parse_resume_position(header, query)
    assert caught.value.code == ErrorCode.INVALID_CURSOR
    assert caught.value.details == {"source": source}


# --- SSE text -------------------------------------------------------------------------------------


def test_event_frame_format():
    frame = format_event(_event())
    assert frame.endswith("\n\n")
    lines = frame[:-2].split("\n")
    assert lines[:2] == ["id: 5", "event: stage.started"]
    assert lines[2].startswith("data: ") and len(lines) == 3
    assert json.loads(lines[2][len("data: ") :]) == {
        "seq": 5,
        "event_type": "stage.started",
        "attempt_no": 1,
        "created_at": "2026-11-10T12:00:00Z",
        "payload": {"stage": "extract_text"},
    }


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_names_stay_inside_one_json_data_line(name):
    payload = {"filename": name, "nested": {"path": name, "list": [name]}}
    frame = format_event(_event(payload=payload))

    lines = frame.split("\n")
    # id, event, data, then the blank line: no extra line, field, or event could be injected.
    assert len(lines) == 5 and lines[3:] == ["", ""]
    assert "\r" not in frame and " " not in frame and "\x85" not in frame
    assert frame.isascii()
    messages = parse_sse(frame)
    assert len(messages) == 1
    assert (messages[0].id, messages[0].event) == ("5", "stage.started")
    assert json.loads(messages[0].data)["payload"] == payload


def test_event_type_that_could_carry_fields_is_refused():
    # The database CHECK prevents this; the formatter refuses it anyway.
    with pytest.raises(ValueError):
        format_event(_event(event_type="job.created\nid: 999"))


def test_stream_end_frame_has_no_id():
    frame = format_stream_end("job_finished", "succeeded", 12)
    [message] = parse_sse(frame)
    assert message.id is None and message.event == "stream.end"
    assert json.loads(message.data) == {
        "reason": "job_finished",
        "status": "succeeded",
        "last_seq": 12,
    }


# --- The client's parser --------------------------------------------------------------------------


def test_parser_follows_the_sse_field_rules():
    text = (
        "retry: 3000\n\n"
        ": keepalive\n\n"
        "id: 1\nevent: a.b\ndata: one\ndata: two\n\n"
        "id:2\r\nevent:a.b\r\ndata:x\r\n\r\n"
        ": lifetime\nid: 2\n\n"
    )
    messages = parse_sse(text)
    assert messages[0].retry == 3000 and messages[0].data is None
    assert messages[1].comments == ["keepalive"]
    assert (messages[2].id, messages[2].event, messages[2].data) == ("1", "a.b", "one\ntwo")
    assert (messages[3].id, messages[3].data) == ("2", "x")
    assert messages[4].id == "2" and messages[4].data is None


# --- Limits ---------------------------------------------------------------------------------------


def test_stream_slots_count_up_to_the_limit_and_release():
    slots = StreamSlots(2)
    assert slots.try_acquire() and slots.try_acquire()
    assert not slots.try_acquire()
    slots.release()
    assert slots.try_acquire()
    assert slots.in_use == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"sse_keepalive_seconds": 0.2, "sse_poll_seconds": 0.5},
        {"sse_max_stream_seconds": 5, "sse_keepalive_seconds": 15},
        {"sse_max_streams": 0},
        {"sse_poll_seconds": 0},
        {"sse_retry_milliseconds": 10},
        {"sse_write_timeout_seconds": 0},
    ],
)
def test_invalid_sse_settings_are_refused_at_startup(unit_settings, overrides):
    with pytest.raises(ValidationError):
        type(unit_settings)(**{**unit_settings.model_dump(), **overrides})


def test_default_sse_settings(unit_settings):
    assert unit_settings.sse_max_streams == 50
    assert unit_settings.sse_max_stream_seconds == 3600
    assert unit_settings.sse_poll_seconds == 0.5
    assert unit_settings.sse_keepalive_seconds == 15
    assert unit_settings.sse_retry_milliseconds == 3000


@pytest.mark.parametrize("failure", ["generator_raises", "client_disconnects"])
def test_stream_slot_is_released_however_the_stream_ends(failure):
    import anyio

    from app.api.events import EventStreamResponse

    slots = StreamSlots(1)
    assert slots.try_acquire()
    closed = []

    async def frames():
        try:
            yield "retry: 3000\n\n"
            if failure == "generator_raises":
                raise ValueError("unexpected event type format")
            await anyio.sleep(30)
        finally:
            closed.append(True)

    async def receive():
        if failure == "client_disconnects":
            await anyio.sleep(0.05)
            return {"type": "http.disconnect"}
        await anyio.sleep(30)

    async def send(message):
        pass

    async def run():
        response = EventStreamResponse(frames(), on_close=slots.release, write_timeout=5)
        scope = {"type": "http", "asgi": {"spec_version": "2.3"}}
        with anyio.fail_after(5):
            if failure == "generator_raises":
                with pytest.raises(ValueError):
                    await response(scope, receive, send)
            else:
                await response(scope, receive, send)

    anyio.run(run)
    assert slots.in_use == 0 and closed == [True]
