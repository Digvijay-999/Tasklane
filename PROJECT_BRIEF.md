# Tasklane — Durable AI Workflow Control Plane

## Project brief for Claude Code

Help me build this project **incrementally**. Do not generate the entire application in one pass.

First inspect the repository and tell me what is already there. Then propose a short architecture and implementation sequence based on this brief. Implement only the milestone I ask for. Explain important design decisions in plain language, keep the code organized, and update the README as the project becomes usable. Do not invent performance results or claim features that have not been implemented.

## 1. Product summary

Tasklane is a local-first service for submitting and monitoring long-running AI processing jobs. It demonstrates how to make background workflows visible, retryable, cancellable, and recoverable after failures.

A user submits a PDF or repository ZIP. Tasklane processes it in stages, stores job state and progress durably, and shows the result in an operations dashboard.

The project’s main engineering focus is **reliable orchestration**. It is not intended to replace a code-search product or a full document-intelligence system.

### Intended users

- A developer running AI or data processing jobs locally.
- A reviewer who wants to inspect job state, stage progress, failures, retries, and outputs.

### Core workflows

1. **Repository ZIP indexing**
   - Validate and safely unpack a source archive.
   - Identify supported text files and skip oversized or binary files.
   - Extract, chunk, and summarize files.
   - Produce a manifest and JSONL chunk artifact.

2. **PDF ingestion**
   - Validate and extract PDF text.
   - Divide text into chunks.
   - Summarize the document.
   - Produce a manifest and summary artifact.

Both workflows must run without API credentials using a deterministic stub provider. An optional LLM provider adapter can produce real summaries.

## 2. Technology and operating assumptions

Use a compatible, locked set of versions when implementation begins:

- Python and FastAPI for the API.
- PostgreSQL as the authoritative store for jobs, attempts, events, and artifacts.
- Celery workers with Redis as the task broker.
- React, TypeScript, and Vite for the dashboard.
- Docker Compose for local startup.
- OpenTelemetry APIs/SDK for traces and metrics, with console output or an optional OTLP endpoint.

Keep the application single-user and local-first for the first release. Compose services should bind to localhost by default. Do not add login, cloud storage, Kubernetes, or a hosted SaaS deployment in the MVP.

