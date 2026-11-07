# Tasklane — working rules for Claude Code

Tasklane is a local-first control plane for long-running AI processing jobs (PDF ingestion and
repository ZIP indexing). Its focus is **reliable orchestration**: durable state in PostgreSQL,
an outbox dispatcher, Celery workers, retries, leases/recovery, cancellation, and SSE event replay.
The full product brief is in `PROJECT_BRIEF.md`; the planned design is in `docs/architecture.md`.

## About the developer

- Beginner with Python backends, PostgreSQL, Celery, and Docker.
- Comfortable with Node.js, Express, MongoDB, and REST APIs — comparisons to those are helpful
  (e.g. FastAPI ≈ Express, Pydantic ≈ zod, Alembic ≈ knex/Prisma migrations, Celery ≈ BullMQ,
  uv + `uv.lock` ≈ npm + `package-lock.json`, pytest ≈ Jest).
- Develops on **Windows** (PowerShell, Docker Desktop). Give commands that work there.

## Working rules

1. **Implement only the phase I ask for.** Never start the next phase on your own.
2. **Plan first.** Before writing code for a phase, show a short plan and wait for my approval.
3. **Explain important design decisions** in plain language suitable for a beginner.
4. **Preserve module boundaries** between `api`, `services`, `domain`, `db`, `storage`, `queue`,
   `workflows`, `providers`, `observability`, and `frontend`:
   - `domain` holds pure logic (state machine, error classes, rules) and imports only the
     standard library and other `domain` modules (a test enforces this).
   - `api` handles HTTP only (parse input, call a service, shape the response, map error codes to
     status codes); it never talks to Celery directly.
   - `services` coordinates `db`, `storage`, and `domain` for each use case and owns transactions.
     It knows nothing about HTTP: it raises `ServiceError` with a stable code.
   - `db` owns SQLAlchemy models, sessions, and repositories (SQL only; never commits).
   - `storage` owns files on disk: generated paths, temporary writes, atomic renames, deletes.
     No database or HTTP code.
   - `queue` owns Celery app setup, the outbox dispatcher, and task entry points.
   - `workflows` owns stage logic; it receives a provider and storage, not HTTP or Celery objects.
   - `providers` owns the summarizer interface and its implementations (stub, optional LLM).
   - `observability` owns logging, tracing, and metrics setup.
   - `frontend` talks to the backend only through the HTTP API.
5. **Every phase includes tests** for its logic and states the exact commands to run them.
6. **At the end of each phase**, list the files created or changed, how to run the project, and how
   to manually verify the new behavior.
7. **Keep `README.md` accurate** to what actually works. Never claim unimplemented features,
   invented performance numbers, production scale, or "exactly once" execution.
8. **Never commit secrets.** Use `.env` (gitignored) and `.env.example` (committed, placeholders only).
9. **Bind all local ports to `127.0.0.1`** (e.g. `"127.0.0.1:8000:8000"` in Compose).
10. Ask before installing dependencies, pushing, deleting work, or resetting Git state. Never
    commit — the developer commits manually.

## Decisions made (Phase 0)

- **Backend versions approved** (see `docs/architecture.md` §7). In Phase 1, verify the latest
  stable compatible patch versions when generating `uv.lock` — do not rely on memory.
- **Frontend versions are not locked yet.** React, Vite, and TypeScript are decided at the start of Phase 9.
- **Tests run in Docker by default** (`docker compose run --rm api pytest`) against real PostgreSQL.
- **Recovery loop runs inside the dispatcher process**, but the dispatcher loop and the recovery
  loop are separate modules with separate tests, so they can become separate services later.
- **`cancel_requested -> succeeded` is legal.** If the last stage finishes before the worker sees
  the cancel request, the job ends `succeeded` and an event records that cancellation was
  requested but the job completed first.
- **Default branch is `main`.** The developer commits manually — do not commit.
- **Local storage** for uploads and artifacts lives under `storage/` (gitignored).

## Decisions made (Phase 1)

- **`/health/ready` requires only PostgreSQL.** Redis is reported but never causes a `503`.
- **Dependency checks run concurrently with a hard deadline** (`DEPENDENCY_TIMEOUT_SECONDS`,
  default 2). Driver timeouts alone don't cover DNS, which is slow for stopped Docker containers.
