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
4. **Preserve module boundaries** between `api`, `domain`, `db`, `queue`, `workflows`,
   `providers`, `observability`, and `frontend`:
   - `domain` holds pure logic (state machine, error classes, rules) and imports no framework,
     database, or queue code.
   - `api` handles HTTP only and calls into services; it never talks to Celery directly.
   - `db` owns SQLAlchemy models, sessions, and repositories.
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
- **Redis Python client stays on 5.2.x**: Celery 5.5's kombu caps it at 5.2.1.
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

## Commands

```powershell
docker compose up --build -d                 # start; rebuild after dependency or alembic.ini changes
docker compose run --rm api pytest           # all tests (unit + integration)
docker compose run --rm api ruff check app tests migrations
docker compose run --rm api ruff format app tests migrations
docker compose run --rm migrate              # apply migrations
docker compose run --rm migrate alembic check   # do models and database agree?
docker compose run --rm api alembic revision --autogenerate --rev-id 0002 -m "describe change"
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
| 2 | Migrations + state machine | **Done** (awaiting developer verification) |
| 3 | Upload and job APIs with idempotency and outbox | Not started |
| 4 | Dispatcher + Celery worker with one dummy stage | Not started |
| 5 | PDF workflow + retries | Not started |
| 6 | ZIP workflow + security | Not started |
| 7 | Leases, recovery, cancellation, manual retry | Not started |
| 8 | SSE with replay | Not started |
| 9 | Dashboard | Not started |
| 10 | Observability | Not started |
| 11 | Optional LLM provider | Not started |
| 12 | Hardening, docs, demo | Not started |

Update the **Status** column when a phase is finished and verified. Notes on the scope of each
phase (including proposed adjustments) are in `docs/architecture.md` → "Build phases".
