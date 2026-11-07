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
  default 3, five progress steps) until Phases 5-6. (Removed in Phase 6: the class lives on as
  `tests/dummy_stage.py`; slow test jobs use `STUB_DELAY_SECONDS`.)
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

## Decisions made (Phase 5, part B)

- **Classification** (`domain/errors.py`): transient = `provider_timeout`,
  `provider_unavailable`, `provider_rate_limited`, `stage_timeout`, `database_unavailable`,
  `database_conflict`, `storage_unavailable`, `injected_transient`; permanent = input errors,
  `fault_injection_disabled`, `injected_permanent`, and any unclassified exception
  (`internal_error`). `services/execution.py::_classify` maps SQLAlchemy
  `OperationalError`/`InterfaceError`/pool timeouts → `database_unavailable`, SQLSTATE
  `40P01`/`40001` → `database_conflict`, `OSError` with `ENOSPC/EIO/EROFS/EDQUOT/EAGAIN` →
  `storage_unavailable`. Providers raise `ProviderError` subclasses (`providers/base.py`); the
  summarize stage records the call and re-raises `JobError(exc.error_class)`.
- **Backoff** (`domain/retry.py`): equal jitter, `d = min(RETRY_BASE_SECONDS × 2^(n−1),
  RETRY_MAX_SECONDS)`, delay `d/2 + random·d/2`; defaults 5 s / 300 s → first retry 2.5–5 s,
  second 5–10 s (use these ranges in docs and walkthroughs). `RetryPolicy` takes an injectable
  random source. e2e uses `RETRY_BASE_SECONDS=1`.
- **Failure transaction** (`_fail` + `_apply_failure`): close attempt (fenced) → `stage.failed`
  `{stage, error_class, retryable}` → `SELECT ... FOR UPDATE` on the job → `status_after_failure`
  → `transition_job`. For `retry_wait`, `retry_at` comes from `SELECT now() + interval` in that
  transaction and is written both to the job and, via `enqueue_retry`, to the outbox
  (`ON CONFLICT (job_id) WHERE published_at IS NULL DO UPDATE SET available_at =
  GREATEST(...)`). Budget exhausted → `failed` with "<message> Gave up after N attempts." and
  `gave_up_after_attempts` in the event; transient during `cancel_requested` → `cancelled`.
  `RunOutcome` gained `RETRY_SCHEDULED` and `CANCELLED`. The failure transaction is retried
  up to 3 times on `40P01`/`40001`. A failure while recording success (`_succeed`) is
  classified like any other but names no stage (no `stage.failed`).
- **Migration 0004:** `ck_jobs_claimable_has_attempt_left` (`status NOT IN ('queued',
  'retry_wait') OR attempt_count < max_attempts`) and `stage_checkpoints` (PK `(job_id, stage)`,
  `stage_version`, `attempt_no`, `input_fingerprint`, `outputs` JSONB array of
  `{path, size_bytes, sha256}`; a list because `extract_text` writes two files).
- **Checkpoints** (`domain/checkpoints.py`, pure): a stage is checkpointed if it has
  `stage_version`, `output_paths(workspace)` and `fingerprint_params(job)` (validate, extract,
  chunk, summarize; not `write_artifacts`, which must register artifacts in the successful
  attempt). Fingerprint = SHA-256 of stage, version, params and every upstream output
  `{path, size, sha256}`. Reuse needs: no earlier stage ran in this attempt, same version, same
  fingerprint, same file set, each file's size and SHA-256 verified (hashing last). Once one
  stage reruns, all later ones do. Reuse emits `stage.skipped {stage, reason:
  "checkpoint_reused"}`. Checkpoints are written in the `stage.completed` transaction and only
  read for `attempt_no > 1`.
