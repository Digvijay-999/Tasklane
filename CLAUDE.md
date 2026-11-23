# Tasklane — maintainer guide for Claude Code

Tasklane is a local-first control plane for long-running AI processing jobs (PDF ingestion and
repository ZIP indexing). Its focus is **reliable orchestration**: durable state in PostgreSQL,
an outbox dispatcher, Celery workers, retries, leases and recovery, cancellation, SSE event
replay, retention. All 12 build phases are done; this file is the guide for changing it safely.

Where things are explained:

| Question | Document |
|---|---|
| What it is, quickstart, known limitations | `README.md` |
| Goals and acceptance criteria | `PROJECT_BRIEF.md`, `docs/acceptance.md` (criterion → tests) |
| Components, lifecycle, build phases, versions | `docs/architecture.md` |
| HTTP API, error codes | `docs/api.md` |
| Delivery guarantees, state machine, recovery, replay | `docs/reliability.md` |
| Threat model, upload/ZIP/PDF defenses | `docs/security.md` |
| Traces, metrics, logs | `docs/observability.md` |
| Real LLM provider | `docs/providers.md` |
| Retention, cleanup, every limit, demo, benchmark, audit | `docs/operations.md`, `docs/performance.md` |
| Why each decision was made | `docs/decisions/` (`DECISION_LOG.md` for Phases 0–11 in short form, `PHASE*_NOTES.md` with alternatives) |
| Dashboard design | `DESIGN.md` |
| Step-by-step manual checks | `docs/walkthroughs/` |

## About the developer

- Beginner with Python backends, PostgreSQL, Celery, and Docker.
- Comfortable with Node.js, Express, MongoDB, and REST APIs — comparisons to those are helpful
  (e.g. FastAPI ≈ Express, Pydantic ≈ zod, Alembic ≈ knex/Prisma migrations, Celery ≈ BullMQ,
  uv + `uv.lock` ≈ npm + `package-lock.json`, pytest ≈ Jest).
- Develops on **Windows** (PowerShell 5.1, Docker Desktop). Give commands that work there
  (`curl.exe`, `--data-binary "@file"`, JSON written to a file: PS 5.1 strips quotes from inline
  JSON for native programs).
- Another local project (landsync) often holds ports 8000 and 5173; ask before stopping it.

## Working rules

1. **Plan first** for anything non-trivial: show a short plan and wait for approval, unless the
   request says it is pre-approved. Do only what was asked.
2. **Explain important design decisions** in plain language, and record them in a
   `docs/decisions/` note (decision, alternatives, reason, plain language).
3. **Every change includes tests** for its logic, and states the exact commands to run them.
4. **At the end of a change**, list the files created or changed, how to run it, and how to
   verify it by hand.
5. **Keep `README.md` and the docs accurate** to what actually works. Never claim unimplemented
   features, invented performance numbers, production scale, or "exactly once" execution.
   Performance numbers come only from `devtools.benchmark` runs recorded in `docs/performance.md`.
6. **Never commit secrets.** `.env` is gitignored; `.env.example` has placeholders only.
7. **Bind every published port to `127.0.0.1`** (e.g. `"127.0.0.1:8000:8000"` in Compose).
8. Ask before installing dependencies, pushing, deleting work, or resetting Git state. **Never
   commit** — the developer commits manually.
9. Write files with LF line endings (`.gitattributes` enforces `eol=lf`; Python's `write_text`
   on Windows writes CRLF unless given `newline="\n"`). Never run repository-wide line-ending
   fixes: the untracked `.claude/` folder is legitimately CRLF.

## Module boundaries (tests enforce them: `tests/unit/test_boundaries.py`)

- `domain` — pure logic (state machine, error classes, retry, checkpoints, costs); imports only
  the standard library and other `domain` modules.
- `api` — HTTP only: parse input, call a service, shape the response, map error codes to status
  codes. Never imports `db`, `storage` or `queue`.
- `services` — one use case each; coordinates `db`, `storage` and `domain` and owns transactions.
  Knows nothing about HTTP (raises `ServiceError` with a stable code); no Celery (only
  `app.queue.broker`, a Redis ping).