- **`pool_pre_ping=True`** on the SQLAlchemy engine replaces dead connections after a Postgres restart.
- ~~Redis Python client stays on 5.2.x~~ (superseded in Phase 4: Celery 5.6 / kombu 5.6 allow
  Redis client 6.4).
- **Dev live reload** bind-mounts `backend/app` and `backend/tests` into the container and runs
  `uvicorn --reload` with `WATCHFILES_FORCE_POLLING=true`. Without polling, edits made on
  Windows were not detected.
- **Request logging** uses plain ASGI middleware (not `BaseHTTPMiddleware`), so it works with SSE.

## Decisions made (Phase 2)

- **State machine** lives in `app/domain/state_machine.py` (pure; a test forbids framework/db
  imports). 12 legal transitions; see `docs/reliability.md`.
- **A definite outcome beats a pending cancel:** `cancel_requested -> succeeded` and
  `cancel_requested -> failed` are legal and emit a `job.cancel_superseded` event. A transient
  error during `cancel_requested` ends `cancelled`.
- **Queued and retry_wait jobs cancel directly** to `cancelled`.
- **Expired lease:** `retry_wait` if attempts remain, else `failed` (`lease_expired`);
  `cancelled` if cancellation was requested.
- **Manual retry** reopens the same `failed` job (`failed -> queued`), grants a fresh budget of 3
  attempts (`max_attempts = attempt_count + 3`), is not offered for input errors, and
  `can_manual_retry` refuses before `max_attempts` would exceed 20 (the DB cap).
- **Idempotency keys live as long as the job row**; document this in Phase 3.
- **Status changes only via `transition_job`** (conditional `UPDATE ... WHERE status = :from`);
  `applied=False` means the job wasn't in `from` (treat the returned status as advice, not
  final). Relies on READ COMMITTED. Repository functions flush pending ORM changes first, expire
  cached `Job` objects after, and never commit. Set `Job.id` explicitly when creating a job.
- **A manual retry starts a clean run:** it requires `max_attempts`, and clears finish time,
  error, stage, progress and `cancel_requested_at` (`ck_jobs_no_cancel_while_active` enforces the
  last one). Success clears errors from earlier attempts.
- **Event numbers are per job** (`jobs.last_event_seq`, incremented under the row lock), never a
  table-wide sequence.
- **Value lists are `text` + CHECK**, not native Postgres enums. Migrations are frozen snapshots;
  tests compare them with the Python enums, and strictly with the models (a `create_all` database
  vs the migrated one: constraint, index, and column definitions must be identical).
- **Every constraint needs a test** in `tests/integration/test_constraints.py` (a meta-test
  enforces it). Watch NULL semantics in CHECKs: NULL counts as a pass.
- **Migration names use `op.f(...)`**, because Alembic applies the naming convention to migration
  operations too (without it, `ck_jobs_status` becomes `ck_jobs_ck_jobs_status`).
- **Migrations run in the one-shot `migrate` Compose service**; never at app startup.
- **Integration tests use `<POSTGRES_DB>_test`** (recreated per run, guarded against the dev DB)
  and truncate all tables before each test.

## Decisions made (Phase 3)

- **Uploads are the raw request body** plus `?filename=` (display only, never in a path), not
  multipart, so size and signature are checked while streaming. Required `Content-Type` per kind.
- **Upload flow:** commit a `receiving` row first, stream to `uploads/.incoming/{id}.part`
  (hashing and counting bytes), fsync, `os.replace` into `uploads/{id}/source.{pdf|zip}`, mark
  `ready`. Any failure deletes the files and marks `rejected` with a `rejection_reason`.
  Interrupted-upload cleanup is deferred to Phase 12.
- **Limits:** PDF 50 MiB, ZIP 100 MiB (enforced on bytes received; Content-Length only fails
  fast), upload TTL 24 h, JSON bodies 64 KiB, page size 20 (max 100).
- **Only the signature is checked for ZIPs now**; contents are Phase 6, PDF structure Phase 5.
- **Idempotency-Key** `^[A-Za-z0-9._:-]{16,128}$`, global scope, lives as long as the job row.
  The request hash covers only fields the client sent (no defaults); stored options include
  defaults. Explicit-default vs omitted fields are different requests (documented).
