# Phase 8 notes: SSE event stream with replay, and the event history route

Each decision: **what** was decided, the **alternatives**, **why**, and a **plain-language**
version. The short form of every decision is in `CLAUDE.md` ("Decisions made (Phase 8)"); the
guarantees end to end are in `docs/reliability.md` ("Event replay"); the client-facing format is
in `docs/api.md` ("Events").

Node comparison for the whole phase: SSE is what you'd write in Express with
`res.writeHead(200, {"Content-Type": "text/event-stream"})` and then `res.write("id: 5\nevent:
...\ndata: {...}\n\n")` in a loop, with `req.on("close", ...)` to stop. The browser side is the
built-in `EventSource`, which reconnects by itself and sends the last id it saw.

---

## 1. One loop for replay and live, reading the database

**Decision.** `app/api/events.py::event_frames` is a single loop: read every committed event with
`seq > cursor` (up to 500), send them in `seq` order, move the cursor to the last one sent, and if
nothing is left, sleep `SSE_POLL_SECONDS` (0.5) and repeat. Replay is simply the first
iterations. Each read takes the job's `last_event_seq` first, then the events up to it
(`app/services/events.py`, `app/db/repositories/events.py`), ordered by `seq` (never by time).

**Alternatives.**
- *Replay with one query, then switch to a live feed* (Redis pub/sub, Postgres LISTEN/NOTIFY).
  Rejected: the switch-over is where events get lost (an event committed after the replay query
  but before the subscription started), and pub/sub forgets anything published while nobody
  listens, so polling would still be needed.
- *LISTEN/NOTIFY only to wake the loop early.* Rejected for now: 0.5 s latency is fine for a
  progress view, and each listener would need a dedicated database connection, the very thing
  the pool-exhaustion test protects. It can be added later without changing the loop.
- *Order by `created_at`.* Rejected (see carry-forward 1 below).

**Why it is race-free.** The cursor only moves to the `seq` of an event that was actually sent.
Thanks to per-job numbering (Phase 2), event N+1 can't be created until N is committed, so a read
never sees N+1 without N, and anything committed later has a larger `seq` and is found by the
next read. The two-step read (last seq, then events up to it) means the stream never decides
"caught up" from a number whose events it hasn't seen yet. Tested deterministically
(`test_events_committed_right_after_a_read_are_sent_by_the_next_poll`): a wrapper around the
service commits a new event right after every read, exactly in the window between "read" and
"sleep", and every one of them arrives, in order.

**Plain language.** The stream keeps a bookmark: "I've sent everything up to number 12." Every
half second it asks the database "anything after 12?", sends what it gets, and moves the bookmark.
Because numbers are handed out one at a time per job, number 13 can't appear before 12 is saved,
so the bookmark never jumps over an event.

## 2. Async generator, not a thread per stream

**Decision.** The route is `async def` and the stream is an async generator running on uvicorn's
event loop. Each database read runs in a worker thread (`anyio.to_thread.run_sync`) inside a short
session that is closed (connection back to the pool) before the loop sleeps with `anyio.sleep`.

**Alternatives.**
- *A sync generator* (Starlette runs it in its thread pool, one `next()` at a time). Rejected:
  sleeping inside the generator holds a thread for the whole life of the stream. Starlette's
  thread pool (40 threads) is shared with every plain `def` route, so 40 open streams would freeze
  the rest of the API.
- *Async database driver* (SQLAlchemy asyncio + psycopg async). Rejected: the rest of the backend
  is synchronous; one query per poll in a thread costs a few milliseconds of a thread.
- *One long-lived connection or transaction per stream.* Rejected: 11 viewers would exhaust the
  API's pool (5 + 5 overflow), and a transaction left open while idle blocks vacuum and holds a
  snapshot.

**How a slow or stuck client is contained.** Writing to a client is an `await` on its own socket:
a slow reader only slows its own stream (back-pressure), never the event loop. A client that stops
reading altogether is dropped when one write can't finish within `SSE_WRITE_TIMEOUT_SECONDS`
(30 s; `EventStreamResponse` wraps `send` in `anyio.fail_after`), which frees its slot. Tested
with a client that never reads while 20 MB of events queue up: other requests stay under 1 s and
the slot frees up.

**Plain language.** Like Node's single event loop: a waiting stream costs almost nothing, because
it isn't occupying a thread or a database connection while it waits. It borrows both for a few
milliseconds every half second.

## 3. Resume position: `Last-Event-ID` header, else `?after=`

