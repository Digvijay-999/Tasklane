"""Event history route, and the SSE loop driven in-process (Phase 8).

TestClient buffers a streaming response until it ends, so it is only used here for streams that
end by themselves (finished jobs) and for errors returned before streaming. Live behaviour over a
real connection is tested in test_events_e2e.py against a real server process.
"""

import json
import logging
import uuid

import anyio
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.events import StreamTimings, event_frames
from app.db.repositories.jobs import append_event
from app.db.session import create_session_factory
from app.domain.enums import JobStatus
from app.services.events import EventService
from tests.event_utils import add_events, event_seqs, finish_job, new_job, parse_sse

FAST = StreamTimings(
    poll_seconds=0.02, keepalive_seconds=30, max_stream_seconds=60, retry_milliseconds=3000
)


def _assert_error(response, status, code):
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code and error["message"] and "request_id" in error


# --- History route --------------------------------------------------------------------------------


def test_history_pages_through_events_in_seq_order(api, engine):
    job_id = new_job(engine, events=7)
    url = f"/v1/jobs/{job_id}/events/history"

    pages, after = [], None
    while True:
        body = api.get(url, params={"limit": 3, **({"after": after} if after else {})}).json()
        pages.append([item["seq"] for item in body["items"]])
        assert body["last_seq"] == 7
        after = body["next_after"]
        if after is None:
            break
    assert pages == [[1, 2, 3], [4, 5, 6], [7]]

    item = api.get(url, params={"limit": 1}).json()["items"][0]
    assert set(item) == {"seq", "event_type", "attempt_no", "created_at", "payload"}
    assert item["event_type"] == "stage.progress" and item["payload"]["stage"] == "summarize"


def test_history_next_after_is_null_exactly_when_caught_up(api, engine):
    job_id = new_job(engine, events=4)
    url = f"/v1/jobs/{job_id}/events/history"
    assert api.get(url, params={"limit": 4}).json()["next_after"] is None  # last page is full
    assert api.get(url, params={"after": 4}).json() == {
        "items": [],
        "next_after": None,
        "last_seq": 4,
    }
    beyond = api.get(url, params={"after": 99}).json()
    assert beyond["items"] == [] and beyond["next_after"] is None


def test_history_limits(api, engine):
    job_id = new_job(engine, events=501)
    url = f"/v1/jobs/{job_id}/events/history"
    default = api.get(url).json()
    assert len(default["items"]) == 100 and default["next_after"] == 100
    assert len(api.get(url, params={"limit": 500}).json()["items"]) == 500
    for bad in (0, 501, "x"):
        _assert_error(api.get(url, params={"limit": bad}), 422, "invalid_request")


@pytest.mark.parametrize("after", ["-1", "abc", "1.5", "٥"])
def test_history_malformed_after_is_invalid_cursor(api, engine, after):
    job_id = new_job(engine, events=1)
    response = api.get(f"/v1/jobs/{job_id}/events/history", params={"after": after})
    _assert_error(response, 400, "invalid_cursor")


@pytest.mark.parametrize("path", ["events/history", "events"])
@pytest.mark.parametrize("job_id", [str(uuid.uuid4()), "not-a-uuid"])
def test_unknown_job_is_404_before_any_streaming(api, engine, path, job_id):
    response = api.get(f"/v1/jobs/{job_id}/{path}")
    _assert_error(response, 404, "job_not_found")
    assert response.headers["content-type"] == "application/json"


# --- The stream through TestClient (finished jobs only) -------------------------------------------


def _finished_job(engine, events=5) -> uuid.UUID:
    job_id = new_job(engine, events=events)
    finish_job(engine, job_id)  # one more event: running -> succeeded
    return job_id


