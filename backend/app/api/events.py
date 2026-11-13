"""Job event routes (Phase 8): a Server-Sent Events stream with replay, and a paginated history.

SSE in one paragraph: the response stays open and the server writes text blocks separated by a
blank line. `id:` is the event's per-job seq, `event:` its type, `data:` one line of JSON. When the
connection drops, a browser's EventSource reconnects by itself and sends the last id it saw in
the `Last-Event-ID` header; we send everything after it. See docs/api.md for the format.

The stream is an async generator (not a thread per stream): while it waits it holds no thread,
no database connection and no transaction. Each poll borrows a worker thread and a pooled
connection for one short read (EventService.page), then gives both back before sleeping.
"""

import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from functools import partial
from typing import Annotated, Any

import anyio
from anyio import to_thread
from fastapi import APIRouter, Header, Query, Request
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from app.api.jobs import parse_job_id
from app.api.schemas import ErrorResponse, JobEventPageResponse, JobEventResponse
from app.services.errors import ErrorCode, ServiceError
from app.services.events import EventService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["events"])

STREAM_BATCH = 500  # events per database read while replaying
DEFAULT_HISTORY_LIMIT = 100
MAX_HISTORY_LIMIT = 500
STREAM_END = "stream.end"
# ASCII digits only (\d would accept other scripts' digits); 18 digits always fit a bigint.
_SEQ = re.compile(r"[0-9]{1,18}")
# The database CHECK on job_events.event_type; re-checked so an event name can never carry an
# extra SSE field.
_EVENT_NAME = re.compile(r"[a-z_]+\.[a-z_]+")
_DATABASE_UNAVAILABLE = (OperationalError, InterfaceError, PoolTimeoutError)


# --- Resume position ------------------------------------------------------------------------------


def parse_resume_position(last_event_id: str | None, after: str | None) -> int:
    """The seq to resume after: the Last-Event-ID header if present, else ?after=, else 0 (from
    the beginning). Empty counts as absent. Anything but a non-negative integer -> 400."""
    source, value = ("Last-Event-ID", last_event_id) if last_event_id else ("after", after)
    if not value:
        return 0
    if not _SEQ.fullmatch(value):
        raise ServiceError(
            ErrorCode.INVALID_CURSOR,
            f"{source} must be a non-negative integer: the seq of the last event received.",
            {"source": source},
        )
    return int(value)


# --- SSE text -------------------------------------------------------------------------------------


def event_item(event: Any) -> dict[str, Any]:
    """The public shape of a stored event (history items and SSE data are the same)."""
    return JobEventResponse.model_validate(event).model_dump(mode="json")


def _data_line(value: dict[str, Any]) -> str:
    # ensure_ascii escapes every control character and non-ASCII character (\n, \r,   ...),
    # so untrusted strings inside the payload can never end the data line or add SSE fields.
    data = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if "\n" in data or "\r" in data:  # impossible with ensure_ascii; never send it if it happens
        raise ValueError("SSE data must be a single line")
    return f"data: {data}\n"


def format_event(event: Any) -> str:
    if not _EVENT_NAME.fullmatch(event.event_type):
        raise ValueError(f"unexpected event type format for seq {event.seq}")
    return f"id: {event.seq}\nevent: {event.event_type}\n" + _data_line(event_item(event)) + "\n"


def format_stream_end(reason: str, status: str | None, last_seq: int) -> str:
    # No `id:`: stream.end is not a stored event, so the client's last id stays the last real seq.
    payload = {"reason": reason, "status": status, "last_seq": last_seq}
    return f"event: {STREAM_END}\n" + _data_line(payload) + "\n"


# --- The stream loop ------------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamTimings:
    poll_seconds: float
    keepalive_seconds: float
    max_stream_seconds: float
    retry_milliseconds: int


