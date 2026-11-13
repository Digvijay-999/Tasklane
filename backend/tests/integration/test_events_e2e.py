"""The event stream over real connections to a real API server process (Phase 8).

TestClient buffers streaming responses, so these tests start `uvicorn` as a separate process on a
free port (test database, test storage folder) and talk to it over sockets: live delivery,
disconnects (clean close and abrupt reset), reconnects with Last-Event-ID / ?after=, limits,
keepalives, and a real job run by the real worker and dispatcher. Run only these with
`pytest -m e2e -k events`.
"""

import http.client
import json
import os
import queue
import signal
import socket
import struct
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.repositories.jobs import append_event
from app.domain.enums import JobStatus
from devtools.sse_client import SseMessage, iter_sse, open_stream
from tests.event_utils import add_events, finish_job, new_job
from tests.integration import test_e2e_pipeline as pipeline_tests

pytestmark = pytest.mark.e2e

# Shared with the pipeline tests: the real worker/dispatcher fixture, and its larger upload limits.
api_settings = pipeline_tests.api_settings
start_pipeline = pipeline_tests.start_pipeline
events_of, submit_job, wait_until = (
    pipeline_tests.events_of,
    pipeline_tests.submit_job,
    pipeline_tests.wait_until,
)

BACKEND = Path(__file__).resolve().parents[2]


# --- A real server --------------------------------------------------------------------------------