- `db` — SQLAlchemy models, sessions, repositories. SQL only; **never commits**.
- `storage` — files on disk: generated paths, atomic writes, deletes. No database or HTTP code.
- `queue` — Celery app, outbox dispatcher, recovery loop, task entry points. `recovery.py` and
  `services/heartbeat.py` import no Celery and no HTTP.
- `workflows` — stage logic; receives a context, provider and storage, never HTTP, Celery or
  database objects. Sandboxed children (`pdf/parse_child.py`, `repo/zip_child.py`,
  `limited_launcher.py`) import only the standard library (plus pypdf for the PDF child).
- `providers` — the summarizer interface and its implementations; knows nothing about jobs.
- `maintenance` — the cleanup; database rows and files only, no HTTP/queue/workflow code.
- `observability` — logging, tracing, metrics setup.
- `frontend` — talks to the backend only through the HTTP API.

## Design rules every change must keep

State and delivery
- **Status changes only through `transition_job`** (conditional `UPDATE ... WHERE status =
  :from`, legal transitions from `domain/state_machine.py`). `applied=False` means you lost a
  race. Value lists are `text` + CHECK constraints, never native enums; **every constraint needs a
  test** in `tests/integration/test_constraints.py` (a meta-test checks).
- **Migrations are frozen snapshots** (`op.f(...)` names), compared strictly with the models by
  tests; they run only in the one-shot `migrate` service. Review autogenerate output by hand.
- **Lock order:** attempt, then job (then outbox). Claim: job, then a new attempt.
- **Job + event + outbox row in one transaction**; the dispatcher publishes; workers must treat
  every message as a possible duplicate (the claim drops it without writes).
- **Every decision about time uses the database clock** (`now()`), never the process's clock.
- **Event numbers are per job** (`jobs.last_event_seq` under the row lock). Events are deleted
  only together with their whole finished job.

Workers and stages
- **Every stage and every slow call has its own time limit** (the heartbeat keeps a lease alive
  forever otherwise); call `keep_alive()` while waiting on anything long; provider timeouts at
  most half the lease (validated at startup).
- **A checkpointed stage** lists every file it writes in `output_paths`, every setting its output
  depends on in `fingerprint_params`, and bumps `stage_version` when its output changes for the
  same input. Outputs are deterministic and written atomically, so stages are safe to re-run.
- **Errors:** raise `JobError(error_class, ...)` with a class from `domain/errors.py`; classify
  new infrastructure failures as transient or permanent in `services/execution.py::_classify`.
  Tracebacks go to logs only; jobs and events get the safe message.
- **Untrusted files are parsed only in the sandbox** (`workflows/sandbox.py`), never in the
  worker process; nothing from an archive is ever written under its own name or executed.
- **Providers:** text sent to a remote provider goes through `SummaryCaller` (scrubbing, cache,
  pacing, recording). The key stays in the worker container and only in the Authorization
  header; never log provider error bodies. Tests use `tests/fake_provider.py`, never the network;
  real calls belong in `tests/live`.

Retention
- **Never touch an unfinished job** (`queued`, `running`, `retry_wait`, `cancel_requested`) or
  anything it uses. **Rows first, files after:** delete or mark rows, commit, then delete files.
  Lock with `FOR UPDATE SKIP LOCKED`; every path through `LocalStorage.checked()`.

Telemetry
- New span attributes go into `ALLOWED_SPAN_ATTRIBUTES` (`tests/telemetry.py`) and
  `docs/observability.md`; never record exceptions on spans; metric labels only from
  `ALLOWED_LABELS` (never a job id); background loops create no spans per poll; worker child
  resources stay within the pool.

Dashboard
- Render every upload-derived string with `SafeText` (never `dangerouslySetInnerHTML`, never
  linkify). Cancel/Retry follow only the server's `can_cancel`/`can_retry`. Keep the build's CSP
  (no inline scripts or styles, no other origins, no `data:` assets). New event types should get
  a timeline treatment in `timelineModel.ts`. Keep the axe audit green
  (`e2e/accessibility.spec.ts`): no color-only states, no faded text, focusable scroll regions.

