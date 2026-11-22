# Phase 10 notes: observability (traces, metrics, structured logs)

Each decision: what was chosen, the alternatives, why, and a plain-language explanation. The
reference for operators is [docs/observability.md](docs/observability.md).

Words used below:
- **Trace / span**: a trace is the story of one piece of work, drawn as a tree of timed steps
  (spans). Each span has the trace's id and its own id. Think of nested `console.time()` blocks
  that a viewer can draw as a timeline, across processes.
- **traceparent**: a short string (`00-<trace id>-<span id>-01`, W3C Trace Context) that names a
  span, so another process can continue the trace under it. Like passing a request id along,
  but with a parent pointer.
- **Exporter**: the part that sends finished spans somewhere (a collector, Jaeger). Without one,
  spans are created and dropped.
- **Cardinality**: the number of distinct label combinations of a metric. Each one is a separate
  time series stored forever, so labels must have few possible values.

## 1. Versions (checked against PyPI on 2026-11-17, locked in `backend/uv.lock`)

| Package | Version | Why it is needed |
|---|---|---|
| `opentelemetry-api` | 1.45.2 | The calls our code makes (`start_span`, meters). Small, no dependencies of note. |
| `opentelemetry-sdk` | 1.45.2 | The implementation behind the API: tracer provider, batch span processor, meter provider. Without it every span is a no-op and there are no trace ids. |
| `opentelemetry-exporter-otlp-proto-http` | 1.45.2 | Optional trace export over OTLP/HTTP (protobuf). Brings `protobuf`, `requests`, `googleapis-common-protos`. Imported only when an endpoint is configured. |
| `opentelemetry-exporter-prometheus` | 0.66b2 | Renders our OpenTelemetry instruments (event-stream metrics) in Prometheus text. Brings `prometheus-client` 0.26.0, which also renders the database-derived metrics. |

- **Decision: no instrumentation packages at all.** Alternatives: `opentelemetry-instrumentation-
  fastapi`, `-celery`, `-sqlalchemy`. Reasons, per package:
  - *FastAPI/ASGI instrumentation* records the full URL including the query string (uploads send
    the file name as `?filename=`), and adds a span for every ASGI message: on an event stream
    that is a span per event, which the brief forbids. Our server span (about 40 lines in
    `app/observability/http.py`) records only the method, the route template, the status code
    and the job id.
  - *Celery instrumentation* injects the trace context that is *current when publishing*. Ours
    must come from the outbox row (written minutes earlier, in another process), so the
    dispatcher sets the header itself (5 lines).
  - *SQLAlchemy instrumentation* is a process-wide singleton that patches `create_engine` (our
    module imports it before any patching could happen), creates root spans for background
    polls (the dispatcher polls every second, the SSE stream every 0.5 s, the heartbeat every
    15 s: thousands of one-span traces), and emits connection-pool metrics labelled with host
    names (a forbidden label). Our hooks (`app/observability/db.py`, ~60 lines) create database
    spans only inside a traced operation and never per poll.
- **Discovered: FastAPI 0.162 ships its own OpenTelemetry support**, active as soon as the
  OpenTelemetry API is installed and a provider is configured. It records `url.query` (with only
  a fixed list of parameter names redacted), continues incoming `traceparent` headers, and adds a
  second server span plus "dependencies/endpoint/serialization" spans. **Decision:** turned off in
  `create_app` (`telemetry={"tracing": False, "metrics": False, "logs": False, ...}`). The span
  scan test caught it: the attributes weren't on the allowlist.
- *Plain language:* we installed only the core libraries and wrote the few hooks ourselves,
  because the ready-made plugins record things we must never record (file names in URLs) or
  create far too many spans.

## 2. Traces

### One trace per job, parent-child (not links)

- **Decision:** the job's trace starts at `job.create` in the API and every later piece of work
  is a **child of the span that caused it**, stored in the database:
  `jobs.traceparent` = `job.create`; `outbox.traceparent` = the span that enqueued the message
  (`job.create`, the failed `job.attempt`, `recovery.*`, `job.manual_retry`); the dispatcher's
  `outbox.publish` continues the row's value and sends its own in the Celery header
  `traceparent`; `job.attempt` continues that. Recovery, manual retry and cancel continue
  `jobs.traceparent`. Manual retry and cancel also **link** to their HTTP request span.