@dataclass
class ApiServer:
    url: str
    log: Path
    process: subprocess.Popen

    def events_url(self, job_id: uuid.UUID) -> str:
        return f"{self.url}/v1/jobs/{job_id}/events"

    def records(self) -> list[dict]:
        out = []
        for line in self.log.read_text(errors="replace").splitlines():
            if line.startswith("{"):
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out

    def stream_closed(self, job_id: uuid.UUID) -> list[dict]:
        return [
            r
            for r in self.records()
            if r.get("message") == "event stream closed" and r.get("job_id") == str(job_id)
        ]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def api_server(engine, api_settings, dev_settings, tmp_path) -> Iterator[Callable[..., ApiServer]]:
    assert api_settings.storage_root != dev_settings.storage_root
    servers: list[ApiServer] = []

    def start(**settings: object) -> ApiServer:
        port = _free_port()
        log = tmp_path / f"api-{port}.log"
        env = {
            **{k: v for k, v in os.environ.items() if "API_KEY" not in k.upper()},
            "POSTGRES_DB": api_settings.postgres_db,  # the test database
            "STORAGE_ROOT": str(api_settings.storage_root),
            "LOG_LEVEL": "INFO",
            "PYTHONUNBUFFERED": "1",
            **{name.upper(): str(value) for name, value in settings.items()},
        }
        with log.open("ab") as log_file:  # the child keeps its own copy of the handle
            process = subprocess.Popen(
                ["uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1"]
                + ["--port", str(port), "--no-access-log", "--timeout-graceful-shutdown", "1"],
                cwd=BACKEND,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        server = ApiServer(f"http://127.0.0.1:{port}", log, process)
        servers.append(server)

        def live() -> bool:
            try:
                return httpx.get(f"{server.url}/health/live", timeout=1).status_code == 200
            except httpx.HTTPError:
                return False

        try:
            wait_until(live, 30, "API server live")
        except TimeoutError:
            print(log.read_text(errors="replace")[-3000:])
            raise
        return server

    yield start
    for server in servers:
        if server.process.poll() is None:
            os.killpg(server.process.pid, signal.SIGTERM)
        try:
            server.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(server.process.pid, signal.SIGKILL)
            server.process.wait(timeout=5)


# --- A streaming client ---------------------------------------------------------------------------


class Reader:
    """Reads an SSE response in a background thread, so tests can wait with timeouts."""

    def __init__(self, url: str, *, after: int | None = None, last_event_id: int | None = None):
        self.connection, self.response = open_stream(
            url, after=after, last_event_id=last_event_id, timeout=60
        )
        self.sock = self.connection.sock
        self.status = self.response.status
        self.items: queue.Queue[tuple[float, SseMessage | None]] = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        if self.status == 200:
            self.thread.start()

    def _run(self) -> None:
        try:
            for message in iter_sse(self.response):
                self.items.put((time.monotonic(), message))
        except (OSError, ValueError, http.client.HTTPException):
            pass
        self.items.put((time.monotonic(), None))  # end of stream

    def next(self, timeout: float = 10, *, events_only: bool = True) -> SseMessage | None:
        """The next message (None at end of stream); raises queue.Empty on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            _, message = self.items.get(timeout=max(0.01, deadline - time.monotonic()))
            if message is None or not events_only or message.event is not None:
                return message

    def events(self, count: int, timeout: float = 10) -> list[SseMessage]:
        out = [self.next(timeout) for _ in range(count)]
        assert None not in out, "stream ended early"
        return out  # type: ignore[return-value]

    def until_end(self, timeout: float = 30) -> list[SseMessage]:
        """Every event up to and including stream.end; the server must then close."""
        out: list[SseMessage] = []
        while True:
            message = self.next(timeout)
            assert message is not None, f"closed without stream.end after {seqs(out)}"
            out.append(message)
            if message.event == "stream.end":
                assert self.next(5, events_only=False) is None, "still open after stream.end"
                return out

    def nothing_for(self, seconds: float) -> None:
        with pytest.raises(queue.Empty):
            self.next(seconds)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # FIN now, and wakes the reader thread
        except OSError:
            pass
        if self.thread.is_alive():
            self.thread.join(5)  # it sees the end of the stream and stops reading
        self.response.close()
        self.connection.close()


def seqs(messages: list[SseMessage]) -> list[int]:
    return [int(m.id) for m in messages if m.id is not None and m.data is not None]


def raw_get(server: ApiServer, path: str, *, rcvbuf: int | None = None) -> socket.socket:
    """A bare socket that sent a GET (for abrupt resets and clients that never read)."""
    sock = socket.socket()
    if rcvbuf:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    sock.settimeout(15)  # a regression fails the test instead of hanging the run
    sock.connect(("127.0.0.1", int(server.url.rsplit(":", 1)[1])))
    sock.sendall(f"GET {path} HTTP/1.1\r\nHost: test\r\n\r\n".encode())
    return sock


def read_until(sock: socket.socket, marker: bytes) -> bytes:
    data = b""
    while marker not in data:
        chunk = sock.recv(65536)
        assert chunk, f"server closed before {marker!r}: {data[-300:]!r}"
        data += chunk
    return data


def reset(sock: socket.socket) -> None:
    """Close with RST instead of FIN: the client vanished."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


def can_connect_soon(server: ApiServer, job_id: uuid.UUID, timeout: float = 3) -> float:
    """Seconds until a new stream is accepted (not 503)."""
    started = time.monotonic()

    def accepted() -> bool:
        reader = Reader(server.events_url(job_id))
        reader.close()
        return reader.status == 200

    wait_until(accepted, timeout, "a free stream slot")
    return time.monotonic() - started


# --- Acceptance #6: disconnect, reconnect, exactly the missing events -----------------------------


@pytest.mark.parametrize("resume_with", ["last_event_id", "after"])
@pytest.mark.parametrize("missed", ["already_stored", "produced_while_away"])
def test_reconnect_receives_exactly_the_missing_events_through_stream_end(
    api_server, engine, resume_with, missed
):
    server = api_server(sse_poll_seconds=0.1)
    job_id = new_job(engine, events=6 if missed == "already_stored" else 3)

    first = Reader(server.events_url(job_id))
    received = first.events(3)
    first.close()
    last_id = int(received[-1].id)
    assert last_id == 3
    wait_until(lambda: server.stream_closed(job_id), 5, "server noticed the disconnect")

    if missed == "produced_while_away":
        add_events(engine, job_id, 3)  # seq 4-6 while nobody listens

    second = Reader(server.events_url(job_id), **{resume_with: last_id})
    replayed = second.events(3)
    add_events(engine, job_id, 2)  # live: 7, 8
    finish_job(engine, job_id)  # 9 and the end
    rest = second.until_end()
    second.close()

    all_seqs = seqs(received) + seqs(replayed) + seqs(rest)
    assert all_seqs == list(range(1, 10))  # in order, no duplicates, no gaps
    assert rest[-1].event == "stream.end"
    assert json.loads(rest[-1].data)["last_seq"] == 9


def test_a_real_job_streams_from_start_to_end_exactly_as_stored(
    api, api_server, engine, start_pipeline
):
    server = api_server(sse_poll_seconds=0.1)
    start_pipeline(stub_delay_seconds=0.2)
    job_id = uuid.UUID(submit_job(api, "pdf_ingest"))

    reader = Reader(server.events_url(job_id))
    streamed = reader.until_end(timeout=90)
    reader.close()

    stored = events_of(engine, str(job_id))
    assert stored[-1].payload["to"] == "succeeded"
    assert [(int(m.id), m.event, json.loads(m.data)) for m in streamed[:-1]] == [
        (
            e.seq,
            e.event_type,
            {
                "seq": e.seq,
                "event_type": e.event_type,
                "attempt_no": e.attempt_no,
                "created_at": e.created_at.isoformat().replace("+00:00", "Z"),
                "payload": e.payload,
            },
        )
        for e in stored
    ]
    assert json.loads(streamed[-1].data) == {
        "reason": "job_finished",
        "status": "succeeded",
        "last_seq": stored[-1].seq,
    }


def test_disconnecting_never_affects_a_running_job(api, api_server, engine, start_pipeline):
    server = api_server(sse_max_streams=1)
    start_pipeline(stub_delay_seconds=0.5)
    job_id = uuid.UUID(submit_job(api, "pdf_ingest"))

    sock = raw_get(server, f"/v1/jobs/{job_id}/events")
    read_until(sock, b"stage.started")
    reset(sock)  # the client vanishes mid-job

    closed = wait_until(lambda: server.stream_closed(job_id), 5, "stream task ended")
    assert closed[0]["reason"] == "disconnected"
    can_connect_soon(server, job_id)  # the only slot was released

    wait_until(lambda: api.get(f"/v1/jobs/{job_id}").json()["status"] == "succeeded", 90, "done")
    types = [e.event_type for e in events_of(engine, str(job_id))]
    assert not any("cancel" in t for t in types)
    assert api.get(f"/v1/jobs/{job_id}").json()["attempt_count"] == 1


# --- Live delivery --------------------------------------------------------------------------------


def test_live_events_arrive_within_about_one_poll_interval(api_server, engine):
    poll = 0.2
    server = api_server(sse_poll_seconds=poll)
    job_id = new_job(engine, events=1)
    reader = Reader(server.events_url(job_id))
    reader.events(1)

    delays = []
    for _ in range(5):
        time.sleep(poll * 1.3)  # land at different points of the poll cycle
        committed = time.monotonic()
        add_events(engine, job_id, 1)
        message = None
        while message is None or message.event is None:
            arrived, message = reader.items.get(timeout=5)
        delays.append(arrived - committed)
    reader.close()
    assert max(delays) < poll + 0.5, delays


def test_nothing_uncommitted_is_ever_streamed(api_server, engine):
    server = api_server(sse_poll_seconds=0.1)
    job_id = new_job(engine, events=1)
    reader = Reader(server.events_url(job_id))
    reader.events(1)

    with Session(engine) as holder:
        append_event(holder, job_id, "stage.progress", {"stage": "summarize", "percent": 10})
        reader.nothing_for(1.0)  # ten polls while seq 2 exists only inside this transaction
        holder.commit()
    assert int(reader.next(5).id) == 2
    reader.close()


# --- Resume edge cases on a real server -----------------------------------------------------------


def test_resume_position_edge_cases_on_a_real_server(api_server, engine):
    server = api_server(sse_poll_seconds=0.1)
    job_id = new_job(engine, events=3)
    url = server.events_url(job_id)

    for kwargs in ({}, {"after": 0}, {"last_event_id": 0}):
        reader = Reader(url, **kwargs)
        assert seqs(reader.events(3)) == [1, 2, 3]
        reader.close()

    future = Reader(url, last_event_id=999)  # past the end: no replay, then live
    future.nothing_for(0.5)
    add_events(engine, job_id, 1)
    assert int(future.next(5).id) == 4
    future.close()

    for bad in ({"after": "x"}, {"Last-Event-ID": "-1"}):
        query = f"?after={bad['after']}" if "after" in bad else ""
        headers = {} if "after" in bad else bad
        response = httpx.get(url + query, headers=headers)
        assert response.status_code == 400
        assert response.headers["content-type"] == "application/json"
        assert response.json()["error"]["code"] == "invalid_cursor"

    missing = httpx.get(server.events_url(uuid.uuid4()))
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "job_not_found"


def test_finished_job_then_manual_retry_reopens_the_stream(api, api_server, engine):
    server = api_server(sse_poll_seconds=0.1)
    job_id = new_job(engine, events=2)
    finish_job(engine, job_id, JobStatus.FAILED)  # seq 3, provider_timeout (retryable)

    reader = Reader(server.events_url(job_id))
    first = reader.until_end()
    reader.close()
    assert seqs(first) == [1, 2, 3]
    assert json.loads(first[-1].data)["status"] == "failed"

    assert api.post(f"/v1/jobs/{job_id}/retry").status_code == 200
    again = Reader(server.events_url(job_id), last_event_id=3)
    reopened = again.events(2)
    assert [m.event for m in reopened] == ["job.status_changed", "job.manual_retry"]
    assert seqs(reopened) == [4, 5]
    again.nothing_for(0.5)  # queued again: the stream stays open
    again.close()


# --- Disconnects, limits, lifetime, keepalive -----------------------------------------------------


@pytest.mark.parametrize("how", ["clean_close", "connection_reset"])
def test_disconnect_ends_the_stream_task_and_releases_the_slot(api_server, engine, how):
    server = api_server(sse_max_streams=1, sse_poll_seconds=0.1)
    job_id = new_job(engine, events=3)

    if how == "clean_close":
        reader = Reader(server.events_url(job_id))
        reader.events(3)
        assert httpx.get(server.events_url(job_id)).status_code == 503  # the only slot is taken
        reader.close()
    else:
        sock = raw_get(server, f"/v1/jobs/{job_id}/events")
        read_until(sock, b"id: 3\n")
        reset(sock)

    closed = wait_until(lambda: server.stream_closed(job_id), 3, "stream task ended")
    assert closed[0]["reason"] == "disconnected"
    assert can_connect_soon(server, job_id) < 2


def test_stream_limit_returns_503_and_frees_up(api_server, engine):
    server = api_server(sse_max_streams=2)
    job_id = new_job(engine, events=1)
    open_streams = [Reader(server.events_url(job_id)) for _ in range(2)]
    assert [r.status for r in open_streams] == [200, 200]

    refused = httpx.get(server.events_url(job_id))
    assert refused.status_code == 503
    error = refused.json()["error"]
    assert error["code"] == "stream_limit_reached" and error["details"] == {"limit": 2}
    assert "Too many event streams" in error["message"]
    assert httpx.get(f"{server.url}/v1/jobs/{job_id}").status_code == 200  # the rest still works

    open_streams[0].close()
    can_connect_soon(server, job_id)
    open_streams[1].close()


def test_max_lifetime_closes_cleanly_with_the_last_id(api_server, engine):
    server = api_server(sse_max_stream_seconds=1, sse_keepalive_seconds=0.5, sse_poll_seconds=0.1)
    job_id = new_job(engine, events=2)
    started = time.monotonic()
    reader = Reader(server.events_url(job_id))
    messages = []
    while (message := reader.next(5, events_only=False)) is not None:
        messages.append(message)
    reader.close()

    assert 0.9 < time.monotonic() - started < 3
    assert seqs(messages) == [1, 2]
    last = messages[-1]
    assert last.id == "2" and last.data is None and last.event is None  # sets the id, no event
    assert "stream lifetime reached" in last.comments[0]
    assert server.stream_closed(job_id)[0]["reason"] == "max_lifetime"

    resumed = Reader(server.events_url(job_id), last_event_id=int(last.id))
    add_events(engine, job_id, 1)
    assert int(resumed.next(5).id) == 3
    resumed.close()


def test_keepalive_comments_when_idle(api_server, engine):
    server = api_server(sse_keepalive_seconds=0.3, sse_poll_seconds=0.05)
    job_id = new_job(engine)
    reader = Reader(server.events_url(job_id))
    times = []
    while len(times) < 5:
        arrived, message = reader.items.get(timeout=5)
        assert message is not None
        if message.comments == ["keepalive"]:
            times.append(arrived)
    reader.close()
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert all(0.2 < gap < 0.8 for gap in gaps), gaps


def test_more_streams_than_pool_connections_leave_the_api_responsive(api_server, engine):
    """The API's pool has 5 + 5 connections. 15 idle streams hold none of them between polls."""
    server = api_server(sse_max_streams=20, sse_poll_seconds=0.1)
    job_id = new_job(engine, events=1)
    readers = [Reader(server.events_url(job_id)) for _ in range(15)]
    for reader in readers:
        reader.events(1)

    for path in (f"/v1/jobs/{job_id}", f"/v1/jobs/{job_id}/events/history", "/health/ready"):
        started = time.monotonic()
        assert httpx.get(server.url + path, timeout=5).status_code == 200
        assert time.monotonic() - started < 2

    with engine.connect() as connection:
        stuck = connection.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state = 'idle in transaction' "
                # a poll is briefly idle-in-transaction between its two reads; a held one isn't
                "AND state_change < now() - interval '1 second'"
            )
        ).scalar_one()
    assert stuck == 0

    add_events(engine, job_id, 1)
    for reader in readers:
        assert int(reader.next(5).id) == 2  # every stream still delivers
        reader.close()