Celery tasks may be delivered more than once. Design workflow stages to be safe to repeat, add timeouts to external operations, and retry only errors classified as transient. Celery documents these idempotency, acknowledgement, retry, and timeout concerns directly. [Celery task guidance](https://docs.celeryq.dev/en/stable/userguide/tasks.html)

## 3. System design

### Main components

- **Web dashboard:** submits jobs and displays history, status, events, outputs, and queue health.
- **FastAPI service:** validates requests, manages uploads, creates jobs, serves job state, and streams events.
- **PostgreSQL:** source of truth for all user-visible job state.
- **Outbox dispatcher:** publishes queued work to Celery from committed database records.
- **Redis/Celery:** transports work to workers.
- **Worker:** executes named workflow stages and writes progress and artifacts to PostgreSQL/local storage.
- **Provider adapter:** supplies either deterministic stub summaries or optional LLM summaries.
- **Local artifact storage:** stores uploaded files and workflow outputs in directories keyed by UUID.

Redis and Celery result state must not be the source of truth for the UI. If the queue is unavailable, the API should still show the job’s last durable database state.

### Submission and delivery reliability

When a job is created, write its job record, initial event, and outbox record in one database transaction. A dispatcher publishes outbox entries to Celery and marks them published. A crash after publishing but before marking the entry can cause duplicate delivery, so workers must verify the database job state and safely handle duplicate messages.

Use a database-backed lease or heartbeat for active attempts. A recovery process should find expired leases and either requeue safe work within its retry budget or record a clear terminal failure. Celery late acknowledgements may be used only with idempotent work; they do not remove the need for explicit recovery behavior.

### Job state machine

Use these states:

- `queued`
- `running`
- `retry_wait`
- `cancel_requested`
- `succeeded`
- `failed`
- `cancelled`

Terminal states are `succeeded`, `failed`, and `cancelled`.

Record legal state transitions and reject invalid ones. A worker checks for cancellation between stages. Cancellation should not depend only on forcibly terminating a Celery task.

### Persistence model

At minimum, create these tables:

- **jobs:** ID, workflow type, status, current stage, percent complete, input reference and hash, idempotency key, request hash, timestamps, retry limits, cancellation flag, and safe error summary.
- **job_attempts:** job ID, attempt number, worker identifier, start/end time, outcome, and error class.
- **job_events:** monotonically increasing per-job sequence number, event type, timestamp, and structured JSON payload.
- **artifacts:** job ID, artifact type, storage path, filename, size, MIME type, hash, and creation time.
- **outbox:** job ID, task payload, creation time, publish time, and dispatch attempts.
- **uploads:** upload ID, safe server-side path, original filename for display only, file hash, size, status, and expiry time.

Use migrations. Store only safe error messages in the user-facing event stream; keep sensitive tracebacks in logs.

## 4. API contract

Use versioned routes under `/v1`.

| Method and route | Purpose |
|---|---|
| `POST /v1/uploads` | Upload a PDF or ZIP and return an `upload_id` and content hash. |
| `POST /v1/jobs` | Create a job using `workflow_type`, `upload_id`, options, and an `Idempotency-Key` header. |
| `GET /v1/jobs` | Return paginated job history with filters for status and workflow. |
| `GET /v1/jobs/{job_id}` | Return current job state, progress, attempts, and artifact metadata. |
| `GET /v1/jobs/{job_id}/events` | Stream events using Server-Sent Events (SSE). Support reconnect using `Last-Event-ID`. |
| `POST /v1/jobs/{job_id}/cancel` | Request cancellation. Return the current state if already terminal. |
| `POST /v1/jobs/{job_id}/retry` | Manually retry an eligible failed job, creating a new attempt while preserving history. |
| `GET /health/live` | Basic liveness response. |
| `GET /health/ready` | Report readiness of required dependencies. |
| `GET /metrics` | Expose or report operational metrics for local monitoring. |

### Idempotency

- The same key and same normalized request returns the existing job rather than creating another.
- Reusing a key with a different request returns `409 Conflict`.
- Document the idempotency-key retention period.
- Reject malformed or missing keys.

### Event stream

Persist events before sending them to the stream. Each event has a sequence ID. On reconnect, replay events after `Last-Event-ID`, then continue streaming new ones. A client disconnect must not cancel the job. If the client falls behind, it should be able to fetch the job’s full event history from an API route or a paginated variant.

## 5. Workflow behavior

Each workflow should report stages with a name, start/end timestamps, and progress.

### Repository ZIP workflow

Suggested stages:

1. `validate_archive`
2. `extract_files`
3. `filter_files`
4. `chunk_sources`
5. `summarize`
6. `write_artifacts`

Do not execute repository code. Ignore binary files, oversized files, hidden/system paths, and configured directories such as `.git`, `node_modules`, and build output. Produce a manifest with included/skipped file counts and skip reasons.

### PDF workflow

Suggested stages:

1. `validate_pdf`
2. `extract_text`
3. `chunk_document`
4. `summarize`
5. `write_artifacts`

Handle scanned PDFs as unsupported in the MVP and report that clearly rather than returning an empty successful summary. OCR can be a later extension.

### Provider adapter

Define a small provider interface such as `summarize(text, options) -> result`.

- **Stub provider:** default; deterministic output; no network or API key required.
- **Optional provider:** OpenAI-compatible chat completions or another provider behind configuration.
- Record provider/model, latency, usage values when available, and whether the call succeeded.
- Never store API keys in the database, artifacts, or frontend.
- Do not hard-code model pricing. If cost is shown, calculate it only from configurable pricing metadata and label it an estimate.
- Treat document/repository content as untrusted data. The summarizer must not follow instructions found inside inputs or invoke tools based on them.

## 6. Frontend requirements

Build a practical operations console with:

1. **Overview**
   - Counts by current state.
   - Recent jobs.
   - Queue readiness and oldest queued-job age.
   - Clear empty, loading, error, and disconnected-stream states.

2. **New job**
   - Select PDF ingestion or repository ZIP indexing.
   - Upload the file and submit.
   - Show validation errors before submission when possible.
   - Display the idempotency outcome if a prior job is returned.

3. **Job history**
   - Search/filter by workflow and status.
   - Show creation time, current stage, progress, attempts, and duration.

4. **Job detail**
   - Live stage timeline.
   - Persisted event history.
   - Attempt and retry information.
   - Error explanation and recovery action where allowed.
   - Artifact download links.
   - Cancel or retry actions that reflect the server state.

Use the API as the state source. Reconnect to SSE after browser refresh and recover the current job from the API.

## 7. Security and data handling

- Validate file extensions, MIME type, file signatures where practical, and maximum size.
- Use generated storage paths; never construct paths from user filenames.
- Defend ZIP extraction against path traversal, symlinks, decompression bombs, and excessive file counts.
- Reject unsupported file types and corrupt inputs with actionable errors.
- Apply request and upload size limits.
- Bind local development ports to `127.0.0.1` by default.
- Keep the API and worker away from executing uploaded repository code.
- Provide a command or documented retention setting to delete uploads/artifacts.
- Do not log full PDF or source-file contents, API keys, or unredacted secrets.

## 8. Observability

Instrument the API, dispatcher, worker, workflow stages, and provider calls with trace context where possible. OpenTelemetry’s Python SDK supports application instrumentation for traces and metrics; exporters can be configured separately. [OpenTelemetry Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)

Track at least:

- Queue wait duration.
- Workflow runtime.
- Jobs created/completed/failed/cancelled by workflow and state.
- Retry count by safe error class.
- Oldest queued job age.
- Provider call count, latency, and usage when available.
- Worker heartbeat/availability.

Do not put a unique job ID as a metric label; it creates unbounded cardinality. Put job and attempt IDs in traces and structured logs instead.

## 9. Acceptance criteria

The MVP is complete when all of the following work locally:

1. The same idempotency key and same request returns one job; reusing it with a different request returns `409`.
2. A temporary provider failure retries with bounded backoff and eventually succeeds or ends in a clear terminal failure.
3. A permanent input/validation error does not retry indefinitely.
4. Restarting a worker leaves job state visible and lets expired work recover or fail clearly.
5. Cancelling a queued job prevents execution; cancelling a running job stops at a stage boundary and ends as `cancelled`.
6. Reconnecting to SSE with `Last-Event-ID` replays the missing persisted events in order.
7. Duplicate Celery delivery does not create duplicate artifacts or corrupt job state.
8. Invalid, oversized, corrupt, or path-traversal ZIP uploads are rejected safely.
9. A no-key local run completes both workflows with the stub provider.
10. A reviewer can find the attempt history, error, output artifacts, and relevant trace/log information for a failed job.

Include unit, integration, and end-to-end coverage for the state machine, idempotency, workflow retries, cancellation, worker recovery, and event replay.

## 10. Suggested repository structure

- `backend/app/api/`
- `backend/app/domain/`
- `backend/app/db/`
- `backend/app/queue/`
- `backend/app/workflows/`
- `backend/app/providers/`
- `backend/app/observability/`
- `backend/migrations/`
- `backend/tests/`
- `frontend/src/features/jobs/`
- `frontend/src/features/dashboard/`
- `frontend/src/lib/api/`
- `frontend/src/components/`
- `compose.yaml`
- `.env.example`
- `README.md`
- `docs/architecture.md`
- `docs/api.md`
- `docs/reliability.md`
- `docs/security.md`

Claude Code may adjust names to fit the repository, but preserve the boundaries between API, persistence, queueing, workflow logic, provider integration, and UI.

## 11. Suggested 4–6 week build sequence

1. **Foundation:** architecture notes, Compose services, migrations, health routes, first API response.
2. **Durable jobs:** job schema, idempotent submission, history/detail endpoints, outbox dispatcher.
3. **Worker workflow:** one workflow end to end with stage events, retries, and artifacts; then add the second.
4. **Recovery and cancellation:** leases/heartbeats, recovery process, manual retry, cancellation, SSE replay.
5. **Dashboard and observability:** overview, job detail, live event view, traces and metrics.
6. **Hardening and portfolio polish:** security limits, error paths, documentation, demo walkthrough, and measured performance.

## 12. Portfolio evidence

Document what you actually implemented and measured:

- A diagram showing database, outbox, broker, worker, and event flow.
- A short failure/recovery demo.
- Measured queue wait and workflow duration on your machine and sample inputs.
- A clear explanation of duplicate delivery, idempotency, retries, and cancellation.
- Screenshots of successful and failed jobs.

Do not claim production scale or “exactly once” execution. The project should demonstrate durable state and safe recovery from duplicate or interrupted work.