- **Alternatives:** (a) *links only*, the OpenTelemetry messaging recommendation: each attempt
  is its own trace linking back to the creation span; (b) *flat*: every attempt a direct child
  of `job.create`.
- **Why:** the requirement is "one job's full story findable from its trace id". With links, a
  trace id finds only one piece; the rest needs link-following or a tag search. Parent = cause
  keeps the order of events visible as nesting (attempt 1 → its retry's publish → attempt 2).
  Costs, documented: a trace stays open as long as the job lives (a manual retry days later adds
  to it), the root span ends long before its children, and a tail-sampling collector would
  split such traces. None of these matter for a local tool with Jaeger.
- *Plain language:* every job gets one "case file". Whatever happens to the job later (retries,
  recovery, a manual retry) is filed in the same case, under the step that caused it.

### Where the trace context is stored: two nullable text columns (migration 0005)

- **Decision:** `jobs.traceparent` and `outbox.traceparent` (W3C string), plus a `trace_id`
  field in every job response, derived from `jobs.traceparent` by a pure domain function.
- **Alternatives:** store only `trace_id` (not enough to continue as a child: the span id is
  needed); put it inside the outbox JSON payload (the payload is the Celery message body, which
  must stay `{"job_id": ...}`); a separate table (no benefit).
- *Plain language:* the job row remembers where its story started, and each queued message
  remembers which step queued it.

### Incoming `traceparent` headers are ignored

- **Decision:** every HTTP request starts a new trace.
- **Alternative:** continue a client's trace context.
- **Why:** browsers don't send one; a client-supplied context could set the job's trace id or mark
  it unsampled (no spans recorded). Predictable beats clever here.

### Exceptions are never recorded on spans

- **Decision:** `span()` uses `record_exception=False, set_status_on_exception=False`; failures
  set status ERROR (no description) and `error_class`.
- **Why:** exception messages and stack traces can contain document text, paths, SQL values or
  secrets. Tracebacks stay in the (redacted) logs, as before.

### No span per poll

- **Decision:** the `sse.stream` span is started but *not made current*, and each poll runs with
  database spans suppressed (a context flag checked by the DB hook). The heartbeat's lease
  renewals are suppressed the same way. The dispatcher and recovery create spans only for rows
  they act on; an idle pass creates none (tested).
- **Why:** a stream polling every 0.5 s for an hour would add 7,200 database spans to one trace.
- *Plain language:* the trace records what happened to the job, not every time someone looked.

### Database spans: statement text with placeholders, never values

- **Decision:** `db.statement` is the SQL text as SQLAlchemy sends it (`... WHERE id = %(id_1)s`),
  truncated to 1,000 characters. Values travel separately as bound parameters.
- **Alternative:** no statement at all (less useful for finding slow queries).
- **Checked by:** a test that creates a job with a distinctive idempotency key and asserts no
  database span contains it, and the span scan, which rejects any UUID in a statement.

### Every span is scanned in tests

- **Decision:** the root `conftest.py` installs an in-memory exporter; after every test, every
  span is checked against an attribute **allowlist** (`tests/telemetry.py`) and a list of
  forbidden value fragments (paths, `Bearer `, `sk-`, `password`, the test database password,
  `Traceback`), and must have no events or status description. The e2e processes write their
  spans to `TELEMETRY_SPAN_FILE`, which the pipeline fixture scans the same way.
- *Plain language:* a new span attribute has to be added to a list on purpose; anything else
  fails the build.

### Span names

`<METHOD> <route>`, `job.create`, `job.manual_retry`, `job.cancel`, `sse.stream`,
`outbox.publish`, `job.attempt`, `stage <name>`, `provider.summarize`, `recovery.lease_expired`,
`recovery.lost_message`, `db <OPERATION>`. Attributes per span: docs/observability.md.

## 3. Metrics

### Which metrics come from where

- **Decision:** everything about jobs, attempts, stages, provider calls, the outbox and worker
  presence is **computed from PostgreSQL at scrape time** by a prometheus-client collector
  (`app/services/metrics.py`). Only event-stream metrics are **process-local OpenTelemetry
  instruments** in the API (`app/observability/metrics.py`).
- **Alternatives:** (a) OpenTelemetry counters and histograms recorded in the worker as things
  happen, pushed over OTLP; (b) prometheus-client "multiprocess mode" with a shared directory;
  (c) a /metrics endpoint in each worker process.
- **Why:** Celery's prefork children are forked, recycled and killed (out of memory, the hard
  time limit, a dev reload); in-memory counters die with them, and the API can't read another
  process's memory. The database already records every fact these metrics count (attempt rows
  with start/end/outcome, events with error classes, provider-call rows with latency and
  tokens), so reading it is exact, survives restarts, needs no exporter, and gives one scrape
  endpoint. (a) needs a collector running to see anything and loses counts on kills; (b) needs a
  volume shared between containers and breaks when files of dead pids pile up; (c) can't be
  scraped (ports per child, children come and go).
