# Decision log (Phases 0-11)

The short-form record of every decision, kept in `CLAUDE.md` while the project was built phase by
phase and moved here unchanged in Phase 12. Paths and setting names were correct when each entry
was written; later entries may supersede earlier ones (marked with strikethrough). The long-form
reasoning, with alternatives, is in the `PHASE*_NOTES.md` files next to this one (Phases 6-12).
File references such as `PHASE7_NOTES.md` mean the file of that name in this folder.

## Phase 0

- **Backend versions approved** (see `docs/architecture.md` §7). In Phase 1, verify the latest
  stable compatible patch versions when generating `uv.lock` — do not rely on memory.
- ~~Frontend versions are not locked yet~~ (locked in Phase 9; see below).
- **Tests run in Docker by default** (`docker compose run --rm api pytest`) against real PostgreSQL.
- **Recovery loop runs inside the dispatcher process**, but the dispatcher loop and the recovery
  loop are separate modules with separate tests, so they can become separate services later.
- **`cancel_requested -> succeeded` is legal.** If the last stage finishes before the worker sees
  the cancel request, the job ends `succeeded` and an event records that cancellation was
  requested but the job completed first.
- **Default branch is `main`.** The developer commits manually — do not commit.
- **Local storage** for uploads and artifacts lives under `storage/` (gitignored).

## Phase 1

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

## Phase 2

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

## Phase 3

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

## Phase 4

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

## Phase 5, part A

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
- ~~**Lease keep-alive:** `StageContext.keep_alive()` renews the lease~~ (superseded in Phase 7:
  the heartbeat thread renews it; `keep_alive()` is a fenced-flag check the sandbox calls every
  second). Summarize still renews after every provider call (as a fencing check). Fenced → the
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

## Phase 5, part B

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

## Phase 6

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

## Phase 7

Full reasoning (alternatives, plain language): `PHASE7_NOTES.md`.

- **Heartbeat** (`app/services/heartbeat.py`): a daemon thread per attempt, started in
  `JobExecutionService.run()` after the claim and stopped/joined by a `with` block however the
  attempt ends; each beat renews the lease in its own transaction on its own pooled connection
  (worker pool 3). Interval `HEARTBEAT_INTERVAL_SECONDS` (unset = lease/4, max lease/3). A failed
  beat is retried next interval; a dead thread sets `died` (stage writes still renew). Renewal
  matching nothing → `fenced` Event.
- **Fencing:** the execution service checks `fenced` at stage boundaries and before every write;
  stage writes still renew in their transaction (database check). `keep_alive()` is a flag check
  only; the sandbox calls it every `FENCE_CHECK_SECONDS` (1 s, `queue/worker_process.py`) and
  kills its child when it raises. A fenced worker may finish one deterministic file write
  (documented; harmless while outputs are deterministic).
- **Recovery** (`app/queue/recovery.py`, no Celery/HTTP imports, test-enforced): thread in the
  dispatcher process with its own stop Event (`start_in_thread`), every
  `RECOVERY_INTERVAL_SECONDS` (5, ≤ lease). One job per transaction, `LIMIT 1 FOR UPDATE SKIP
  LOCKED`, up to 100 of each kind per pass; a row whose handling raises is rolled back and
  skipped for that pass (`skip=`), never blocking the rest. Dispatcher pool 3.
- **Expired lease:** close attempt `lease_expired` (new *transient* error class) →
  `attempt.lease_expired {attempt_no, worker_id, last_heartbeat_at, lease_expired_at}` → lock
  job → `status_after_lease_expired`: `retry_wait` (backoff, `retry_at` = outbox `available_at`
  from one `now()`, `enqueue_retry`) / `failed` ("Gave up after N attempts.") / `cancelled`. The
  next attempt's trigger is `recovery` (claim reads `error_class`).
- **Lock order** everywhere: attempt, then job (then outbox). Claim: job, then a new attempt.
  Cancel/retry: job, then outbox. Tested with a repeated simultaneous finish + recovery race.