**Decision.** `parse_resume_position(header, query)`: the header wins when both are present
(that is what EventSource sends when it reconnects); an empty value counts as absent; missing
or `0` start from the beginning; anything but 1-18 ASCII digits is `400 invalid_cursor` (same
error shape and code as the job list's cursor) with `details.source` naming which input was bad;
a value greater than the job's last seq is clamped to it (no replay, then live, no error); an
unknown or malformed job id is `404 job_not_found`. All of this happens before the response
starts, so errors are ordinary JSON responses.

**Alternatives.**
- *`?after=` wins.* Rejected: EventSource reuses the original URL on every reconnect, so a stale
  `?after=` in the URL would replay events the browser already has; the header is always newer.
- *Reject a cursor past the end* (`400`). Rejected, as asked: a stale bookmark (e.g. from a
  different database after a reset) shouldn't break the page. *Keeping it unclamped* was also
  rejected: the client would silently miss new events numbered up to its bogus cursor.
- *Treat an empty header as malformed.* Rejected: some proxies forward empty headers; "absent"
  is the harmless reading.

**Plain language.** The client says "the last one I got was N" (header from a reconnecting
browser, or `?after=N` from a page that was reloaded and can't set headers). If N is nonsense
the server says so; if N is from the future, the server just starts from now.

## 4. SSE text format and injection safety

**Decision.** Each event is `id: <seq>\nevent: <event_type>\ndata: <json>\n\n`. The JSON is
`{"seq", "event_type", "attempt_no", "created_at", "payload"}`, the same object the history route
returns. It is produced with `json.dumps(..., ensure_ascii=True)`, so every control character and
every non-ASCII character in any string is escaped (`\n`, `\r`, ` `, `\u0085`, ...); the
formatter also refuses (raises) if a raw `\n` or `\r` ever appeared, and re-checks the event type
against the database's `^[a-z_]+\.[a-z_]+$` CHECK before writing it as the `event:` field. The
first block is `retry: <SSE_RETRY_MILLISECONDS>`.

**Alternatives.**
- *Pydantic's `model_dump_json`.* Rejected: it writes non-ASCII characters raw. Not unsafe for
  SSE framing (only CR and LF end lines), but plain ASCII is easier to reason about and to show
  in a Windows console.
- *Splitting multi-line values into several `data:` lines* (allowed by SSE). Rejected: a JSON
  string never needs it once newlines are escaped, and one line is simpler for every client.

**Tested** with names like `"evil\n\nid: 999\nevent: job.hacked\ndata: {}\n\n"`, lone `\r`,
`\r\n`, U+2028/U+2029/U+0085, NUL, `: comment`, `data: fake`, and HTML: the frame always has
exactly the `id`, `event` and `data` lines, is pure ASCII, parses back to exactly one event, and
the payload round-trips unchanged (`tests/unit/test_events_pure.py`, and through the route in
`test_hostile_names_in_a_stored_payload_cannot_inject_sse_fields`). Today no event payload
contains an upload's file name or an archive path; if one ever does, it stays a JSON string.

**Plain language.** SSE is a line-based text format, so a newline inside a value could start a
fake event. Encoding the payload as JSON with everything unusual escaped turns every newline into
the two characters `\` and `n`, so a file name can never break out of its line.

## 5. Ending a stream: `stream.end`, then close

**Decision.** When the job is in a terminal state (`succeeded`, `failed`, `cancelled`) and the
cursor has reached its last seq, the server sends `event: stream.end` with `data:
{"reason": "job_finished", "status", "last_seq"}` and **no `id:`** line, then closes the
connection. `docs/api.md` tells clients to close their EventSource on `stream.end`. A manual
retry reopens the job (`failed -> queued`), so a client that wants to follow the new run
reconnects with the last id and gets `job.status_changed` and `job.manual_retry` next (tested).
If the job is deleted while streaming, the stream ends with `reason: job_not_found`.

**Alternatives.**
- *Keep the stream open after the job finishes* (until the client leaves or the lifetime ends).
  Rejected: it holds a slot for nothing, and a manual retry is a deliberate user action whose
  caller can simply reconnect.
- *Close without a final event.* Rejected: EventSource treats any close as a dropped connection
  and reconnects forever (every `retry:` interval).
- *Answer a reconnect to a finished, fully-sent job with `204 No Content`* (which makes a
  browser give up). Considered as an extra safety net, rejected for now: curl and the devtool
  would see an empty response instead of `stream.end`, and a well-written client already stops
  on `stream.end`. A naive client that ignores it reconnects at most once per `retry:` interval
  (3 s) and immediately gets `stream.end` again: cheap and bounded.
- *Give `stream.end` an id.* Rejected: it isn't a stored event; without an id the client's
  last id remains the seq of a real event.

**Plain language.** When the job is done, the server says "that's everything" and hangs up. A
good client hears that and stops. If someone retries the job later, the client can tune in again
from where it left off.

## 6. Limits: concurrent streams, lifetime, keepalive, write timeout

**Decision.** Six settings, validated at startup like earlier phases:

| Setting | Default | Validation |
|---|---|---|
| `SSE_MAX_STREAMS` | 50 | 1-10,000 |
| `SSE_MAX_STREAM_SECONDS` | 3600 | 1-86,400, ≥ keepalive |
| `SSE_POLL_SECONDS` | 0.5 | 0.05-10 |
| `SSE_KEEPALIVE_SECONDS` | 15 | 0.1-300, ≥ poll |
| `SSE_RETRY_MILLISECONDS` | 3000 | 100-600,000 |
| `SSE_WRITE_TIMEOUT_SECONDS` | 30 | 0.1-600 |

- **Concurrent streams:** an in-memory counter per API process (`StreamSlots`), acquired after the
  job check and released in `EventStreamResponse.__call__`'s `finally`, so every way a stream ends
  (finished, lifetime, disconnect, write timeout, error, shutdown) releases it. Over the limit:
  `503 stream_limit_reached`, message "Too many event streams are open. Try again shortly, or poll
  the history route.", `details.limit`. Only touched from the event loop, so no lock is needed.
- **Lifetime:** after `SSE_MAX_STREAM_SECONDS` the server sends a comment and a bare
  `id: <cursor>` line, then closes. Per the SSE spec, an `id:` with no `data:` updates the
  client's last event id without firing an event, so even a client that received nothing on this
  connection reconnects from the right place. No `stream.end` (the client *should* reconnect).
- **Keepalive:** `: keepalive` after `SSE_KEEPALIVE_SECONDS` without any other output.
- **Write timeout:** see decision 2.

**Alternatives.** *Counting streams in Redis or Postgres* (a global limit across API processes):
rejected for now: there is one API process; documented as per-process. *Limiting per client IP*:
rejected; it's a local-first tool with no users or auth yet. *No lifetime limit*: rejected; long
connections pile up behind proxies and stale tabs, and periodic reconnects are free with replay.

**Plain language.** At most 50 viewers at once per server; each connection is recycled after an
hour (the browser reconnects and picks up where it was); a short "still here" comment every 15 s
stops proxies from cutting a quiet connection; and a viewer that stops reading is let go.

## 7. Disconnects stop the loop at once and never touch the job

**Decision.** Rely on Starlette + uvicorn: uvicorn reports ASGI spec 2.3, so Starlette's
`StreamingResponse` listens for `http.disconnect` next to the body and cancels the body task as
soon as the socket closes (clean FIN or an abrupt reset). The cancellation lands at the loop's
current `await` (the sleep, or the end of the current read: a read in a thread finishes its few
milliseconds first, and its session is closed by its `with` block). The loop logs
`event stream closed` with `reason` (`disconnected`, `job_finished`, `max_lifetime`,
`job_not_found`), `events_sent` and `last_seq`. The stream code is read-only: nothing in it writes
to the database.

**Alternatives.** *Polling `request.is_disconnected()` in the loop.* Rejected: redundant with
Starlette's listener, and it would only notice at the next poll. *Detecting the disconnect only
when a write fails.* Rejected: an idle stream writes only every 15 s.

**Tested** with a real server process: a clean close and a TCP reset (`SO_LINGER 0`) both end the
stream task (logged `reason: disconnected`) and free the only slot within 2 s; a real job keeps
running and succeeds, with no cancel events and one attempt, after its viewer vanishes. If a
future uvicorn moves to ASGI spec 2.4 (no disconnect listener), these tests would catch it.

**Also:** the API now runs with `uvicorn --timeout-graceful-shutdown 5` (Dockerfile, both
targets). Without it, uvicorn waits for every open response to finish before stopping, and an
open stream never finishes: `docker compose stop` and live reload would hang until the stream
lifetime. Clients reconnect and resume.

**Plain language.** When the viewer closes the tab, the server notices immediately, stops that
stream and frees its place. The job never knew anyone was watching.

## 8. Database connections: short reads, proven with more streams than the pool

**Decision.** Each poll is `with session_factory() as session:` around two `SELECT`s; the session
(and its implicit transaction) closes before the sleep. Tested with 15 idle streams against the
API's 10-connection pool: the job detail, history and readiness routes answer in under 2 s, no
session is `idle in transaction`, and all 15 streams still deliver the next event.

**Database outage during a stream:** the loop catches the same connectivity errors the API maps to
`503` (`OperationalError`, `InterfaceError`, pool timeout), logs one warning per outage with only
the error type, keeps sending keepalives, and retries with a growing delay (poll, 2 × poll,
4 × poll, ... capped at the keepalive interval), then logs `database available again`. Without
the backoff, 50 open streams would log about 100 warnings a second and keep the shared thread
pool busy with failing connection attempts. A new connection during the outage gets
`503 service_unavailable` from the job check (before streaming).

**Plain language.** The stream is like a person who checks their mailbox every half second but
doesn't stand at the counter between checks, so the queue at the post office (the connection
pool) keeps moving.

## 9. Gaps are logged, never skipped silently

**Decision.** If a read returns a seq other than `cursor + 1`, the loop logs `event seq gap`
(ERROR, `expected`, `got`) and still sends the event. If the job's `last_event_seq` says events
exist but the read returns none (missing rows at the end), it logs the gap with `got: null` and
moves the cursor to `last_event_seq`, so the loop can't spin. Tested by deleting rows.

**Alternatives.** *Stop the stream on a gap.* Rejected: the client would reconnect into the same
gap forever. *Wait for the missing event.* Rejected: with per-job numbering a committed later
event proves the earlier one will never appear; the only cause is a deleted row.

**Plain language.** Gaps can't happen today. If one ever does (a bug, or a future cleanup job), it
shows up loudly in the logs instead of quietly hiding a missing event.

## 10. Only committed events are streamed

**Decision.** Nothing new was needed: events are written in the same transaction as the change
they describe (since Phase 2), and the stream reads from its own connection, so uncommitted rows
are invisible to it. Asserted in two tests: in-process (an event appended in an open transaction
isn't sent during several polls, and arrives right after the commit) and over a real connection
(ten polls, nothing; commit; delivered).

## 11. The history route

**Decision.** `GET /v1/jobs/{job_id}/events/history?after=&limit=` returns
`{items, next_after, last_seq}`: items in `seq` order (same objects as the SSE data),
`limit` 1-500 (default 100; outside → `422 invalid_request`, as for the job list), `after` parsed
by the same function as the stream (malformed → `400 invalid_cursor`), `next_after` = last item's
seq if it is below `last_seq`, else `null`. Plain `def` route (thread pool), one short read.

**Alternatives.** *An opaque cursor like the job list.* Rejected: `seq` is already a public,
stable, per-job position, and a client switching between SSE and history should use the same
number.

**Plain language.** The same events as pages of JSON, for anything that can't hold a stream open:
"give me 100 events after number N".

## 12. Module boundaries

- `app/api/events.py`: HTTP only: parsing the resume position, SSE text, the async loop
  (it is HTTP framing plus timing), the response class, the slot counter, the routes. It imports
  no `app.db` (enforced by `test_boundaries.py`); it catches SQLAlchemy's connectivity exception
  classes, as `api/errors.py` already does.
- `app/services/events.py`: `EventService.page()` / `last_seq()`, one short session each, raises
  `ServiceError(JOB_NOT_FOUND)`.
- `app/db/repositories/events.py`: the two `SELECT`s.
- New error code `stream_limit_reached` (503) in `services/errors.py` / `api/errors.py` (the test
  that every code has a status still passes).

## 13. Developer tool: `devtools/sse_client.py`

**Decision.** Standard library only (`http.client`, which decodes chunked responses), so it runs
in the api container without extra dependencies. One line per event in the same format as
`devtools.job_events` (reusing its `describe`), `stage.progress` hidden unless `--all`;
`--after`, `--last-event-id` (header), `--stop-after N` (closes the socket after N printed
events), `--raw`. Exit codes: `0` stream.end, `2` stopped by `--stop-after`, `3` the server closed
without stream.end (prints the id to resume with), `1` HTTP error (prints the error code). Its SSE
parser (`iter_sse`, spec field rules: optional space after the colon, `:` comments, multi-line
data, ids with NUL ignored) is reused by the tests. Tested against a real server.

## 14. Tests

- `tests/unit/test_events_pure.py`: resume-position rules, frame format, hostile names, event
  type re-check, `stream.end`, the parser, `StreamSlots`, settings validation and defaults.
- `tests/integration/test_events_api.py` (in-process, real Postgres): history pagination,
  `next_after`, limits, malformed `after`, 404s; finished-job streams through TestClient (headers,
  every resume edge case, injection through the route, Phase 7 event types); the loop driven
  directly: the read/sleep race, uncommitted events, gaps, database outage, job deleted mid-stream,
  read-only.
- `tests/integration/test_events_e2e.py` (marked `e2e`; a real `uvicorn` process on a free port,
  real sockets): acceptance #6 (disconnect, reconnect with `Last-Event-ID` and with `?after=`,
  with the missed events stored before or produced while away: exactly the missing events, in
  order, through `stream.end`); a real PDF job through the real dispatcher and worker, streamed
  from start to `stream.end` and compared field by field with the stored events; disconnect
  doesn't affect a running job; live latency (< poll + 0.5 s); nothing uncommitted; resume edge
  cases; manual retry after `stream.end`; clean close and TCP reset release the slot; 503 at the
  limit; lifetime close with the last id; keepalive spacing; 15 streams vs a 10-connection pool; a
  client that stops reading; the devtool's exit codes.

TestClient is used only for streams that end by themselves (it buffers the whole response).

## 15. Review follow-ups

An independent read-only review found no correctness problem in the loop, slot release or
connection handling. Changed after it:
- **Backoff during a database outage** (decision 8): one warning per outage and a growing retry
  delay, instead of a warning and a retry every 0.5 s per stream.
- **Slot release when the generator raises:** now covered by a unit test (`EventStreamResponse`
  with a generator that raises, and with a client that disconnects).
- **Test robustness:** the "no session idle in transaction" check ignores the instant between
  a poll's two reads (`state_change` older than 1 s); raw-socket reads have a timeout and fail
  on an early close instead of hanging the run; the server fixture closes its log handle.

Kept as is: the `Connection: keep-alive` header (requested; harmless on HTTP/1.1), checking the
job before the slot (a refused request costs one primary-key read), and raising on an event type
that fails the format check (the database CHECK makes it impossible).

---

## Carry-forward items from CLAUDE.md

1. **"Phase 8 — event order: order and replay events by `seq` only, never `created_at`."**
   Resolved. Both reads order by `seq` (`list_events ... ORDER BY seq`), the cursor is a `seq`,
   `created_at` is documented as informational ("transaction start time, order by seq") in the
   API schema description, `docs/api.md` and `docs/reliability.md`. The real-job test compares
   the stream with the stored events ordered by `seq`. Removed from CLAUDE.md.
2. **"Phase 8 — new event types: `attempt.lease_expired`, `job.requeued`, `job.manual_retry`, and
   `job.status_changed` payloads with `reason`, `before_stage`, `outbox_removed`, `requested_by`
   must stream and replay like the others."** Resolved. The stream passes every stored payload
   through unchanged and has no per-type handling; the devtool describes them (via
   `job_events.describe`). `test_phase7_event_types_stream_and_replay_like_the_others` streams,
   replays (`?after=2`) and pages all four with those payload fields. All types are listed with
   their payload fields in `docs/api.md`. Removed from CLAUDE.md.

Phase 9 items added to CLAUDE.md: the dashboard must not auto-link bare URLs in file names (or
anywhere it shows event payloads or manifest data), must set up CORS or a Vite dev proxy for the
dev origin, and must follow the EventSource notes (named-event listeners, `?after=` after a
reload, close on `stream.end`, reconnect itself after an error status).

## Things to know

- **Rebuild once:** `docker compose up --build -d`, because the Dockerfile's uvicorn command gained
  `--timeout-graceful-shutdown 5`.
- **Latency is polling-based:** up to `SSE_POLL_SECONDS` (0.5 s) plus a read. Each open stream
  costs one tiny query every 0.5 s (50 streams ≈ 100 queries/s, each by primary key).
- **The stream limit is per API process** and in memory.
- **The stuck-client defence needs a full send buffer.** A client that stops reading is dropped
  only once the kernel's buffers fill and a write blocks for 30 s; until then it just holds a
  slot (bounded by the lifetime limit).
- **A browser gives up after an error status** on reconnect (e.g. `503` while at the limit, or
  while the database is down); the Phase 9 page must reconnect itself with `?after=`.