- **Costs, documented:** a few aggregate queries per scrape (5 s statement timeout, one
  read-only repeatable-read transaction); they scan `job_attempts`, `job_events` and
  `provider_calls`, which grow until Phase 12 adds retention. Counters are totals over existing
  rows: once retention deletes rows, a total can drop (Prometheus treats it as a reset).
- **Event streams stay process-local** because a stream leaves no row in the database, and it
  lives in exactly one API process, so the API's own counter is the truth.
- *Plain language:* instead of each worker keeping a tally that is lost when it crashes, the
  metrics page counts what the database already wrote down.

### Queue wait needs one new column

- **Decision:** `job_attempts.queued_at` = when the job became claimable before this attempt:
  `retry_at` for a `retry_wait` job, else `jobs.updated_at` (when it entered `queued`; nothing
  else updates a queued row). Queue wait = `started_at - queued_at`. Also on the `job.attempt`
  span (`queue_wait_seconds`) and the "job claimed" log line.
- **Alternative:** reconstruct it from events at scrape time (a self-join over all events).

### Workflow runtime = attempt run time

- **Decision:** `tasklane_workflow_runtime_seconds{workflow_type, outcome}` per finished attempt
  (claim to end). Stage durations come from the `duration_ms` the `stage.completed` event already
  records.
- **Alternative:** job creation to finish (mixes in queue wait and backoff, and a manual retry
  resets `finished_at`). Queue wait has its own histogram.

### Buckets

Queue wait 0.1 s–1 h, runtime 0.5 s–1 h (the Celery hard limit), stages 50 ms–15 min (the
summarize limit), provider calls 10 ms–30 s (the provider timeout is 20 s).

### Labels: bounded enums only, enforced by a test

`workflow_type`, `status`, `stage`, `error_class`, `provider`, `model`, `outcome` (+ `le`). The
Prometheus exporter adds constant `otel_scope_*` labels and `target_info`; we drop the labels
and disable `target_info`, and render only `tasklane_*` families (not the client library's
process/GC metrics, whose labels aren't on the list). The test also rejects UUID-like values.

### Content type `text/plain; version=0.0.4`

prometheus-client 0.26 advertises text format 1.0.0, which only newer Prometheus servers
negotiate. The text is the same; we send the classic content type every version understands.

### Worker availability: a `worker_heartbeats` table (migration 0005)

- **Decision:** each Celery child process starts a presence thread at `worker_process_init` that
  upserts its row (`worker_id`, `last_seen_at = now()`, counters) every 5 s; a clean shutdown
  sets `stopped_at`. Alive = not stopped and seen within 3 intervals (15 s). Exposed as
  `tasklane_workers_alive`, in `GET /v1/overview` (`workers.alive`, `workers.last_seen_seconds`)
  and as "No worker is running" on the dashboard.
- **Alternatives:** Celery `inspect ping` over Redis (needs the broker, slow when workers are
  busy, not durable, and the brief forbids it); the attempts' lease heartbeat (only exists while
  a job runs, so an idle healthy worker would look dead).
- **Why children and not the Celery main process:** children run the jobs; a main process whose
  children can't start would otherwise look healthy.
- **The row also carries worker-only counters** (fenced attempts, failed heartbeat beats, dead
  heartbeat threads): kept in memory, saved with each report, summed by the scrape. A kill -9
  loses at most one interval of increments. Rows are kept after a process stops (counter totals
  stay monotonic); pruning is a Phase 12 retention item.