async def event_frames(
    service: EventService,
    job_id: uuid.UUID,
    cursor: int,
    timings: StreamTimings,
    *,
    sleep: Callable[[float], Any] = anyio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> AsyncIterator[str]:
    """Yield SSE text for every event with seq > cursor, in seq order, then keep polling.

    One loop does replay and live: read everything after the cursor, send it, advance the cursor,
    wait, repeat. Ends with stream.end once the job is finished and everything is sent, or with a
    bare `id:` (the client reconnects and resumes) at the maximum lifetime. A client disconnect
    cancels it at its current await.
    """
    log = {"job_id": str(job_id)}
    started = last_output = clock()
    sent, reason = 0, "disconnected"  # if cancelled: client gone, write timeout, or shutdown
    failures = 0  # consecutive failed reads (database unavailable)
    logger.info("event stream opened", extra={**log, "after": cursor})
    try:
        yield f"retry: {timings.retry_milliseconds}\n\n"
        while True:
            try:
                page = await to_thread.run_sync(
                    partial(service.page, job_id, after=cursor, limit=STREAM_BATCH)
                )
            except ServiceError as exc:
                if exc.code != ErrorCode.JOB_NOT_FOUND:
                    raise
                reason = "job_not_found"  # deleted while streaming
                yield format_stream_end(reason, None, cursor)
                return
            except _DATABASE_UNAVAILABLE as exc:
                # Keep the connection: keepalives continue and events resume when it's back.
                # Warn once per outage; the retry delay below grows so that many open streams
                # don't flood the logs or tie up the shared thread pool while it lasts.
                if not failures:
                    logger.warning(
                        "event stream: database unavailable, retrying",
                        extra={**log, "error_type": type(exc).__name__},
                    )
                failures += 1
                page = None
            else:
                if failures:
                    logger.info("event stream: database available again", extra=log)
                failures = 0

            if page is not None:
                for event in page.events:
                    if event.seq != cursor + 1:
                        logger.error(
                            "event seq gap", extra={**log, "expected": cursor + 1, "got": event.seq}
                        )
                    yield format_event(event)
                    cursor, sent = event.seq, sent + 1
                if page.events:
                    last_output = clock()
                if cursor < page.last_seq:
                    if page.events:
                        continue  # more to replay: read the next batch at once
                    # The job says these exist but none were returned: log, never skip silently.
                    logger.error(
                        "event seq gap",
                        extra={
                            **log,
                            "expected": cursor + 1,
                            "got": None,
                            "last_seq": page.last_seq,
                        },
                    )
                    cursor = page.last_seq
                if page.finished:
                    reason = "job_finished"
                    yield format_stream_end(reason, page.status.value, cursor)
                    return

            now = clock()
            if now - started >= timings.max_stream_seconds:
                reason = "max_lifetime"
                yield f": stream lifetime reached, reconnect to resume\nid: {cursor}\n\n"
                return
            if now - last_output >= timings.keepalive_seconds:
                yield ": keepalive\n\n"
                last_output = now
            # Backoff after failed reads: poll, 2 x poll, 4 x poll ... up to the keepalive interval.
            delay = timings.poll_seconds * 2 ** min(failures, 10)
            await sleep(min(delay, timings.keepalive_seconds))  # keepalive >= poll (Settings)
    finally:
        logger.info(
            "event stream closed",
            extra={**log, "reason": reason, "events_sent": sent, "last_seq": cursor},
        )


# --- Connection limit and response ----------------------------------------------------------------


class StreamSlots:
    """Counts open streams in this API process. Only touched from the event loop (async route
    and response), never from threads, so a plain counter is safe."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.in_use = 0

    def try_acquire(self) -> bool:
        if self.in_use >= self.limit:
            return False
        self.in_use += 1
        return True

    def release(self) -> None:
        self.in_use -= 1


class EventStreamResponse(StreamingResponse):
    """StreamingResponse plus: a per-write timeout (a client that stops reading is dropped
    instead of holding a stream slot forever) and a callback that runs however the stream ends.

    Disconnects: uvicorn reports ASGI spec 2.3, so Starlette listens for `http.disconnect` next to
    the body and cancels the body as soon as the client goes away.
    """

    media_type = "text/event-stream"

    def __init__(
        self,
        frames: AsyncIterator[str],
        *,
        on_close: Callable[[], None],
        write_timeout: float,
    ) -> None:
        super().__init__(
            frames,
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # tells nginx-style proxies not to buffer the stream
                "Connection": "keep-alive",
            },
        )
        self.on_close = on_close
        self.write_timeout = write_timeout

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def timed_send(message: Message) -> None:
            with anyio.fail_after(self.write_timeout):
                await send(message)

        try:
            await super().__call__(scope, receive, timed_send)
        except TimeoutError:
            logger.warning("event stream client stopped reading; disconnecting it")
        finally:
            try:
                # A failed write leaves the generator paused at a `yield`; close it now (runs
                # its cleanup) instead of whenever it is garbage-collected.
                with anyio.CancelScope(shield=True):
                    await self.body_iterator.aclose()  # type: ignore[attr-defined]
            finally:
                self.on_close()


# --- Routes ---------------------------------------------------------------------------------------


@router.get(
    "/jobs/{job_id}/events",
    response_class=EventStreamResponse,
    responses={
        200: {"description": "Server-Sent Events", "content": {"text/event-stream": {}}},
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def stream_events(
    job_id: str,
    request: Request,
    after: Annotated[
        str | None, Query(description="Resume after this seq (used if Last-Event-ID is absent).")
    ] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> EventStreamResponse:
    parsed_id = parse_job_id(job_id)
    cursor = parse_resume_position(last_event_id, after)
    service: EventService = request.app.state.event_service
    last_seq = await to_thread.run_sync(service.last_seq, parsed_id)  # 404 before streaming
    if cursor > last_seq:
        # An id from the future (e.g. a stale bookmark): nothing to replay, stream new events.
        logger.info(
            "resume position past the last event; streaming new events only",
            extra={"job_id": str(parsed_id), "requested": cursor, "last_seq": last_seq},
        )
        cursor = last_seq

    slots: StreamSlots = request.app.state.sse_slots
    if not slots.try_acquire():
        raise ServiceError(
            ErrorCode.STREAM_LIMIT_REACHED,
            "Too many event streams are open. Try again shortly, or poll the history route.",
            {"limit": slots.limit},
        )
    settings = request.app.state.settings
    timings = StreamTimings(
        poll_seconds=settings.sse_poll_seconds,
        keepalive_seconds=settings.sse_keepalive_seconds,
        max_stream_seconds=settings.sse_max_stream_seconds,
        retry_milliseconds=settings.sse_retry_milliseconds,
    )
    # Nothing between acquire and here can fail; the response releases the slot when it ends.
    return EventStreamResponse(
        event_frames(service, parsed_id, cursor, timings),
        on_close=slots.release,
        write_timeout=settings.sse_write_timeout_seconds,
    )


@router.get(
    "/jobs/{job_id}/events/history",
    response_model=JobEventPageResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def event_history(
    job_id: str,
    request: Request,
    after: Annotated[str | None, Query(description="Return events with seq > after.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_HISTORY_LIMIT)] = DEFAULT_HISTORY_LIMIT,
) -> JobEventPageResponse:
    parsed_id = parse_job_id(job_id)
    cursor = parse_resume_position(None, after)
    page = request.app.state.event_service.page(parsed_id, after=cursor, limit=limit)
    last = page.events[-1].seq if page.events else None
    return JobEventPageResponse.model_validate(
        {
            "items": [event_item(event) for event in page.events],
            "next_after": last if last is not None and last < page.last_seq else None,
            "last_seq": page.last_seq,
        }
    )