- **Lost messages:** `queued` (or `retry_wait` overdue by more than the grace), unchanged for
  `LOST_MESSAGE_GRACE_SECONDS` (300, ≥ 5 and ≥ 2 × recovery interval and dispatch poll), no open
  attempt, no outbox row unpublished or published within the grace → `enqueue_run_job` +
  `job.requeued {reason: lost_message, status, last_published_at}` (`lock_next_lost_message_job`).
- **Cancel** `POST /v1/jobs/{id}/cancel` (always 200 + job detail): `cancel_job()` locks the job;
  queued/retry_wait → `cancelled` + `DELETE` the unpublished outbox row (`outbox_removed`);
  running → `cancel_requested`; else no-op. The worker checks the status before each stage and
  ends `cancelled` (attempt outcome `cancelled`, `before_stage` in the event) in one transaction.
  `CancelDecision` has a `reason` (`already_cancel_requested`, `already_finished`).
- **Manual retry** `POST /v1/jobs/{id}/retry`: `retry_job()` locks the job; refusal → `409
  job_not_retryable {reason, status}`; allowed → `failed → queued`, `max_attempts =
  attempt_count + 3`, `manual_retry_count + 1`, `job.manual_retry {manual_retry_count,
  previous_error_class, attempt_count, max_attempts}`, new outbox row. **Resumes from verified
  checkpoints.** Backoff uses `run_attempt_no(attempt_no, max_attempts) = attempt_no -
  (max_attempts - 3)` (≥ 1) in the worker and recovery.
- **Job detail** adds `lease {attempt_no, worker_id, heartbeat_at, lease_expires_at, expired}`
  (expired vs the database `now()`), `can_cancel`/`cancel_blocked_reason`,
  `can_retry`/`retry_blocked_reason`, all from the state machine. Cancel/retry respond with the
  same detail.
- **Orphans:** `limited_launcher.py <parent_pid> <memory> <cpu> <fsize> <script> ...` calls
  `prctl(PR_SET_PDEATHSIG, SIGKILL)` via ctypes, then exits 1 if `getppid() != parent_pid`.
  Tested with a really killed parent process.
- **Settings** use `env_ignore_empty=True` (Compose passes unset `${VAR:-}` as ""). Property
  `heartbeat_seconds` replaced `keep_alive_interval_seconds`.
- **Dev tools:** `python -m devtools.ps` lists container processes (the image has no `ps`);
  `devtools.job_events` describes the new event types.

## Phase 8

Full reasoning (alternatives, plain language): `PHASE8_NOTES.md`.

- **One loop for replay and live** (`app/api/events.py::event_frames`): read the job's
  `last_event_seq`, then events with `cursor < seq <= last_event_seq` ORDER BY `seq` (500 per
  read; `services/events.py`, `db/repositories/events.py`), send them, cursor = last sent seq,
  sleep `SSE_POLL_SECONDS`, repeat. No LISTEN/NOTIFY, no Redis. Race-free because of per-job
  numbering (tested with a wrapper that commits right after each read).