- **Job creation:** key lookup (replay shortcut, skips upload validation) → upload `FOR SHARE` →
  `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING` → event + outbox, one transaction.
  201 + `Location` / 200 + `Idempotent-Replayed: true` / 409. One upload may feed many jobs.
- **Listing** uses keyset pagination on `(created_at, id)` DESC with opaque cursors bound to the
  filters; indexes in migration 0002.
- **Errors:** one shape `{"error": {code, message, details, request_id}}`; services raise
  `ServiceError`, `api/errors.py` maps codes to status. Unexpected errors → 500/503 generic.
- **Boundaries are tested** (`tests/unit/test_boundaries.py`): `api` never imports `db` or
  `storage` (health checks go through `services/health.py`); `services` never imports
  FastAPI/Starlette; `storage` imports no database or HTTP code. Shared pure values such as
  `JobListPosition` live in `domain`.
- **Storage** is the named Docker volume `storage` at `/data/storage`;
  `docker compose down -v` deletes it.
- **Windows docs:** examples use `curl.exe`, `--data-binary "@file"`, and JSON written to a file
  (PowerShell 5.1 strips quotes from inline JSON).

## Decisions made (Phase 4)

- **Celery 5.6.3, kombu `>=5.6.2,<5.7` (5.6.2), Redis client 6.4.0.** Celery 5.6 sets no upper
  bound on kombu; we stay on the kombu line it shipped with. Phase 1 health tests stayed green.
- **Dispatcher** (`app/queue/dispatcher.py`, `python -m app.queue.dispatcher`): one pass = one
  transaction: `SELECT ... WHERE published_at IS NULL AND available_at <= now() ORDER BY
  available_at, id LIMIT 50 FOR UPDATE SKIP LOCKED`, publish while holding the locks, mark
  published, commit. Polls every 1 s (immediately again after a full batch). **No
  LISTEN/NOTIFY** (polling is needed anyway for delayed retries).
- **Redis down:** the failed row and the rest of the batch get `dispatch_attempts + 1`, the error
  *type*, and `available_at = clock_timestamp() + backoff` (1 s doubling to 60 s, never gives up);
  the loop backs off too. Only the dispatcher's Celery app skips kombu's connection retries
  (`for_publishing=True`); a failed publish can still take a few seconds (DNS for a stopped
  container). Workers reconnect forever (`broker_connection_max_retries=None`) and don't cancel
  running tasks on a Redis disconnect.
- **Celery settings:** JSON only, no result backend, `task_acks_late`, `task_reject_on_worker_lost`,
  prefetch 1, visibility timeout 900 s (must exceed the lease; validated in Settings), hard task
  limit 3600 s, `worker_hijack_root_logger=False`, never eager. Tests use Redis DB 15 and queue
  `tasklane-test`.
- **Claim** (`services/execution.py`, one transaction): read the job `FOR UPDATE`, then
  `queued`/`retry_wait` -> `running` via `transition_job` (from `retry_wait` only if
  `retry_at <= now()`: `only_if_retry_due`), `attempt_count + 1`, attempt row with a 60 s lease,
  `attempt.started`. Anything else is a duplicate or stale message: **dropped with no writes**,
  logged as `message dropped` with a reason.
- **Database outage in a task:** `run_job` catches DB-connectivity errors and calls
  `task.retry(countdown=backoff, max_retries=None)`, because with late acks a raising task is still
  acked (the message would be lost and the job stuck `queued`). Safe either way: a retry after a
  committed claim is a dropped duplicate. Other exceptions escaping `run()` are acked (logged).
- **Worker identity** `worker@<hostname>:<pid>`. Each worker process creates its own engine on first
  use and rebuilds it if it finds itself in a forked child (`queue/worker_process.py`).
- **Progress = lease renewal + fencing:** every progress report renews the lease
  (`UPDATE ... WHERE id = :attempt AND ended_at IS NULL`); if that matches nothing the worker stops
  without writing (`RunOutcome.FENCED`).
- **Phase 4 failure handling:** any exception in a stage fails the job permanently with
  `error_class = internal_error` and a safe summary (traceback only in logs). No retries yet.