Tests
- Integration tests use `<POSTGRES_DB>_test` and Redis database 15 / queue `tasklane-test`;
  e2e processes use the test `STORAGE_ROOT` (an isolation test checks the real volume stays
  untouched).
- Exact dependency versions: `uv.lock` (regenerate with the command below), `package-lock.json`
  with exact versions in `package.json`, pinned Docker image tags, actions pinned to commit SHAs.

## Commands

```powershell
docker compose up --build -d                 # start; rebuild after dependency, alembic.ini or Dockerfile changes
docker compose run --rm api pytest           # all backend tests (unit + integration + e2e)
docker compose run --rm api pytest -m "not e2e"   # what CI runs
docker compose run --rm -e PROVIDER_API_KEY api pytest -m live   # real provider smoke test (needs the key)
docker compose run --rm api ruff check app tests migrations devtools
docker compose run --rm api ruff format app tests migrations devtools
docker compose logs -f worker dispatcher     # watch jobs being published and run
docker compose run --rm migrate              # apply migrations
docker compose run --rm migrate alembic check   # do models and database agree?
docker compose run --rm api alembic revision --autogenerate --rev-id 0006 -m "describe change"
docker compose run --rm api python -m app.maintenance.cleanup --dry-run   # retention (docs/operations.md)
docker compose exec -T api python -m devtools.benchmark --jobs 20         # docs/performance.md
powershell -ExecutionPolicy Bypass -File scripts\demo-recovery.ps1        # kill a worker mid-job
docker compose exec -T api python -m devtools.job_events <job_id>         # a job's event history
docker compose run --rm -v "${env:USERPROFILE}\tasklane-demo:/out" api python -m devtools.pdf_samples /out
docker compose run --rm -v "${env:USERPROFILE}\tasklane-demo:/out" api python -m devtools.zip_samples /out
# Dashboard: http://127.0.0.1:5173. From frontend/ with Node 24:
npm ci; npm test; npm run typecheck
npm run e2e            # Playwright incl. the accessibility audit; needs the demo settings (README "Tests")
npm run screenshots    # regenerate docs/screenshots
curl.exe -s http://127.0.0.1:8000/metrics
docker compose --profile observability up -d   # + Jaeger :16686, Prometheus :9090
```

Regenerate `backend/uv.lock` after editing dependencies (runs uv in a throwaway container):

```powershell
docker run --rm -v "${PWD}\backend:/src" -w /src python:3.13.18-slim sh -c "pip install -q uv==0.13.6 && uv lock"
```

## Notes

- Restarting the worker (including by live reload after a code change) interrupts running jobs;
  recovery retries them once their lease (60 s) expires.
- Playwright runs, the benchmark and the screenshot run create jobs in the development database;
  `RETENTION_FINISHED_JOB_DAYS=0` with the cleanup command removes finished ones.
- Things deliberately not done are listed in the README under "Known limitations".

## Phase list

| # | Phase | Status |
|---|---|---|
| 0 | Planning: architecture, versions, working rules | **Done** |
| 1 | Compose + FastAPI skeleton + health routes | **Done** |
| 2 | Migrations + state machine | **Done** |
| 3 | Upload and job APIs with idempotency and outbox | **Done** |
| 4 | Dispatcher + Celery worker with one dummy stage | **Done** (awaiting developer verification) |
| 5 | PDF workflow + retries | **Done** (awaiting developer verification) |
| 6 | ZIP workflow + security | **Done** (awaiting developer verification) |
| 7 | Leases, recovery, cancellation, manual retry | **Done** (awaiting developer verification) |
| 8 | SSE with replay | **Done** (awaiting developer verification) |
| 9 | Dashboard | **Done** (awaiting developer verification) |
| 10 | Observability | **Done** (awaiting developer verification) |
| 11 | Optional LLM provider | **Done** (awaiting developer verification) |
| 12 | Hardening, retention, evidence, polish | **Done** (awaiting developer verification) |