- **Async generator, not threads:** the route is `async def`; each read runs in
  `anyio.to_thread` inside a short session closed before sleeping. Never a sync generator
  (it would hold one of Starlette's 40 shared threads per stream).
- **Resume position** (`parse_resume_position`): `Last-Event-ID` wins over `?after=`; empty =
  absent; missing/`0` = from the start; not `^[0-9]{1,18}$` → `400 invalid_cursor`
  (`details.source`); past the job's last seq → clamped (no replay, then live). Unknown job →
  `404 job_not_found`. All checked before the response starts.
- **Frame:** `id: <seq>`, `event: <event_type>` (re-checked against the DB regex), `data:` =
  `{seq, event_type, attempt_no, created_at, payload}` via `json.dumps(ensure_ascii=True)` (no raw
  CR/LF/U+2028 possible; the formatter raises if one appears). First block `retry: <ms>`.
  History items use the same object (`JobEventResponse`).
- **End:** job terminal and cursor == last seq → `event: stream.end`, data `{reason:
  job_finished|job_not_found, status, last_seq}`, **no `id:`**, then close. Clients close on it;
  a manual retry reopens the job, so a reconnect with the last id gets the new events.
- **Lifetime end** (`SSE_MAX_STREAM_SECONDS`): a comment plus a bare `id: <cursor>` (updates
  the client's last id without an event), no `stream.end`, then close (EventSource resumes).
- **Settings** (validated): `SSE_MAX_STREAMS` 50, `SSE_MAX_STREAM_SECONDS` 3600,
  `SSE_POLL_SECONDS` 0.5, `SSE_KEEPALIVE_SECONDS` 15 (≥ poll; ≤ lifetime),
  `SSE_RETRY_MILLISECONDS` 3000, `SSE_WRITE_TIMEOUT_SECONDS` 30. Passed through Compose so a
  shell variable overrides them.
- **Slots:** in-memory `StreamSlots` per API process (event-loop only, no lock), acquired after
  the job check, released in `EventStreamResponse.__call__`'s `finally` (which also `aclose()`s
  the generator); over the limit → `503 stream_limit_reached` with `details.limit`.
- **Disconnects:** uvicorn reports ASGI spec 2.3, so Starlette's `StreamingResponse` cancels the
  body on `http.disconnect` (tested: clean close and TCP reset free the slot within 2 s). A
  client that can't accept a write for `SSE_WRITE_TIMEOUT_SECONDS` is dropped (`send` wrapped in
  `anyio.fail_after`). The stream never writes to the database.
- **Database outage mid-stream:** connectivity errors are warned once per outage (type only) and
  retried with backoff (poll × 2ⁿ, capped at the keepalive interval); keepalives continue. A gap in seqs is logged as `event seq gap` (ERROR) and never
  skipped silently; missing rows at the end move the cursor to `last_event_seq` (no spinning).
- **`uvicorn --timeout-graceful-shutdown 5`** in both Dockerfile targets: otherwise stop/reload
  waits for open streams to end.
- **History route** `GET /v1/jobs/{id}/events/history?after=&limit=` (1-500, default 100):
  `{items, next_after (null when caught up), last_seq}`.
- **Logs:** `event stream opened` / `event stream closed` (`reason`: `disconnected`,
  `job_finished`, `max_lifetime`, `job_not_found`; `events_sent`, `last_seq`).
- **Tests:** streaming over real connections runs against a real `uvicorn` subprocess on a free
  port (`tests/integration/test_events_e2e.py`, marked e2e); TestClient only for streams that
  end by themselves. `devtools/sse_client.py` (stdlib only) and its parser `iter_sse` are reused
  by the tests. Pipeline fixtures are shared by assigning them from `test_e2e_pipeline`.

## Phase 9

Full reasoning (alternatives, plain language): `PHASE9_NOTES.md`; visual design: `DESIGN.md`.

- **Frontend versions** (npm, 2026-11-14): Node 24.21 (`node:24.21.0-bookworm-slim`), React
  19.3.1, TypeScript 7.0.2 (type checking only), Vite 8.3.8 + plugin-react 6.3.0, React Router
  8.5.1, TanStack Query 5.107.1, Base UI 1.9.0 (AlertDialog only), IBM Plex Sans/Mono via
  @fontsource 5.3.0; Vitest 5.1.0, jsdom 31.0.1, Testing Library, Playwright 1.65.0. Exact
  versions, committed `package-lock.json`, `npm ci`. Plain CSS with custom properties (no
  Tailwind, no UI kit, no Markdown renderer, no icon library).