- **Placeholder workflow:** every workflow type runs one `dummy` stage (`DUMMY_STAGE_SECONDS`,
  default 3, five progress steps) until Phases 5-6. (Since Phase 5 only `repo_index` does.)
- **Compose:** `dispatcher` and `worker` services, Redis with append-only persistence on the
  `redisdata` volume (`docker compose down -v` also deletes queued messages).
- **Dev reload runner** (`backend/devtools/reload.py`), not the `watchfiles` CLI: watchfiles 1.3.0
  kills its child 5 s after receiving SIGTERM regardless of `--sigint-timeout`, which cut off
  running jobs on every `docker compose stop`. The runner forwards SIGTERM (Celery warm shutdown),
  waits `--grace` (worker 25 s < `stop_grace_period` 30 s), restarts on `.py` changes, and exits
  when its child exits so the restart policy applies. Verified: a 10 s job finishes on stop.
- **Boundaries:** `workflows` imports no Celery/HTTP/database code; `services` never imports
  Celery or the Celery modules of `queue` (only `app.queue.broker`, a Redis ping); `api` never
  imports `queue`.

## Decisions made (Phase 5, part A)

- **pypdf `>=6.21.4,<7` with the `crypto` extra** (pypdf 6.21.4, cryptography 50.0.2), for
  AES-encrypted PDFs. Owner-password-only PDFs (`decrypt("")` succeeds) are processed and flagged
  `owner_password_only` in the manifest.
- **Stages** (`app/workflows/pdf/stages.py`, progress weights in brackets): `validate_pdf` [5],
  `extract_text` [30], `chunk_document` [5], `summarize` [55], `write_artifacts` [5].
  `workflows/registry.py` maps workflow type → stages; `repo_index` keeps `dummy` until Phase 6.
- **Parser sandbox** (`workflows/pdf/sandbox.py` + `limited_launcher.py` + `parse_child.py`):
  `python -I` child, env only `PATH`/`LANG`, own session, `RLIMIT_AS` (1 GiB), `RLIMIT_CPU`
  (deadline + 5 s), `RLIMIT_FSIZE` (64 MiB), no core dumps, all set by `limited_launcher.py`
  inside the child (no `preexec_fn`: unsafe in a threaded worker); parent deadline + `killpg(SIGKILL)`; JSON lines on stdout; parent trusts
  only known error classes and int params. Killed/out of memory/EFBIG → `pdf_too_complex`; any
  other unexpected exit → `internal_error` (stderr tail only in logs). `parse_child.py` imports
  only stdlib + pypdf, the launcher only stdlib (tests). Python ignores SIGXFSZ, so the file-size
  limit shows up as `EFBIG`.
- **Limits:** 500 pages, 2,000,000 raw characters (fail, never truncate), validate 60 s,
  extract 300 s, summarize stage 900 s (`stage_timeout`), provider call 20 s. Settings refuse
  `PROVIDER_TIMEOUT_SECONDS > LEASE_SECONDS / 2` and a Celery hard limit not longer than the sum
  of stage limits, an output limit below `PDF_MAX_TEXT_CHARS * 12 + pages * 64` bytes, and
  invalid chunk/summary sizes (all checked at startup). Tests with a 30 s lease set the provider
  timeout to 10 s. Provider output is cut to `max_output_chars` by the stage, and a combine round
  that doesn't shrink raises (map-reduce always terminates).
- **Scanned rule:** fewer than 100 visible characters in total, or fewer than 20 per page on
  average → `pdf_no_text`.
- **Errors** (`app/domain/errors.py`): `JobError(error_class, **params)` with a fixed message
  template per class (`ERRORS`); `INPUT_ERROR_CLASSES` lives there (state machine imports it).
  In part A every class is PERMANENT; unknown exceptions → `internal_error`. A failure appends
  `stage.failed {stage, error_class}`.
- **Lease keep-alive:** `StageContext.keep_alive()` renews the lease; the sandbox calls it every
  `LEASE_SECONDS / 4` while waiting; summarize renews after every provider call. Fenced → the
  parser is killed and nothing more is written.