- *Plain language:* every worker process writes "still here" in a table every 5 seconds; the
  dashboard warns when nobody has written for 15 seconds.

## 4. Logs

### trace_id, span_id and bound job fields on every line

- **Decision:** a logging filter adds the active span's ids; `log_context(job_id=...,
  attempt_no=..., worker_id=...)` (a ContextVar, like Node's AsyncLocalStorage) binds fields for
  a block. The worker binds them for the whole attempt, the dispatcher per published message,
  recovery per action. The heartbeat thread runs in a copy of the attempt's context, so its
  lines carry the trace id too.
- **Alternative:** pass `extra=` everywhere (stage code deep in the workflows doesn't know the
  attempt number).

### A redaction layer on every line

- **Decision:** `app/observability/redaction.py` (stdlib only), applied by the JSON formatter to
  the message, every extra field (recursively) and the traceback. Patterns: private key blocks,
  URL credentials, `Bearer`/`Basic` values, known token formats (`sk-`, AWS, GitHub, GitLab,
  Slack, Google, JWT), sensitive `name=value` pairs; plus literal registration of the database
  password and of every environment variable whose *name* looks sensitive (values of 8+
  characters, so short common strings don't garble logs); plus masking whole values of extra
  fields whose *key* looks sensitive.
- **Alternatives:** an allowlist of loggable fields (would break third-party log lines such as
  Celery's and uvicorn's); redacting only our own messages (driver errors and tracebacks are
  where secrets actually leak).
- **Limits, documented in security.md:** pattern-based redaction can't recognise an arbitrary
  secret with no telltale format or name; it can over-mask (e.g. `total_tokens=12`). It is a
  safety net; code still never logs document content, file contents or archive names.
- *Plain language:* before a log line is printed, anything that looks like a password, key or
  token is replaced with `[REDACTED]`.

### The SDK's own warnings are rate-limited

A stopped collector makes the exporter warn on every batch (every second). A filter lets one line
per minute per message through.

## 5. Export and the local viewer

### Default: on, no exporter

- **Decision:** the tracer provider is always installed (trace ids in logs and the API), but an
  exporter only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. `OTEL_SDK_DISABLED=true` turns tracing
  off. Settings are read by our `Settings` class (validated: `http(s)://`, `{trace_id}` in
  `TRACE_VIEWER_URL`), not picked up implicitly by the SDK.
- **Telemetry never blocks:** the batch processor queues finished spans (2,048) and exports every
  second from its own thread; each export gives up after `OTEL_EXPORT_TIMEOUT_SECONDS` (2). At
  process exit the final flush is bounded by the same timeout (the worker's warm-shutdown grace
  is 25 s). Tested with an unreachable endpoint (`192.0.2.1`, where connections hang): span
  creation stays instant, jobs succeed in normal time, a real API process answers requests in
  well under a second and streams to `stream.end`, and the processes stop promptly.

### Viewer: Jaeger + Prometheus in an optional profile

- **Decision:** `jaegertracing/jaeger:2.23.0` (traces) and `prom/prometheus:v3.16.0` (scrapes
  `/metrics`) under the Compose profile `observability`; only the UI ports are published, on
  127.0.0.1. Traces go out over OTLP/HTTP; metrics are pulled.
- **Alternatives:** `grafana/otel-lgtm` (one image with collector, Tempo, Prometheus, Loki and
  Grafana: about 10× the download, and its trace links are long Grafana "explore" URLs instead
  of `/trace/{id}`; scraping our `/metrics` would mean overriding its internal config); Jaeger
  alone (no metrics graphs); pushing metrics over OTLP (Jaeger doesn't accept metrics, and the
  database-derived metrics are pull-shaped anyway).
- **Why:** each image does one job with a small config we own, and Jaeger's URL
  `http://127.0.0.1:16686/trace/{trace_id}` makes a clean "Open trace" link.
- **Jaeger keeps traces in memory** (lost when it restarts): fine for local debugging.

### Dashboard: trace id, copy button, "Open trace"

`GET /v1/meta` returns `trace_viewer_url` (or null). The job detail shows the trace id with a copy
button; with a template, an "Open trace" link (`target="_blank" rel="noopener noreferrer"`,
http(s) only, checked in the browser too). Navigation to another origin is not restricted by the
page's CSP, so the build's CSP is unchanged.