def test_stream_headers_and_full_replay_then_stream_end(api, engine):
    job_id = _finished_job(engine)
    response = api.get(f"/v1/jobs/{job_id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["connection"] == "keep-alive"

    assert response.text.startswith("retry: 3000\n\n")
    messages = parse_sse(response.text)
    assert event_seqs(messages) == [1, 2, 3, 4, 5, 6]
    end = messages[-1]
    assert end.event == "stream.end" and end.id is None
    assert json.loads(end.data) == {"reason": "job_finished", "status": "succeeded", "last_seq": 6}


@pytest.mark.parametrize(
    ("headers", "params", "expected"),
    [
        ({}, {}, [1, 2, 3, 4, 5, 6]),  # missing: from the beginning
        ({}, {"after": "0"}, [1, 2, 3, 4, 5, 6]),
        ({"Last-Event-ID": "0"}, {}, [1, 2, 3, 4, 5, 6]),
        ({"Last-Event-ID": "4"}, {}, [5, 6]),
        ({}, {"after": "4"}, [5, 6]),
        ({"Last-Event-ID": "4"}, {"after": "1"}, [5, 6]),  # the header wins
        ({"Last-Event-ID": ""}, {"after": "5"}, [6]),  # empty header = absent
        ({}, {"after": "6"}, []),  # caught up: just stream.end
        ({"Last-Event-ID": "999"}, {}, []),  # past the end: no replay, no error
    ],
)
def test_resume_position_on_a_finished_job(api, engine, headers, params, expected):
    job_id = _finished_job(engine)
    response = api.get(f"/v1/jobs/{job_id}/events", headers=headers, params=params)
    assert response.status_code == 200
    messages = parse_sse(response.text)
    assert event_seqs(messages) == expected
    assert messages[-1].event == "stream.end"


@pytest.mark.parametrize(
    ("headers", "params"),
    [({"Last-Event-ID": "-1"}, {}), ({}, {"after": "x"}), ({"Last-Event-ID": "x"}, {"after": "1"})],
)
def test_malformed_resume_position_is_400_before_streaming(api, engine, headers, params):
    job_id = _finished_job(engine)
    response = api.get(f"/v1/jobs/{job_id}/events", headers=headers, params=params)
    _assert_error(response, 400, "invalid_cursor")


def test_hostile_names_in_a_stored_payload_cannot_inject_sse_fields(api, engine):
    hostile = {
        "filename": "evil.pdf\n\nid: 999\nevent: job.hacked\ndata: {}\n\n",
        "archive_path": "src/\r\rid: 998\r\nretry: 1\r\n",
        "title": "line sep\x85",
    }
    job_id = new_job(engine)
    add_events(engine, job_id, 1, "job.created", hostile)
    finish_job(engine, job_id)

    raw = api.get(f"/v1/jobs/{job_id}/events").text
    assert [line for line in raw.split("\n") if line.startswith("id:")] == ["id: 1", "id: 2"]
    assert [line for line in raw.split("\n") if line.startswith("retry:")] == ["retry: 3000"]
    assert "\r" not in raw and raw.isascii()
    messages = parse_sse(raw)
    assert [m.event for m in messages if m.event] == [
        "job.created",
        "job.status_changed",
        "stream.end",
    ]
    assert json.loads(messages[1].data)["payload"] == hostile


# --- The loop, driven directly --------------------------------------------------------------------


@pytest.fixture
def event_service(engine):
    return EventService(create_session_factory(engine))


def _run(frames_iter, *, until, timeout=10.0):
    """Collect frames until `until(text so far)` is true or the stream ends."""

    async def collect():
        frames = []
        with anyio.fail_after(timeout):
            async for frame in frames_iter:
                frames.append(frame)
                if until("".join(frames)):
                    break
        await frames_iter.aclose()
        return "".join(frames)

    return anyio.run(collect)


def test_events_committed_right_after_a_read_are_sent_by_the_next_poll(engine, event_service):
    """The race between "read events" and "go to sleep": an event committed in that window
    must not be lost. The wrapper commits one exactly there, after every read."""
    job_id = new_job(engine, events=3)
    reads = []

    class CommitAfterRead:
        def page(self, job_id, *, after, limit):
            page = event_service.page(job_id, after=after, limit=limit)
            reads.append(after)
            if len(reads) <= 3:  # the reads at cursor 0, 3 (+1 each time) ...
                add_events(engine, job_id, 1)
            elif len(reads) == 4:
                finish_job(engine, job_id)
            return page

    text_ = _run(
        event_frames(CommitAfterRead(), job_id, 0, FAST), until=lambda t: "stream.end" in t
    )
    messages = parse_sse(text_)
    assert event_seqs(messages) == [1, 2, 3, 4, 5, 6, 7]
    assert messages[-1].event == "stream.end"
    assert reads[:4] == [0, 3, 4, 5]  # each read starts after the last event sent


def test_uncommitted_events_are_never_streamed(engine, event_service):
    job_id = new_job(engine, events=2)
    holder = Session(engine)
    append_event(holder, job_id, "stage.progress", {"stage": "summarize", "percent": 50})
    sleeps = 0

    async def sleep(seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 5:  # several polls happened while seq 3 was pending: now commit it
            await anyio.to_thread.run_sync(holder.commit)
        await anyio.sleep(seconds)

    seen_before_commit = []

    def until(text_so_far):
        if sleeps < 5:
            seen_before_commit[:] = event_seqs(parse_sse(text_so_far))
        return "id: 3\n" in text_so_far

    try:
        text_ = _run(event_frames(event_service, job_id, 0, FAST, sleep=sleep), until=until)
    finally:
        holder.close()
    assert seen_before_commit == [1, 2]
    assert event_seqs(parse_sse(text_)) == [1, 2, 3]


def test_a_seq_gap_is_logged_and_never_silently_skipped(engine, event_service, caplog):
    job_id = new_job(engine, events=4)
    with engine.begin() as connection:  # simulate lost rows (e.g. a future retention bug)
        connection.execute(
            text("DELETE FROM job_events WHERE job_id = :id AND seq IN (2, 4)"), {"id": job_id}
        )
    finish_job(engine, job_id)  # seq 5
    with caplog.at_level(logging.ERROR, logger="app.api.events"):
        text_ = _run(
            event_frames(event_service, job_id, 0, FAST), until=lambda t: "stream.end" in t
        )
    assert event_seqs(parse_sse(text_)) == [1, 3, 5]
    gaps = [(r.expected, r.got) for r in caplog.records if r.getMessage() == "event seq gap"]
    assert gaps == [(2, 3), (4, 5)]


def test_missing_events_at_the_end_are_logged_and_do_not_spin(engine, event_service, caplog):
    job_id = new_job(engine, events=3)
    finish_job(engine, job_id)  # seq 4
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM job_events WHERE job_id = :id AND seq >= 3"), {"id": job_id}
        )
    with caplog.at_level(logging.ERROR, logger="app.api.events"):
        text_ = _run(
            event_frames(event_service, job_id, 0, FAST), until=lambda t: "stream.end" in t
        )
    assert event_seqs(parse_sse(text_)) == [1, 2]
    assert json.loads(parse_sse(text_)[-1].data)["last_seq"] == 4
    assert [(r.expected, r.got) for r in caplog.records if r.getMessage() == "event seq gap"] == [
        (3, None)
    ]


def test_a_database_outage_keeps_the_stream_open_and_it_resumes(engine, event_service, caplog):
    job_id = new_job(engine, events=2)
    failures = 4
    delays = []

    class Flaky:
        def page(self, job_id, *, after, limit):
            nonlocal failures
            if failures:
                failures -= 1
                raise OperationalError("SELECT", {}, Exception("connection refused"))
            return event_service.page(job_id, after=after, limit=limit)

    async def sleep(seconds):
        delays.append(seconds)

    timings = StreamTimings(
        poll_seconds=0.1, keepalive_seconds=0.5, max_stream_seconds=60, retry_milliseconds=3000
    )
    with caplog.at_level(logging.INFO, logger="app.api.events"):
        text_ = _run(
            event_frames(Flaky(), job_id, 0, timings, sleep=sleep),
            until=lambda t: "id: 2\n" in t,
        )
    assert event_seqs(parse_sse(text_)) == [1, 2]
    assert delays == [0.2, 0.4, 0.5, 0.5]  # backs off, capped at the keepalive interval
    warnings = [r for r in caplog.records if "database unavailable" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].error_type == "OperationalError"  # once per outage
    assert any("database available again" in r.getMessage() for r in caplog.records)
    assert "connection refused" not in caplog.text  # only the error type is logged


def test_a_job_deleted_while_streaming_ends_the_stream(engine, event_service):
    job_id = new_job(engine, events=2)

    def until(text_so_far):
        if "id: 2\n" in text_so_far and "stream.end" not in text_so_far:
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
        return "stream.end" in text_so_far

    messages = parse_sse(_run(event_frames(event_service, job_id, 0, FAST), until=until))
    assert json.loads(messages[-1].data) == {
        "reason": "job_not_found",
        "status": None,
        "last_seq": 2,
    }


def test_status_stays_untouched_by_reading(engine, event_service, api):
    """Streaming is read-only: the job row is the same before and after."""
    job_id = new_job(engine, status=JobStatus.RUNNING, events=3)
    before = api.get(f"/v1/jobs/{job_id}").json()
    _run(event_frames(event_service, job_id, 0, FAST), until=lambda t: "id: 3\n" in t)
    assert api.get(f"/v1/jobs/{job_id}").json() == before


def test_phase7_event_types_stream_and_replay_like_the_others(api, engine):
    """Carry-forward from Phase 7: these types and payload fields need no special handling."""
    phase7 = [
        (
            "attempt.lease_expired",
            {
                "attempt_no": 1,
                "worker_id": "w@h:1",
                "last_heartbeat_at": "2026-11-10T12:00:00+00:00",
                "lease_expired_at": "2026-11-10T12:01:00+00:00",
            },
        ),
        ("job.requeued", {"reason": "lost_message", "status": "queued", "last_published_at": None}),
        (
            "job.manual_retry",
            {
                "manual_retry_count": 1,
                "previous_error_class": "lease_expired",
                "attempt_count": 3,
                "max_attempts": 6,
            },
        ),
        (
            "job.status_changed",
            {
                "from": "running",
                "to": "cancelled",
                "reason": "lease_expired",
                "before_stage": "summarize",
                "outbox_removed": True,
                "requested_by": "api",
            },
        ),
    ]
    job_id = new_job(engine)
    for event_type, payload in phase7:
        add_events(engine, job_id, 1, event_type, payload)
    finish_job(engine, job_id)

    streamed = parse_sse(api.get(f"/v1/jobs/{job_id}/events").text)
    replayed = parse_sse(api.get(f"/v1/jobs/{job_id}/events", params={"after": 2}).text)
    history = api.get(f"/v1/jobs/{job_id}/events/history").json()["items"]
    expected = [(i + 1, t, p) for i, (t, p) in enumerate(phase7)]
    assert [(int(m.id), m.event, json.loads(m.data)["payload"]) for m in streamed if m.id][
        :4
    ] == expected
    assert event_seqs(replayed) == [3, 4, 5]
    assert [(i["seq"], i["event_type"], i["payload"]) for i in history[:4]] == expected