- **Storage layout:** `work/{job_id}/` (intermediate: `validate.json`, `pages.jsonl`,
  `extract.json`, `chunks.jsonl`, `summary.json`) and `artifacts/{job_id}/` (`summary.md`,
  `manifest.json`), via `storage/workspace.py` (atomic writes: temp in same dir, fsync,
  `os.replace`, fsync dir). Outputs are deterministic (no timestamps; chunk ids hash input sha,
  chunker version, settings, index, text), so every stage is safe to re-run (tested byte for byte).
- **Artifacts:** `save_artifact` only remembers them (and checks the lease); the rows are upserted
  on `(job_id, artifact_type)` in the `_succeed` transaction, so artifacts are visible only for
  succeeded jobs; `GET /v1/jobs/{id}/artifacts/{artifact_id}`
  returns `FileResponse` with `nosniff`, `ETag: "<sha256>"`, `Cache-Control: private, no-cache`;
  404 `artifact_not_found` (also other job's artifact / malformed id), 410 `artifact_gone` (file
  missing or size mismatch). Job detail artifacts include `download_url`.
- **Providers** (`app/providers/`): `Provider` protocol, `StubProvider` (`stub-extractive-v1`,
  deterministic, no usage), `create_provider("stub")`. Providers import nothing from db, storage,
  workflows, services, queue, api (test). Map-reduce summarization; chunk 600 / final 2000 /
  combine input 12000 characters.
- **`provider_calls` table (migration 0003):** one row per call (attempt, stage, purpose
  `chunk`/`combine`, provider, model, latency, success, error class, char counts, tokens); never
  the text. Recorded in the same transaction as the lease renewal.
- **Samples:** `python -m devtools.pdf_samples <folder>` writes `report.pdf`, `scanned.pdf`,
  `encrypted.pdf`, `owner_locked.pdf`, `corrupt.pdf`; tests import the same generators.
- **E2E isolation:** e2e processes get the test `STORAGE_ROOT` (guarded in the fixture) and no
  `*API_KEY*` variables; a test reads `/proc/<pid>/environ` of the dispatcher, the Celery main
  process and the worker child, and checks the real volume has nothing for the job.
- **Part B (when asked):** backoff 5 s base doubling, 300 s cap, equal jitter: the first retry is
  2.5–5 s out and the second 5–10 s (use these ranges in docs and walkthroughs); budget CHECK;
  fault injection (`ENABLE_FAULT_INJECTION` + per-job option, 422 when disabled); stage
  checkpoints and resume (migration 0004).

## Carry-forward requirements

Items later phases must address. Remove an item only when it is implemented and tested.

- **Phase 5B — error classification:** replace the "any exception fails permanently" stopgap
  with transient vs permanent errors, `retry_wait` + delayed outbox rows, and the attempt budget.
  New stages must keep calling `report_progress`/`keep_alive` within the lease, and any new slow
  call needs a timeout of at most half the lease.
- **Phase 5B — retry timing:** a retry's outbox `available_at` must be >= the job's `retry_at`,
  both computed from the same database clock in the same transaction (e.g. `now() + interval`),
  never Python time. Otherwise the one legitimate retry message can be dropped as
  `retry_not_due` and the job stalls in `retry_wait`.
- **Phase 5B — attempt budget at claim:** the claim relies on the invariant
  `attempt_count < max_attempts` for claimable jobs (the DB CHECK would reject a violation). Keep
  it true in every path that moves a job to `queued`/`retry_wait`.
- **Phase 5B — transient database errors inside a running stage** (in `report_progress`,
  `keep_alive`, deadlock aborts) currently fail the job permanently; classify them as transient.
- **Phase 6 — reuse the sandbox pattern** for anything that parses untrusted ZIP content, and
  keep e2e processes on the test `STORAGE_ROOT` (the isolation test must keep passing).
- **Phase 7/11 — fenced writes to files:** a fenced worker makes no more database writes, but it
  can finish replacing a file under `work/` or `artifacts/` first. Harmless while outputs are
  deterministic; a non-deterministic provider (Phase 11) or recovery that starts a new attempt
  (Phase 7) must write per-attempt files or otherwise prevent two attempts writing the same path.
- **Phase 9 — rendering `summary.md`:** it contains PDF text as Markdown (only Markdown
  punctuation is escaped). The dashboard must not render it with raw HTML or active links.