## 6. Carry-forward items addressed in this phase

| Item (from CLAUDE.md) | How it was resolved |
|---|---|
| **Phase 10/11 — log redaction** before real providers and API keys | `redaction.py` on every log line, tracebacks included; env secrets registered at startup in the API, dispatcher and worker; tests for each category (`tests/unit/test_redaction.py`), including a real logger call with a fake API key. |
| **Phase 10 — recovery metrics**: expired leases, requeued lost messages, fenced workers, failed heartbeat beats, dead heartbeat threads | `tasklane_recoveries_total{outcome="lease_expired"|"lost_message"}` (from events), `tasklane_worker_fenced_attempts_total`, `tasklane_worker_heartbeat_failures_total`, `tasklane_worker_heartbeat_thread_deaths_total` (worker counters saved in `worker_heartbeats`). Tested with known rows. |
| **Phase 10 — stream metrics**: open streams, refused, closes by reason, seq gaps | `tasklane_sse_streams_open`, `_refused_total`, `_closed_total{outcome}`, `_seq_gaps_total` (+ `_events_sent_total`), OpenTelemetry instruments in the API; one `sse.stream` span per connection. |
| **Phase 10 — dashboard metrics**: show only numbers `/metrics` provides, no invented rates | The Overview still reads `GET /v1/overview` (the same database numbers `/metrics` is computed from) and adds worker availability; it shows counts and ages, no rates. Browser-side reconnects and polling fallbacks are documented as not visible to the server. |

## 7. Fixes after the code review

A read-only review of the finished phase found these; each is fixed and tested.

- **Redaction could freeze a process (high).** The `name=value` pattern had two unbounded runs
  around a word list: about cubic backtracking. Measured: a 5 KB request path like
  `/pass.pass.pass...` took 84 s to redact, holding Python's GIL (every thread of the API, every
  event stream, stalled). Request paths are logged and attacker-controlled (any web page can
  send a request to 127.0.0.1). **Fix:** possessive quantifiers and lookbehinds so a match can
  only start at the beginning of a run of name characters (linear time); the name is matched
  whole and checked for sensitive words in a replacement function; strings over 64 KiB are cut
  before redaction. Test: five hostile inputs, each under 0.5 s.
  *Plain language:* a cleverly shaped URL could make the "hide secrets" step think for minutes;
  now it reads each character once.
- **Counters that could go down.** (a) Worker counters: the presence row was keyed by
  `worker@<host>:<pid>`, and after a container restart a new process often gets the same host
  name and pid, so its zeros overwrote the old totals. Now the key adds a random suffix per
  process. (b) Dispatch failures were `attempts - published rows` over existing rows; cancelling
  a queued job deletes its unpublished row (and its failures). Now counted on published rows
  only (`dispatch_attempts - 1`), which are never deleted.
- **Lost last spans of a worker child.** Celery's children end with `os._exit()`, skipping the
  SDK's exit flush; the final `job.attempt` span could sit in the export queue. The worker now
  flushes at `worker_process_shutdown` (bounded by the export timeout).
- **Histograms counted a missing value as 0 s** (`GREATEST(NULL, 0)` is 0). Rows with a missing
  value or label are now skipped.
- **A reused retry row kept an older traceparent.** `enqueue_retry` now also updates it.
- **Redaction gaps:** camelCase keys (`accessToken`, `dbPassword`) are now sensitive, and a
  quoted value with spaces (`password="correct horse battery"`) is masked whole.
- **Checked, already fine:** a `task.retry()` while the database is down keeps the custom
  `traceparent` header (Celery rebuilds the message from the request, headers included); a unit
  test now pins that.

## 8. Things to know

- `GET /metrics` reads the database on every scrape. Prometheus scrapes every 15 s; keep other
  scrapers off it or raise the interval if the tables get large before Phase 12 retention.
- Jobs created before this phase have no `traceparent`: `trace_id` is null, their later attempts
  start their own traces.
- Old `worker_heartbeats` rows (one per worker process ever started, including each dev reload)
  are kept until Phase 12 retention.
- A trace in Jaeger can be large (a PDF job: ~120 spans, most of them database statements).