def test_a_client_that_stops_reading_is_dropped_without_blocking_others(api_server, engine):
    server = api_server(sse_max_streams=1, sse_write_timeout_seconds=1, sse_poll_seconds=0.1)
    job_id = new_job(engine)
    add_events(engine, job_id, 2000, payload={"stage": "summarize", "blob": "x" * 10_000})

    stuck = raw_get(server, f"/v1/jobs/{job_id}/events", rcvbuf=4096)  # never reads
    try:
        for _ in range(3):
            started = time.monotonic()
            assert httpx.get(f"{server.url}/v1/jobs/{job_id}", timeout=5).status_code == 200
            assert time.monotonic() - started < 1
        closed = wait_until(lambda: server.stream_closed(job_id), 15, "stuck stream dropped")
        assert closed[0]["reason"] == "disconnected"
        assert any(
            r.get("message") == "event stream client stopped reading; disconnecting it"
            for r in server.records()
        )
        can_connect_soon(server, job_id)
    finally:
        stuck.close()


def test_sse_client_devtool_exit_codes(api_server, engine, capsys):
    from devtools import sse_client

    server = api_server(sse_poll_seconds=0.1)
    job_id = new_job(engine, events=4)
    url = server.events_url(job_id)

    assert sse_client.main([url, "--all", "--stop-after", "2"]) == 2
    out = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in out[1:3]] == ["1", "2"]
    assert out[-1].startswith("disconnected after 2 events; last id 2")

    finish_job(engine, job_id)
    assert sse_client.main([url, "--last-event-id", "2", "--all"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in out[1:-1]] == ["3", "4", "5"]
    assert out[-1].startswith("stream.end: job_finished (status succeeded, last seq 5)")

    assert sse_client.main([url, "--after", "4"]) == 0  # progress hidden, 5 is a status change
    assert "running -> succeeded" in capsys.readouterr().out

    assert sse_client.main([server.events_url(uuid.uuid4())]) == 1
    assert "HTTP 404: job_not_found" in capsys.readouterr().err
