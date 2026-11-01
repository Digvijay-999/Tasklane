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

## Phase list

| # | Phase | Status |
|---|---|---|
| 0 | Planning: architecture, versions, working rules | **Done** |
| 1 | Compose + FastAPI skeleton + health routes | Not started |
| 2 | Migrations + state machine | Not started |
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