- **Per-chunk reuse:** `work/{job_id}/summaries/{chunk_id}.json` holds `chunk_sha256`, the
  configured provider and model (not the provider's reported model), `stage_version`,
  `max_output_chars`, summary, tokens; reused only if all match. `summary.json["calls"]`
  counts summaries (cached or not), so reruns stay byte-identical.
- **Fault injection** (`providers/faults.py`): option `{"fault_injection": {"mode":
  "transient", "failures": 1-20}}` or `{"mode": "permanent"}` (pydantic discriminated union, extra
  forbidden). Transient: the first provider call of attempts 1..N raises `injected_transient`.
  Permanent: first call raises `injected_permanent`. `ENABLE_FAULT_INJECTION` default false
  (commented out in `.env.example`, `${ENABLE_FAULT_INJECTION:-false}` in Compose, so a shell
  variable enables it for one session); API → `422 fault_injection_disabled` (checked after the
  idempotent-replay shortcut); worker re-check → permanent `fault_injection_disabled` before any
  stage; startup warnings from the API lifespan and Celery `worker_ready`. Stored options omit
  options that are off (`exclude_none`).

## Decisions made (Phase 6)

Full reasoning for each decision: `PHASE6_NOTES.md`.

- **Stages** (`app/workflows/repo/stages.py`, weights): `validate_archive` [5], `extract_files`
  [15], `filter_files` [5], `chunk_sources` [5], `summarize` [65], `write_artifacts` [5]. The
  first five are checkpointed. Work files (generated names only): `archive.json`, `files.bin` +
  `extract.json`, `files.jsonl` + `filter.json`, `chunks.jsonl`, `summary.json`.
- **Shared sandbox:** `app/workflows/sandbox.py` (`run_sandboxed`, `SandboxLimits`) and
  `app/workflows/limited_launcher.py` serve both children; each caller passes its
  `too_complex_error` and the `allowed_errors` its child may report. `pdf/sandbox.py` is a thin
  wrapper. ZIP child: `workflows/repo/zip_child.py` (stdlib only, test-enforced), wrapper
  `workflows/repo/archive.py` re-validates the child's output files.
- **Two sandboxed passes:** `inspect` reads only the central directory (rejections, entry kinds);
  `extract` decompresses only entries that pass the path rules and whose declared size is within
  `ZIP_MAX_FILE_BYTES`. Skipped paths (node_modules, nested archives, ...) are never
  decompressed, and `ZIP_MAX_TOTAL_BYTES` counts only extracted files.
- **No file is written under an archive name:** extracted bytes go into `files.bin` at offsets
  recorded (with SHA-256) in `extract.json`. Names are still validated, because they appear in
  outputs.
- **Whole-archive rejection (`zip_unsafe_path`)** for: NUL (checked on `orig_filename`), control
  characters, backslash, leading `/`, drive letter `^[A-Za-z]:`, empty / `.` / `..` segments, a
  name whose `realpath` against the job's work folder leaves it, duplicates, NFC+casefold
  collisions, a path that is both file and folder, a name over 4,096 characters. Both
  `orig_filename` and `filename` (Unicode extra field) are checked. Names over
  `ZIP_MAX_PATH_LENGTH` skip `realpath` and the folder-prefix set (cost), and are skipped. Encrypted flag or AES method anywhere → `zip_encrypted`. Any read
  error → `zip_corrupt`, except host disk errnos (`ENOSPC/EIO/EROFS/EDQUOT/EAGAIN`) →
  transient `storage_unavailable` (allowed from the ZIP child).
- **Path too long (240) / too deep (20 components): skipped, not rejected**; the child cuts long
  names to the limit in `archive.json`.
- **Bomb defense:** declared sizes only fail fast (declared total, declared ratio) or skip early;
  real bytes are counted per 64 KiB block (`Budget`): total > limit → `zip_too_large`; per entry
  or overall `out > ratio × compressed` once `out > ZIP_RATIO_CHECK_MIN_BYTES` (1 MiB) →
  `zip_bomb`; a file over the per-file limit stops reading and is skipped `too_large`. A header
  that understates a size ends as `zip_corrupt` (zipfile stops at the stated size; CRC fails).
- **`filter_files` deletes `files.bin`** when it fails permanently (`repo_no_sources`,
  `repo_too_large`): no retry can use it. Successful jobs keep it (Phase 12 retention).
- **Symlinks / special files** from the mode bits (`external_attr >> 16`): skipped `symlink` /
  `special_file`, content never read.
- **Filtering** (`workflows/repo/filters.py`, pure): extension allowlist (+ README, LICENSE,
  Makefile, Dockerfile), 14 skip reasons in `SkipReason`; path rules in a fixed order; content
  rules: NUL in first 8 KiB or >10 % replacement characters → `binary`, blank → `empty`,
  JS/TS/CSS/JSON/HTML averaging >250 chars per line → `minified`. Hidden files (incl. `.env`)
  are skipped. `REPO_EXCLUDED_DIRS` (comma list, empty = default list) matched case-insensitively
  at any depth.
- **Chunking by lines** (`workflows/repo/chunking.py`) reuses `pdf/chunking.group_pieces` and
  `chunk_id` (version `repo-lines-1`); a chunk never spans files; global chunk index.
- **Summaries:** per file `summarize_document` (map, then combine if >1 chunk); overview =
  `reduce_summaries` over `"path: summary"` cut to `final_max_chars` (keeps the
  terminates-guarantee). `planned_calls` is an upper bound checked against
  `REPO_MAX_PROVIDER_CALLS` (1000) before any call; `filter_files` also stops early once the text
  can't fit (`calls × chunk_max_chars`). `SummaryCaller` (`workflows/common.py`) is shared with
  the PDF summarize stage (cache, fault injection, recording, output cap, progress).
- **Artifacts:** `summary.md` (paths: `escape_markdown` after making category-C characters
  visible as `\uXXXX`; summaries in `fenced()` blocks with a fence longer than any `~` run
  inside), `chunks.jsonl` (`application/x-ndjson`) and `manifest.json`
  (`tasklane.repo_manifest/1`), both written with `ensure_ascii=True`. No migration was needed
  (`chunks` was already an artifact type; error classes are unconstrained text).
- **Errors:** nine permanent input errors (`zip_corrupt`, `zip_encrypted`, `zip_unsafe_path`,
  `zip_too_many_files {entries, limit}`, `zip_too_large {limit}`, `zip_bomb {ratio}`,
  `zip_too_complex`, `repo_no_sources {files}`, `repo_too_large {limit}`), all in
  `INPUT_ERROR_CLASSES`.
- **`escape_markdown` escapes `&`** too (both workflows), so `&#x202e;` stays literal.
- **Settings** (bytes, validated at startup): `ZIP_MAX_ENTRIES` 5000 (max 100,000; the inspect
  output limit grows with it), `ZIP_MAX_TOTAL_BYTES`
  500 MiB, `ZIP_MAX_FILE_BYTES` 5 MiB (≤ total), `ZIP_MAX_COMPRESSION_RATIO` 100,
  `ZIP_RATIO_CHECK_MIN_BYTES` 1 MiB, `ZIP_MAX_PATH_LENGTH` 240, `ZIP_MAX_PATH_DEPTH` 20, ZIP
  timeouts 60/300 s, `ZIP_SANDBOX_MEMORY_MB` 512 (≥ 128 + 3 × file MiB), `REPO_EXCLUDED_DIRS`,
  `REPO_MAX_PROVIDER_CALLS`. `config.py` imports the filter module lazily (reading settings
  doesn't load `workflows`). The Celery hard limit must exceed the larger workflow's stage sum.
- **Checkpoint fingerprints:** extract and filter include every path-rule setting (excluded
  dirs, path length/depth, file limit); `REPO_MAX_PROVIDER_CALLS` is in none (it can only fail a
  stage, never change a successful output).
- **`STUB_DELAY_SECONDS`** (default 0, < provider timeout) replaces `DUMMY_STAGE_SECONDS`;
  e2e timing tests use it. `repo_index` accepts the `fault_injection` option.
- **Tests:** archives are built in code (`devtools/zip_samples.py`; header patching for
  encrypted flags, NUL names and lying sizes). Test limits: 50 entries, 512 KiB total, 128 KiB
  per file, 16 KiB ratio floor. Every rejection test snapshots the storage folder's parent before
  and after the job.

## Carry-forward requirements

Items later phases must address. Remove an item only when it is implemented and tested.

- **All later stages (Phase 6+):** keep calling `report_progress`/`keep_alive` within the lease;
  any new slow call needs a timeout of at most half the lease. A checkpointed stage must list
  every file it writes in `output_paths` and every setting its output depends on in
  `fingerprint_params`, and bump `stage_version` when its output changes for the same input.
- **Phase 7 — lease-expiry retries:** recovery moving an expired job to `retry_wait` must reuse
  the Part B rules: `retry_at` and the outbox `available_at` from one database-clock value in one
  transaction (`enqueue_retry`), the backoff policy, and the budget (`claimable_has_attempt_left`
  will reject a violation).
- **Phase 7 — backoff after a manual retry:** the delay uses the lifetime `attempt_no`, so after
  a manual retry (attempt 4+) the first automatic retry waits 20–40 s, not 2.5–5 s. Base it on the
  attempt number within the current run, or document it, when manual retry is built.
- **Phase 7 — manual retry and checkpoints:** a manual retry's first attempt has
  `attempt_no > 1`, so it resumes from verified checkpoints. Decide whether that's wanted, or
  whether manual retry should clear `stage_checkpoints` and start fresh.
- **Phase 11 — real providers:** raise `ProviderTimeout`/`ProviderUnavailable`/
  `ProviderRateLimited` for transient failures; add permanent `provider_auth` and
  `provider_rejected` classes. Per-chunk cache keys include provider and model, so switching
  models re-summarizes.
- **All later phases — e2e isolation:** keep e2e processes on the test `STORAGE_ROOT`; the
  isolation test (now for a PDF and a repo job) must keep passing.
- **Phase 7/11 — fenced writes to files:** a fenced worker makes no more database writes, but it
  can finish replacing a file under `work/` or `artifacts/` first. Harmless while outputs are
  deterministic; a non-deterministic provider (Phase 11) or recovery that starts a new attempt
  (Phase 7) must write per-attempt files or otherwise prevent two attempts writing the same path.
- **Phase 9 — rendering `summary.md`:** the PDF one contains PDF text as Markdown (only
  Markdown punctuation is escaped); the repo one keeps summaries in fenced blocks. The dashboard
  must not render either with raw HTML, active links or auto-linked bare URLs, and must escape
  file paths from `manifest.json` (they are untrusted).
- **Phase 7 — orphaned parser processes:** the PDF parser and the ZIP child run in their own
  session so they can be killed as a group; if the *worker* is killed (SIGKILL/OOM), the child is
  not killed with it. It exits at its next write to the closed stdout pipe (per page / per
  extracted file), when it finishes, or at `RLIMIT_CPU` (deadline + 5 s). Consider a
  parent-death signal.
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
- **Phase 11 — repository text sent to a real provider:** indexed files can contain secrets
  (hidden files like `.env` are skipped, but a key in `config.py` is not). Add secret scanning or
  redaction before repo text leaves the machine, delimit repo text *and file paths* as data in
  the prompt, and test with prompt-injection content (`docs/security.md`, "Not covered").
- **Phase 12 — repo work folders are large:** `work/{job_id}/files.bin` can approach
  `ZIP_MAX_TOTAL_BYTES` (500 MiB) per *succeeded* job (one upload can feed many jobs), plus
  `files.jsonl`; retention must clean them. Consider also capping extraction near the text the
  call limit allows (`calls × chunk_max_chars × 4` bytes) without misfiring on files the filter
  would skip (minified, binary).
- **Phase 11 — summarize time vs call limit:** 1,000 calls at ~2 s each exceed the 900 s
  summarize limit, and combine calls aren't cached across attempts. Revisit
  `SUMMARIZE_TIMEOUT_SECONDS` / `REPO_MAX_PROVIDER_CALLS` and cache combines for a real provider.
- **Phase 12 — huge central directories:** the ZIP child reads the whole central directory
  before checking `ZIP_MAX_ENTRIES` (a ZIP with millions of entries is stopped by the sandbox's
  memory/time limits as `zip_too_complex`, not `zip_too_many_files`). An end-of-central-directory
  pre-check would give the clearer error.
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
docker compose run --rm api alembic revision --autogenerate --rev-id 0005 -m "describe change"
docker compose run --rm -v "${env:USERPROFILE}\tasklane-demo:/out" api python -m devtools.zip_samples /out   # sample ZIPs
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
| 5 | PDF workflow + retries | **Done** (awaiting developer verification) |
| 6 | ZIP workflow + security | **Done** (awaiting developer verification) |
| 7 | Leases, recovery, cancellation, manual retry | Not started |
| 8 | SSE with replay | Not started |
| 9 | Dashboard | Not started |
| 10 | Observability | Not started |
| 11 | Optional LLM provider | Not started |
| 12 | Hardening, docs, demo | Not started |

Update the **Status** column when a phase is finished and verified. Notes on the scope of each
phase (including proposed adjustments) are in `docs/architecture.md` → "Build phases".