- **Backend:** `GET /v1/overview` (`services/overview.py`, `db/repositories/overview.py`):
  `GROUP BY status` (can use `ix_jobs_status_created_at_id`, tested), oldest queued =
  `now() - updated_at` of queued jobs (DB clock), outbox pending (due *or* already failing: a
  failed publish moves `available_at` forward) / scheduled / failing / max attempts / last error
  type, oldest pending age (a failing row from `created_at`), Redis via `run_checks`. `GET /v1/meta` (in `api/overview.py`, settings + domain
  only): workflows (extensions, content types, max bytes, signature hex, stages + weights from
  `domain/workflows.py::WORKFLOW_STAGES`, kept equal to the worker's stages by a unit test),
  filename limit, upload TTL, fault injection flag. Job list items gain `upload_filename` (one
  PK query per page, `repositories/uploads.original_filenames`).
- **Live stream** (`src/lib/live/jobEventStream.ts`, hook `useJobEvents`): history pages first,
  then `EventSource(...?after=<last seq>)`; events kept in a Map by seq, only the contiguous
  prefix shown; gap → history read from the last contiguous seq (deleted seqs counted, shown);
  `stream.end` → `close()`; CONNECTING error → "reconnecting" (browser resumes with
  Last-Event-ID); CLOSED → `fetch` probe: 503 `stream_limit_reached` → poll history every 2 s,
  retry SSE every 30 s; 404 → stop; else reconnect with backoff 1–15 s. Job snapshot re-read
  ~150 ms after non-progress events, at most once a second for progress, every 5 s while
  unfinished.
- **Timeline** (`timelineModel.ts`, pure, tested; `Timeline.tsx`): rows = attempts, columns =
  stages from `/v1/meta`; cell states pending/running/done/reused/failed/interrupted/not_run;
  sentences between rows (retry wait, lease expired, manual retry, lost message, cancel
  requested); final outcome row. Container query → list under 720 px.
- **States** are glyph shape + label + color (never color alone); the primary action is ink, not a
  hue. Tokens in `src/styles/tokens.css` (light + full dark set; system preference, theme select
  stored in localStorage, applied before paint by `public/theme.js`).
- **Stale data:** React Query keeps the last data on failure; sections show "Not current: last
  updated ..." and dim; a banner when `/health/ready` is unreachable or reports the database
  down; stale checks never show green/red.
- **New job:** limits from `/v1/meta` (extension, size, empty, filename rules, signature bytes);
  XHR upload with progress and cancel; one `crypto.randomUUID()` key per submission
  (fingerprint: workflow, file name/size/mtime, fault option), reused with the same upload after
  a network error/5xx; `200 + Idempotent-Replayed: true` shown on the page with a link.
- **Jobs list:** filters in the URL (`?workflow=&status=&status=`), `useInfiniteQuery` with
  `next_cursor` ("Load more jobs"), refetch every 5 s.
- **Security:** `SafeText` (`<bdi>` + visible `\uXXXX` for Cc/Cf/Zl/Zp/Cs); summary preview is
  plain `<pre>` text (256 KiB cap); manifest parsed defensively (16 MiB cap); no
  `dangerouslySetInnerHTML` (source-scan test). Production CSP as a build-only `<meta>` tag
  (plus a `frame-ancestors 'none'` header in `vite preview`); `assetsInlineLimit: 0`; Base UI
  `CSPProvider disableStyleElements`. Vite proxies `/v1` and `/health` (the API has no CORS).
- **Compose `frontend`:** Vite on `127.0.0.1:5173` (listens on 0.0.0.0 inside the container),
  `TASKLANE_API_URL=http://api:8000`, polling watcher (`TASKLANE_DEV_POLLING=true`), only
  `src/`, `public/`, `index.html`, `vite.config.ts` mounted, runs as `node`. Locally:
  `npm run dev` (127.0.0.1, proxy to 127.0.0.1:8000).
- **Tests:** Vitest + Testing Library (`npm test`); Playwright (`npm run e2e`) against the real
  stack through `vite preview` of a fresh build (`reuseExistingServer: false`), one worker, fails
  on any console error (CSP violations); needs the demo settings (the global setup checks fault
  injection). `npm run screenshots` regenerates `docs/screenshots/` (48 files: every screen and
  state, light and dark; edge states via route mocking, recovery via a real
  `docker compose kill worker`).

## Phase 10

Full reasoning (alternatives, plain language): `PHASE10_NOTES.md`; reference:
`docs/observability.md`.

- **Packages** (PyPI 2026-11-17): `opentelemetry-api`/`-sdk`/`-exporter-otlp-proto-http` 1.45.2,
  `opentelemetry-exporter-prometheus` 0.66b2 (prometheus-client 0.26.0). **No instrumentation
  packages**: our own server span (`observability/http.py`), DB hooks (`observability/db.py`),
  Celery header. **FastAPI 0.162's built-in telemetry is off** (`FastAPI(telemetry={...False})`):
  it records `url.query` and continues incoming `traceparent`.
- **One trace per job, parent = the span that caused the work** (not links):
  `jobs.traceparent` = `job.create`; `outbox.traceparent` = enqueuing span (`job.create`,
  failed `job.attempt`, `recovery.*`, `job.manual_retry`); dispatcher `outbox.publish` continues
  it and sends header `traceparent`; worker `job.attempt` continues that. Recovery, manual retry
  and cancel continue `jobs.traceparent`; retry/cancel link to the HTTP span. Incoming
  `traceparent` headers ignored. `trace_id` in every job response (`domain/tracing.py`).
- **Spans:** `<METHOD> <route>`, `job.create`, `job.manual_retry`, `job.cancel`, `sse.stream`
  (one per connection, not current; polls run with `suppressed_db_spans()`), `outbox.publish`,
  `job.attempt`, `stage <name>`, `provider.summarize`, `recovery.lease_expired`,
  `recovery.lost_message`, `db <OP>` (only under a recording span; `db.statement` with
  placeholders). `span()` never records exceptions (ERROR status + `error_class`). Health and
  `/metrics` untraced; idle dispatch/recovery passes create no spans; heartbeat renewals no DB
  spans.
- **Span scan:** root conftest installs an in-memory exporter (`tests/telemetry.py`); every
  test's spans are checked against an attribute allowlist + forbidden fragments; e2e processes
  write `TELEMETRY_SPAN_FILE` (JSON lines), scanned at pipeline teardown. A new attribute must be
  added to `ALLOWED_SPAN_ATTRIBUTES` and documented.
- **Metrics split:** job/attempt/stage/provider/outbox/worker metrics are **computed from the
  database at scrape time** (`services/metrics.py`, `db/repositories/metrics.py`, one read-only
  repeatable-read transaction, 5 s statement timeout); only SSE metrics are API-local
  OpenTelemetry instruments (`observability/metrics.py`, Prometheus reader, `target_info` off,
  `otel_scope_*` labels stripped, only `tasklane_*` rendered). Content type
  `text/plain; version=0.0.4`. Labels: `workflow_type`, `status`, `stage`, `error_class`,
  `provider`, `model`, `outcome` only (`ALLOWED_LABELS`, tested).
- **Migration 0005:** `jobs.traceparent`, `outbox.traceparent`, `job_attempts.queued_at` (=
  `retry_at` for retry_wait, else `updated_at`; queue wait = `started_at - queued_at`),
  `worker_heartbeats` (PK `worker_id`, `last_seen_at`, `stopped_at`, counters
  `fenced_attempts`, `heartbeat_failures`, `heartbeat_thread_deaths`).
- **Worker presence:** each Celery child starts `WorkerPresence` at `worker_process_init`
  (every `WORKER_PRESENCE_INTERVAL_SECONDS`=5), marks stopped at `worker_process_shutdown`;
  alive = not stopped and seen within 3 intervals. `GET /v1/overview` `workers {alive,
  last_seen_seconds}`; dashboard "No worker is running". Worker pool size 4. Worker-only counters
  (`services/worker_presence.COUNTERS`) are saved in the row. Telemetry is configured in each
  child (after the fork).
- **Logs:** `ContextFilter` adds `request_id`, `trace_id`, `span_id` and `log_context(...)`
  fields (job_id, attempt_no, worker_id); the heartbeat thread runs in a copy of the attempt's
  context. **Redaction** (`observability/redaction.py`) on message, extras (recursive,
  sensitive keys masked whole) and tracebacks; literal secrets = DB password + env vars with
  sensitive names (8+ chars). OpenTelemetry SDK log lines: one per minute per message.
- **Export:** default no exporter; `OTEL_EXPORTER_OTLP_ENDPOINT` (traces only, OTLP/HTTP,
  batch, `OTEL_EXPORT_TIMEOUT_SECONDS`=2), `TRACE_VIEWER_URL` (`{trace_id}` template, in
  `/v1/meta`), `OTEL_SDK_DISABLED`. Compose profile `observability`: `jaegertracing/jaeger:2.23.0`
  (UI 127.0.0.1:16686, OTLP 4318 internal) + `prom/prometheus:v3.16.0` (127.0.0.1:9090,
  `observability/prometheus.yml`, volume `promdata`).
- **Dashboard:** job detail "Trace id" with copy button and "Open trace" (http(s) only,
  `features/jobs/trace.ts`); Overview "Workers" fact and a notice when none is alive.

## Phase 11

Full reasoning (alternatives, plain language): `PHASE11_NOTES.md`; operator guide:
`docs/providers.md`.

- **httpx 0.28.1** (already locked as a dev dependency) moved to runtime; no SDK (it retries by
  itself and brings ~8 dependencies). `app/providers/openai_compatible.py`:
  `POST {PROVIDER_BASE_URL}/chat/completions`, temperature 0 (constant), `max_tokens` =
  `PROVIDER_MAX_OUTPUT_TOKENS` (1024), no tools/functions/response_format, `follow_redirects=False`,
  1 MiB response cap. **Hard deadline**: the request runs on a daemon thread and the caller waits
  at most `PROVIDER_TIMEOUT_SECONDS` (httpx timeouts are per operation; a trickling server is
  tested). `SummaryResult.model` is the configured model (bounded metric labels).
- **Settings:** `PROVIDER` (`stub` | `openai_compatible`), `PROVIDER_BASE_URL`, `PROVIDER_MODEL`
  (both required for openai_compatible, every service), `PROVIDER_API_KEY` (SecretStr; Compose
  passes it to the **worker only**; `Settings.provider_key_problem()` makes `worker_main` exit
  with `tasklane worker cannot start: ...`), `PROVIDER_MAX_CALLS_PER_MINUTE`, prices
  `PROVIDER_INPUT/OUTPUT_PRICE_PER_MILLION_TOKENS` (Decimal, both or neither;
  `Settings.provider_pricing`). `create_provider(settings)`.
- **Errors:** 408/timeouts → `provider_timeout`; connection errors/5xx → `provider_unavailable`;
  429 → `provider_rate_limited` with `retry_after_seconds` (seconds or HTTP date, ≤ 1 day);
  401/403 → `provider_auth`; other 4xx → `provider_rejected`; malformed/empty/oversized/1xx-3xx →
  `provider_bad_response` (also undecodable bodies; the three new classes permanent, not input errors: manual retry
  allowed). `JobError(retry_after_seconds=)` → `_Failure.retry_after` →
  `RetryPolicy.delay_seconds(n, retry_after)` = max(backoff, min(hint, RETRY_MAX_SECONDS)).
  A `JobError` raised inside `provider.summarize` (stage deadline during a pacing wait) is
  re-raised without a `provider_calls` row. Provider error bodies are never read into messages or logs; logs get `status_code` and a
  token-like `provider_error_code` only.
- **Secret scrubbing:** `observability/redaction.scrub(text) -> (text, counts)` (same patterns as
  log redaction, kinds `known_secret`, `private_key`, `url_credentials`, `authorization`,
  `token`, `assignment`; never truncates). `SummaryCaller` scrubs every text when
  `provider.remote` (new Provider attribute: stub False, openai True), counting chunks only,
  before the cache check. `summary.json`/manifests: `secret_scrubbing {replaced, by_kind}` or
  null (stub).
- **Prompt structure:** fixed system message (never document text); user message = task + block
  `<<<DATA <id>` ... `DATA <id>>>>`, id = 16 random hex per call (redrawn if in the text).
- **Summary cache** (`workflows/common.py`): keyed by SHA-256 of the exact text sent (after
  scrubbing) + configured provider/model + `cache_scope` (OpenAI-compatible: base URL + max output
  tokens; also in the summarize fingerprint) + stage_version + max chars; chunk entries
  `summaries/{chunk_id}.json`, **combine calls cached too** (`summaries/combine-<sha>.json`).
  Entries store prompt/completion/total tokens. `SummaryCaller` no longer takes `chunk_sha256`.
- **summarize stage_version 2** (both workflows): `summary.json` adds `prompt_tokens`,
  `completion_tokens`, `secret_scrubbing`. Manifests: `usage {provider_calls, prompt_tokens,
  completion_tokens, total_tokens, estimated_cost}`, `secret_scrubbing`; schema names stay `/1`.
  `summary.md` gets `LLM_NOTICE` for non-stub providers; the PDF `summary.md` now fences every
  summary (`fenced`/`visible` live in `workflows/pdf/report.py`), sections as `### Page N`.
- **Cost** (`domain/costs.py`): `Pricing`, `estimate_cost` (Decimal, 6 places, USD),
  `cost_record`. Manifest = calls behind the summary (cached included); job detail
  `usage[]` (`db/repositories/provider_calls.usage_by_model`, `JobService(pricing=,
  priced_model=)`) = all calls of all attempts per provider/model, priced only for the
  configured (provider, model).
- **Pacing, not a semaphore:** a worker process never has two calls in flight (prefork + sequential
  map-reduce), so the setting is a rate: `CallPacer` per process; waits go through
  `SummarizeOptions.sleep` = `SummaryCaller._sleep` (1 s slices, `keep_alive()`, stage deadline).
- **Per-attempt artifact folders:** `JobWorkspace.artifact(name, attempt_no)` →
  `artifacts/{job_id}/attempt-{n}/name` (a fenced attempt can't replace a registered file).
- **API:** `GET /v1/meta` → `provider {name, model, remote, host, estimated_cost}`;
  `GET /v1/jobs/{id}` → `usage[] {provider, model, calls, failed_calls, prompt_tokens,
  completion_tokens, total_tokens, estimated_cost (decimal string), estimated_cost_currency}`.
- **Dashboard:** New job `ProviderNotice` (remote: host, model, what is scrubbed, before upload;
  stub: nothing leaves the machine); job detail "Summarization" table (`Usage`); repo manifest
  viewer shows "Secrets replaced before sending".
- **Logging:** worker registers the key literal; httpx/httpcore loggers at WARNING; worker logs
  `document text is sent to a remote summarization provider` (provider, model, host) at ready.
- **Tests:** `tests/fake_provider.py` (stdlib server: scripted status/body/headers/delay/trickle,
  records requests); unit (`test_openai_provider.py`, `test_real_provider_stages.py`,
  `test_provider_settings.py`), integration (`test_real_provider.py`), e2e
  (`test_real_provider_e2e.py`: real worker + fake server; the key is searched in every table,
  stored file, log, span and API response). `live` marker: `tests/live/test_live_provider.py`,
  skipped without `PROVIDER_API_KEY` (`docker compose run --rm -e PROVIDER_API_KEY api pytest -m live`).