- **Phase 7 — orphaned parser processes:** the PDF parser runs in its own session so it can be
  killed as a group; if the *worker* is killed (SIGKILL/OOM), the parser is not killed with it. It
  exits at its next write to the closed stdout pipe (per page during extraction), when it
  finishes, or at `RLIMIT_CPU` (deadline + 5 s). Consider a parent-death signal.
- **Phase 7 — heartbeats independent of progress updates** (a background heartbeat for stages
  that can't report progress often enough).
- **Phase 7 — lost messages:** recovery must detect claimable jobs (`queued`, or `retry_wait`
  past its `retry_at`) with **no open attempt** and **no unpublished outbox row**, older than a
  threshold, and re-queue them through the outbox. Causes: Redis losing a message, a manually
  retried job whose message was lost, and a task that hit a non-database error before its claim
  committed (the message is acked).
- **Phase 7 — dead workers:** jobs left `running` by a worker killed mid-stage, or by the hard task
  time limit, are recovered via lease expiry (close the attempt as `lease_expired`, then
  `retry_wait` or `failed` per the state machine).
- **Phase 7 — lock order:** the claim locks job then attempt; progress and finishing lock
  attempt then job. Recovery must lock attempt then job (or detect and retry deadlock aborts), or
  it can turn a live worker's progress write into a failure.
- **Phase 8 — event order:** order and replay events by `seq` only, never `created_at` (`now()`
  is the transaction start time).
- **Phase 9 — queue health:** surface dispatch failures (`dispatch_attempts`,
  `last_dispatch_error`) and the oldest queued job's age.
- **Phase 10/11 — log redaction:** worker failure logs include the full exception text (by
  design: details stay out of the database). Add redaction before real providers and API keys
  are involved.
- **Phase 12 — uploads:** clean up interrupted uploads (`receiving` rows and their files), and make
  retention never delete files of uploads referenced by unfinished jobs.
- **Phase 12 — work folders:** `work/{job_id}/` intermediate files are kept forever for now;
  retention must clean them (never for unfinished jobs), along with stray `.*.tmp` files left by
  a crash mid-write.
- **Dev note:** restarting the worker (including by live reload after a code change) interrupts
  running jobs; they stay `running` until Phase 7 recovery.

## Commands

```powershell
docker compose up --build -d                 # start; rebuild after dependency or alembic.ini changes
docker compose run --rm api pytest           # all tests (unit + integration + e2e)
docker compose run --rm api pytest -m "not e2e"   # skip the slower end-to-end tests
docker compose logs -f worker dispatcher     # watch jobs being published and run
docker compose run --rm api ruff check app tests migrations devtools
docker compose run --rm api ruff format app tests migrations devtools
docker compose run --rm migrate              # apply migrations
docker compose run --rm migrate alembic check   # do models and database agree?
docker compose run --rm api alembic revision --autogenerate --rev-id 0003 -m "describe change"
```

Regenerate `backend/uv.lock` after editing dependencies (runs uv in a throwaway container):

```powershell
docker run --rm -v "${PWD}\backend:/src" -w /src python:3.13.18-slim sh -c "pip install -q uv==0.13.6 && uv lock"
```

## Phase list

| # | Phase | Status |
|---|---|---|
| 0 | Planning: architecture, versions, working rules | **Done** |
| 1 | Compose + FastAPI skeleton + health routes | **Done** |
| 2 | Migrations + state machine | **Done** |
| 3 | Upload and job APIs with idempotency and outbox | **Done** |
| 4 | Dispatcher + Celery worker with one dummy stage | **Done** (awaiting developer verification) |
| 5 | PDF workflow + retries | **Part A done** (awaiting developer verification); Part B not started |
| 6 | ZIP workflow + security | Not started |
| 7 | Leases, recovery, cancellation, manual retry | Not started |
| 8 | SSE with replay | Not started |
| 9 | Dashboard | Not started |
| 10 | Observability | Not started |
| 11 | Optional LLM provider | Not started |
| 12 | Hardening, docs, demo | Not started |

Update the **Status** column when a phase is finished and verified. Notes on the scope of each
phase (including proposed adjustments) are in `docs/architecture.md` → "Build phases".
