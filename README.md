# Tasklane

Tasklane is a planned local-first service for submitting and monitoring long-running AI
processing jobs (PDF ingestion and repository ZIP indexing), with a focus on durable job state,
retries, cancellation, and recovery. See [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for the goals and
[docs/architecture.md](docs/architecture.md) for the planned design.

## Current status: Phase 5 of 12

**What works today:**

- A Docker Compose stack with PostgreSQL 17, Redis 7.4 (with append-only persistence), a FastAPI
  service, an outbox dispatcher, and a Celery worker. All published ports are bound to
  `127.0.0.1`; the dispatcher and worker publish none.
- `GET /health/live` reports whether the API process is running.
- `GET /health/ready` checks PostgreSQL and Redis at the same time and answers within about
  2 seconds (`DEPENDENCY_TIMEOUT_SECONDS`), even when a dependency hangs. It returns `503` only
  when PostgreSQL is unreachable. When Redis is down it returns `200` and reports
  `"queue": "unavailable"`.
- The API reconnects to PostgreSQL and Redis by itself after they restart.
- JSON logs with an `X-Request-ID` on every request.
- Live reload in development.
- The database schema (uploads, jobs, attempts, events, artifacts, outbox), created by Alembic
  migrations that run automatically before the API starts.
- The job state machine and the database functions that apply status changes (with conditional
  updates, so concurrent changes can't both succeed) and append per-job numbered events. See
  [docs/reliability.md](docs/reliability.md).
- **Uploads** (`POST /v1/uploads`): PDF and ZIP files are streamed to disk in chunks, hashed
  (SHA-256) while streaming, size-limited on the bytes actually received (50 MiB PDF, 100 MiB ZIP),
  and checked for extension, declared type, and file signature. ZIP *contents* are not inspected
  yet (Phase 6).
- **Jobs** (`POST /v1/jobs`, `GET /v1/jobs`, `GET /v1/jobs/{id}`): job creation requires an
  `Idempotency-Key`; replays return the same job (`200`, `Idempotent-Replayed: true`) and reusing
  a key for a different request returns `409`. The job, its `job.created` event, and an outbox
  message are written in one transaction. Listing is newest first with cursor pagination.
- One error format on every route. Full reference: [docs/api.md](docs/api.md).
- **Jobs run.** The dispatcher publishes outbox rows to Redis (Celery); a worker claims the job,
  records an attempt with a lease, runs it, and marks it `succeeded`. Progress, stage events, and
  attempts are visible through `GET /v1/jobs/{id}`.
  - Duplicate or stale messages are detected by the claim and dropped without side effects, so
    a job is never run twice at the same time.
  - While Redis is down, new jobs stay `queued` and visible; the dispatcher backs off and
    publishes them when Redis returns.
  - **Transient failures are retried automatically**: provider timeouts and outages,
    database connection trouble and deadlocks, disk trouble, and stage timeouts. The job waits
    in `retry_wait` with a backoff (equal jitter: the first retry is 2.5–5 s away, the second
    5–10 s), up to 3 attempts in all, and then ends `failed` with "Gave up after 3 attempts."
    A retry resumes after the stages that already finished: their saved outputs are verified by
    size and SHA-256 first. Errors caused by the input, and errors nobody has classified, fail
    at once without retrying.
  - For development, a job can ask for injected failures (`fault_injection`) to watch retries
    happen. This needs `ENABLE_FAULT_INJECTION=true` on the server and is off by default.
  - A worker killed mid-job leaves that job `running` until lease recovery exists (Phase 7).
  See [docs/reliability.md](docs/reliability.md#delivery-outbox--dispatcher--redis--worker).
- **PDF ingestion** (`pdf_ingest`): validate, extract text, chunk, summarize, and write two
  artifacts, `summary.md` and `manifest.json`, downloadable with
  `GET /v1/jobs/{id}/artifacts/{artifact_id}`.
  - **The summarizer is a stub, not an AI model**: it picks the first sentences of each chunk
    and frequent key terms. It needs no API key and is deterministic. A real (optional) LLM
    provider is Phase 11.
  - The PDF is opened in a separate process with time, memory, CPU, and output-size limits, so
    a malicious or broken file can't hang or exhaust the worker.
  - Password-protected, damaged, image-only (scanned), too-long, and too-large PDFs fail with a
    clear message after one attempt. Scanned PDFs are not supported (no OCR).
  - Details: [docs/api.md](docs/api.md#what-a-pdf_ingest-job-does).
- `repo_index` jobs still run a placeholder stage (`dummy`, about 3 seconds of simulated
  progress, `DUMMY_STAGE_SECONDS`). ZIP contents are processed in Phase 6.

**Not implemented yet:** ZIP processing, lease recovery,
cancellation, manual retry, the event stream, the dashboard, metrics, and any real LLM provider.
See the phase list in [CLAUDE.md](CLAUDE.md).

## Requirements

- Docker Desktop (or Docker Engine with Compose v2)

## Run it

PowerShell on Windows (on macOS or Linux, use `cp` instead of `Copy-Item`):

```powershell
Copy-Item .env.example .env        # then edit .env if you like; it is gitignored
docker compose up --build -d
curl.exe http://127.0.0.1:8000/health/live
curl.exe http://127.0.0.1:8000/health/ready
```

Interactive API docs (generated by FastAPI): http://127.0.0.1:8000/docs

If a port is already in use, change `API_HOST_PORT`, `POSTGRES_HOST_PORT`, or `REDIS_HOST_PORT` in `.env`.

`docker compose up` first runs a one-shot `migrate` container (`alembic upgrade head`); the API
starts only after it succeeds. `docker compose ps -a` shows it as `Exited (0)` when it worked.

Stop the stack with `docker compose down`; the database, uploaded files, and queued messages are
kept in Docker volumes. `docker compose down -v` **deletes all three** (all jobs, all uploaded
files, and any messages waiting in Redis).

Watch jobs being published and run:

```powershell
docker compose logs -f worker dispatcher
```

To have time to watch a `repo_index` job (or to stop the worker mid-job), make the placeholder
stage longer, e.g. `DUMMY_STAGE_SECONDS=30` in `.env`, then `docker compose up -d worker`.

### Sample PDFs

The repository contains no PDF files; a script generates small samples (a text report, a scanned
look-alike with no text, a password-protected one, one with only an owner password, and a damaged
one) into a folder you choose:

```powershell
$demo = "$env:USERPROFILE\tasklane-demo"
New-Item -ItemType Directory -Force $demo | Out-Null
docker compose run --rm -v "${demo}:/out" api python -m devtools.pdf_samples /out
```

## Try the API (PowerShell)

These use `curl.exe` (not the PowerShell `curl` alias). Run them from a folder containing a PDF
named `report.pdf`. Every route, with all options and error codes, is documented in
[docs/api.md](docs/api.md).

```powershell
$api = "http://127.0.0.1:8000"

# Upload: raw file body, --data-binary, and an explicit Content-Type (curl's default is rejected)
$upload = curl.exe -s -X POST "$api/v1/uploads?filename=report.pdf" -H "Content-Type: application/pdf" --data-binary "@report.pdf" | ConvertFrom-Json
$upload

# Create a job. JSON goes through a file: PowerShell 5.1 mangles quotes in inline JSON.
@{ workflow_type = "pdf_ingest"; upload_id = $upload.id } | ConvertTo-Json -Compress | Set-Content -Encoding ascii job.json
$key = "demo-" + [guid]::NewGuid()
curl.exe -i -X POST "$api/v1/jobs" -H "Content-Type: application/json" -H "Idempotency-Key: $key" --data-binary "@job.json"

# Same key + same body again: 200 with "Idempotent-Replayed: true", same job
curl.exe -i -X POST "$api/v1/jobs" -H "Content-Type: application/json" -H "Idempotency-Key: $key" --data-binary "@job.json"

# List jobs (newest first) and fetch one
curl.exe -s "$api/v1/jobs?limit=5"
$job = (curl.exe -s "$api/v1/jobs?limit=1" | ConvertFrom-Json).items[0]
curl.exe -s "$api/v1/jobs/$($job.id)"

# Once it has succeeded, download the summary
$detail = curl.exe -s "$api/v1/jobs/$($job.id)" | ConvertFrom-Json
$summary = $detail.artifacts | Where-Object artifact_type -eq "summary"
curl.exe -s -o summary.md "$api$($summary.download_url)"
```

Uploaded files, intermediate files (`work/`), and artifacts live in the `storage` Docker volume,
not in your project folder:

```powershell
docker compose exec api find /data/storage -type f
```

## Database migrations

Migrations live in `backend/migrations/versions/` (like knex or Prisma migrations).

```powershell
docker compose run --rm migrate                                   # apply new migrations
docker compose run --rm migrate alembic current                   # show the applied version
docker compose run --rm migrate alembic history                   # list migrations
docker compose run --rm migrate alembic check                     # "No new upgrade operations
                                                                  #  detected" = models match DB
docker compose run --rm api alembic revision --autogenerate --rev-id 0005 -m "describe change"
```

Autogenerate writes a *draft* into `backend/migrations/versions/`. Always review it by hand: it
does not detect CHECK constraints and is unreliable with partial indexes. Tests fail if the models
and migrations disagree.

## Development

The `api` container runs your local `backend/app` source code, and uvicorn restarts when a file
changes, so you don't need to rebuild after editing code. Rebuild only after changing dependencies
(`backend/pyproject.toml` / `backend/uv.lock`) or `backend/alembic.ini`:
`docker compose up --build -d`.

On Docker Desktop for Windows, file-change events from the Windows filesystem don't reach Linux
containers, so `.env` sets `WATCHFILES_FORCE_POLLING=true` and the reloader checks files on a timer.

The `worker` and `dispatcher` containers also run your local code and restart when a `.py` file
under `backend/app` changes (via `backend/devtools/reload.py`). A restart, like
`docker compose stop worker`, lets a running job finish for up to 25 seconds; a job still running
after that is interrupted and stays `running` until lease recovery exists (Phase 7).

Watch logs: `docker compose logs -f api worker dispatcher`

## Tests

Tests run inside Docker against the real PostgreSQL and Redis containers. Integration tests use a
separate database, `<POSTGRES_DB>_test` (for example `tasklane_test`), which they drop and recreate
on every run; they never touch your development database.

```powershell
docker compose run --rm api pytest                  # everything (about a minute)
docker compose run --rm api pytest -m "not e2e"     # skip the end-to-end tests
docker compose run --rm api pytest -m e2e           # only end-to-end: real worker + dispatcher
docker compose run --rm api pytest tests/unit       # unit tests only (no database needed)
docker compose run --rm api pytest -m integration   # integration tests only
docker compose run --rm api ruff check app tests migrations devtools   # lint
```

## Dependency pinning

- Python dependencies are declared in `backend/pyproject.toml` and locked to exact versions in
  `backend/uv.lock` (like `package.json` + `package-lock.json`). Docker installs with
  `uv sync --frozen`, which fails if the lock file is out of date.
- Docker images are pinned to specific tags in `compose.yaml` and `backend/Dockerfile`.
