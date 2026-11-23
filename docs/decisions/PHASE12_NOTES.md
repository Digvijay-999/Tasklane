# Phase 12 notes: hardening, retention, evidence and polish

Every decision of Phase 12: what was decided, the alternatives, why, and what it means in plain
language. The operator-facing reference is [docs/operations.md](../operations.md); measured
numbers are in [docs/performance.md](../performance.md); the acceptance criteria and their tests
are in [docs/acceptance.md](../acceptance.md).

## 1. Retention and cleanup

### 1.1 One cleanup module, run as a command or as a dispatcher loop

**Decision.** `app/maintenance/cleanup.py` holds the whole cleanup. `python -m
app.maintenance.cleanup [--dry-run]` runs it once; with `CLEANUP_ENABLED=true` the dispatcher
process runs it every `CLEANUP_INTERVAL_SECONDS` in its own thread (like the recovery loop).
Off by default. SQL lives in `db/repositories/retention.py`, file operations in
`storage/local.py`; a boundary test forbids HTTP, Celery, workflow and provider imports.

**Alternatives.** A separate `cleanup` Compose service with its own schedule; Celery beat; a cron
job on the host; a new `services/` class plus a thin CLI.

**Why.** The dispatcher already hosts the background loops and has the database; one more thread
costs one pooled connection (pool raised from 3 to 4). Celery beat would put database
maintenance behind the broker, which is exactly what must keep working when Redis is down. A cron
job doesn't exist on Windows the same way. A separate services class would be a second layer
with no second caller.

**Plain language.** It's one program. You can run it by hand when you want, or tell the
dispatcher (the process that is always running anyway) to run it every hour.

### 1.2 Rows first, files after; leftovers found by "folder whose row is gone"

**Decision.** Each category first changes rows (delete, or mark `deleted`/`rejected`) and
commits. Then one sweep walks `uploads/`, `uploads/.incoming/`, `work/` and `artifacts/` and
deletes every folder whose row no longer keeps it alive. A real run decides from committed rows
only; a dry run passes along the rows it *would* have changed.

**Alternatives.** Delete files inside the transaction (a rollback would leave rows pointing at
missing files); a "files to delete" table written in the same transaction (more schema, and still
needs a sweep for crashes); renaming folders into a trash folder before the commit.

**Why.** If the process crashes after the commit but before the files are gone, nothing points at
the files any more, and the next run's sweep finds them because their row is missing. That makes
the crash case and the normal case the same code. It also means "files deleted only after their
rows are deleted in a committed transaction" holds by construction. New folders can't be mistaken
for leftovers: every upload, work and artifact folder is created only after its row was
committed.

**Plain language.** First we cross the job off the list (and save the list), then we throw away
its files. If we get interrupted in between, next time we notice files that aren't on the list
any more and throw them away then.

### 1.3 Work folders of jobs that still exist: two transactions, the second holding the lock

**Decision.** For a job finished longer ago than `RETENTION_WORK_DAYS`: transaction 1 locks the
job (`SKIP LOCKED`), checks it is still finished, deletes its `stage_checkpoints` rows, commits.
Transaction 2 locks it again, checks again, deletes `work/{job_id}/` while holding the lock,
commits. Step 2 runs only if step 1 deleted the checkpoints.

**Alternatives.** Delete the folder without a lock (a manual retry could start an attempt that
writes into the folder at the same moment, then lose its files); delete it inside transaction 1
(files before the commit); rename the folder to a trash folder under the lock.

**Why.** The checkpoint rows are the database's record of these files, so they go first and are
committed. Holding the job's row lock while deleting means `retry_job()` and the worker's claim
(both lock the job row first) can't start a new attempt in the middle. If the job was retried
between the two transactions, step 2 sees it unfinished and leaves the folder alone; the retry
simply runs every stage again (its checkpoints are gone).

**Plain language.** We tell the database "these saved stage results are gone" first, then delete
them while holding the job's door shut so nobody can restart it at that moment.

### 1.4 Uploads: delete the row when nothing uses it, otherwise mark it deleted

**Decision.** An expired upload no unfinished job uses: if no job references it, the row is
deleted; if finished jobs still do, it is marked `deleted` (the `deleted_at` column and CHECK
constraint existed since Phase 2). Once those jobs are deleted, the next run deletes the row.
Uploads still `receiving` after `RETENTION_INTERRUPTED_UPLOAD_MINUTES` are marked `rejected`
with the existing reason `interrupted`, through the same conditional update the API uses
(`receiving` only), so an upload that finished at that moment stays `ready`.

**Alternatives.** Always keep upload rows as tombstones (they would grow forever); delete upload
rows with `ON DELETE CASCADE` from jobs (the foreign key is `RESTRICT` on purpose: a job must not
silently lose its input record).

**Why.** Finished jobs' pages show the upload's filename; keeping the row until the last job is
gone keeps those pages correct. New jobs can't attach to an expired upload (job creation checks
expiry while holding a `FOR SHARE` lock on the upload row, which our `FOR UPDATE SKIP LOCKED`
respects), so "expired and not used by an unfinished job" stays true.

**Plain language.** An old upload's file is deleted. Its name tag stays while some finished job
still shows that name, and goes once those jobs are gone too.

### 1.5 Upload deadline shorter than the interrupted-upload retention

**Decision.** `UPLOAD_MAX_SECONDS` (1800) must be shorter than
`RETENTION_INTERRUPTED_UPLOAD_MINUTES` × 60 (3600), checked at startup.

**Why.** Otherwise the cleanup could mark an upload "interrupted" while it is still legitimately
streaming in. With the API's own deadline shorter, any upload still `receiving` after an hour is
certainly dead.

### 1.6 SKIP LOCKED, keyset batches, and a dry run on the same code path

**Decision.** Every category selects up to `CLEANUP_BATCH_SIZE` rows ordered by id after the
last id seen (`WHERE id > :after ORDER BY id LIMIT n`), `FOR UPDATE SKIP LOCKED` in a real run,
acts, commits, and repeats until nothing is left. The dry run runs the same queries without the
lock and without acting.

**Alternatives.** `DELETE ... WHERE <condition>` in one statement (unbounded, and can't report
what it deleted per row); `OFFSET` paging (skips rows when earlier ones are deleted); a separate
dry-run implementation (it would drift from the real one).

**Why.** SKIP LOCKED lets two runs (two dispatchers, or the loop plus a manual run) work side by
side: each takes different rows, and a row someone else is busy with waits for the next run
instead of blocking. The keyset makes paging correct both when rows disappear (real run) and
when they don't (dry run). Every row a run handles stops matching its query, so a run always ends.

**Plain language.** Each run takes a small handful of rows at a time, skips any that someone else
is holding, and the "practice run" walks exactly the same path without touching anything.

### 1.7 Path safety

**Decision.** The sweep only considers names that are canonical lowercase UUIDs (exactly how we
generate them) in the four known folders. Every path goes through `LocalStorage.checked()`: it
is resolved, refused if it leaves the storage root, is the root, or resolved through a symlink.
Symlinks found inside a folder are deleted as links, never followed. A refused path is logged,
counted (`errors`, exit code 1), and skipped.

**Why.** The storage root is a shared volume; nothing else in it may ever be touched, even if a
bug or a person put something there. The full-disk snapshot test plants unknown names, a
non-canonical UUID and a symlink to a folder outside the root, and checks that exactly the
targeted paths disappear.

### 1.8 Retention defaults

| Setting | Default | Why this value |
|---|---|---|
| `RETENTION_FINISHED_JOB_DAYS` | 30 | Long enough to investigate a failure and retry it; it is also how long an idempotency key stays taken. |
| `RETENTION_WORK_DAYS` | 7 | `files.bin` can be ~500 MiB per repository job; a week covers "retry it tomorrow". |
| `RETENTION_INTERRUPTED_UPLOAD_MINUTES` | 60 | Twice the upload deadline. |
| `RETENTION_PUBLISHED_OUTBOX_HOURS` | 24 | Must cover `LOST_MESSAGE_GRACE_SECONDS` (validated): recovery decides a message is lost by looking at recently published rows. |
| `RETENTION_WORKER_HEARTBEAT_DAYS` | 7 | One row per worker process ever started; a week is enough for the dashboard's "last seen". |
| `CLEANUP_ENABLED` | false | Deleting data should be a choice. |

Deleting a live worker's row (with a retention set absurdly short) is harmless: the worker
re-creates it from memory on its next presence report.

### 1.9 Idempotency keys and metrics

Keys live as long as their job row (decided in Phase 2); the cleanup is what finally frees them:
30 days after the job finished, by default. Documented in docs/api.md and docs/operations.md.

`/metrics` counters are totals over existing rows (Phase 10), so deleting jobs lowers them.
Prometheus treats a lower value as a counter reset, which `rate()` and `increase()` handle.
Documented in docs/observability.md. Events are only deleted with their whole finished job, so
the event stream never meets a gap caused by retention (carry-forward item "event retention").

## 2. Hardening

### 2.1 idle_in_transaction_session_timeout on every connection

**Decision.** `create_db_engine` passes `options=-c idle_in_transaction_session_timeout=<ms>` to
every connection (`DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS`, default 60). A worker that finds its
session ended this way (SQLSTATE `25P03`, raised as `InternalError`, not `OperationalError`)
classifies it as the transient `database_unavailable`.

**Alternatives.** Set it server-wide in `compose.yaml` (CI's service containers can't take
server flags, and it would not travel with the code to another database); server-side
`tcp_keepalives_*` (only helps when the network is gone, not when the client is alive but stuck);
`statement_timeout` (doesn't apply to an idle session); `lock_timeout` on the waiting side (the
waiting side is recovery, which already uses SKIP LOCKED and doesn't wait).

**Why.** The problem was a worker machine that disappears while its transaction holds a row lock:
the server keeps the lock until TCP gives up (~2 h). From the server's side that session is
"idle in transaction", which is exactly what this timeout ends. Every transaction in the code base
is short (the longest idle gap is a dispatcher batch being published to Redis), so 60 s is far
from normal behavior and far below two hours. Without the `25P03` classification, a worker
whose session was ended would have failed its job permanently as `internal_error`.

**Test.** `test_a_vanished_worker_cannot_block_recovery_for_long`: a connection with a 1 s
timeout locks an expired attempt's row and goes silent; recovery skips the row, then recovers it
once PostgreSQL ends the session; the dead connection's error is classified as transient.

**Plain language.** If a computer running a job vanishes mid-conversation with the database, the
database now hangs up on it after a minute instead of two hours, so the job can be rescued.

### 2.2 ZIP entry count from the end-of-central-directory record

**Decision.** Before `zipfile` reads the central directory, the ZIP child reads the entry count
from the end-of-central-directory record (the Zip64 one when present), using `zipfile`'s own
`_EndRecData`. A count over `ZIP_MAX_ENTRIES` fails with `zip_too_many_files` at once. The real
count is still checked after the directory is read.

**Alternatives.** Our own record parser (~25 lines duplicating stdlib logic, including the Zip64
locator and archive comments); bounding the central directory's byte size instead of its count
(a less clear error).

**Why.** `_EndRecData` is private but has been stable for many Python versions; the interpreter
is pinned in the Docker image, and a test with a real 70,000-entry Zip64 archive (and one patched
to claim five million) would catch a change. A record that understates the count falls back to
the old behavior (real count, or the sandbox's memory/time limit: `zip_too_complex`), tested.

**Plain language.** A ZIP says at its very end how many files it contains. We read that number
first, so an archive claiming millions of files is refused with a clear message instead of
running out of memory while being read.

### 2.3 Long queues: ask Redis before calling a message lost

**Decision.** Before the lost-message check, recovery asks Redis how many messages are waiting
(`LLEN` of the Celery queue key, passed in as a `queue_depth` callable from the dispatcher; the
recovery module still imports no Celery). While any are waiting, the grace period is
`LONG_QUEUE_GRACE_FACTOR` (4) times longer. If Redis can't be asked, the normal grace applies.
(The first version skipped the check entirely while the queue was non-empty; the review below
found that a queue that never drains would then hide a lost message forever.)

**Alternatives.** Raise `LOST_MESSAGE_GRACE_SECONDS` (slower recovery of real losses); a
per-job growing grace (more state); inspecting the queue's message bodies (expensive, and couples
recovery to Celery's message format).

**Why.** A message can only be "lost" if it isn't in the queue. When the queue is empty, every
waiting job's message has been delivered or lost, so the old check is exact. When it isn't, the
job may simply be waiting its turn, so it gets more time, but not unlimited time. A test confirms `LLEN <queue>` counts what the dispatcher
publishes (kombu's key for default-priority messages is the queue name).

**Plain language.** Before deciding a job's message got lost, recovery checks whether the queue
still has messages in it. If it does, the job is probably just waiting in line, so recovery
gives it four times longer before sending a new message.

### 2.4 Upload idle and total timeouts (found by the review)

**Decision.** `UploadService` wraps the body stream: each chunk must arrive within
`UPLOAD_IDLE_TIMEOUT_SECONDS` (30) and all of them within `UPLOAD_MAX_SECONDS` (1800). Otherwise
`408 upload_timeout`, the upload is marked `rejected` (`interrupted`, an existing reason, so no
migration) and its files are deleted.

**Why.** uvicorn has no request-body timeout. A client that stalled or trickled one byte at a time
held its request, a `.part` file and a `receiving` row open forever. The idle timeout catches the
stall; the total deadline catches the trickle.

### 2.5 The review of limits, timeouts and error paths

Checked, with the result:

| Area | Finding |
|---|---|
| Upload body: stalled or slow client | **Gap, fixed** (2.4) |
| Upload size, signature, empty file, disconnect, cancellation | OK: enforced on bytes received; files cleaned up on every path |
| JSON body size (incl. chunked bodies without Content-Length) | OK: middleware counts received bytes |
| Request line and headers | OK: uvicorn/h11 limit of 16 KiB |
| Query parameters: page size, cursor, `after`, idempotency key, filename | OK: validated (cursor bounded by the URL limit) |
| SSE: stream count, lifetime, keepalive, unread clients | OK (Phase 8) |
| Database: connect, pool wait, keepalives, scrape statement timeout | OK; **idle-in-transaction added** (2.1) |
| Lock waits in the API (`cancel`/`retry` lock the job row without SKIP LOCKED) | Could wait on a vanished worker's lock; now bounded by 2.1 |
| Redis: socket timeouts, publish timeout, worker reconnect | OK (Phases 1, 4) |
| Parser sandboxes: time, memory, CPU, output size, orphan kill | OK (Phases 5–7) |
| ZIP entry count for huge directories | **Gap, fixed** (2.2) |
| Provider: per-call deadline, response cap, Retry-After cap | OK (Phase 11) |
| Every stage has its own time limit | OK (carry-forward rule; Celery hard limit validated to exceed their sum) |
| Worker error classification | **Gap, fixed**: `25P03` was permanent `internal_error` (2.1) |

### 2.6 Dependency audit

`pip-audit` 2.10.1 over `uv export --frozen --all-groups` (every locked package, runtime and dev,
installed nowhere: `--no-deps --disable-pip` in a throwaway container) and `npm audit` over the
frontend lock file (including the new `@axe-core/playwright`): **no known vulnerabilities** in
either on 2026-11-23. Nothing to fix; the commands are in docs/operations.md.

## 3. Accessibility

**Decision.** `frontend/e2e/accessibility.spec.ts` runs axe-core (`@axe-core/playwright` 4.14.0,
released 2026-10-15, exact version, dev dependency) on 25 states of every screen (overview with
data, empty, loading, API unreachable; jobs list, filtered, no match; new job, validation error,
uploading, idempotent replay; job detail succeeded, failed, retried, cancelled, repository
manifest, running, cancel dialog, reconnecting, stream-limit polling; job not found; page not
found; narrow and phone widths) in both themes: 50 audits. Rules: WCAG 2.0, 2.1 and 2.2 at A and
AA, plus axe's best practices. It is part of `npm run e2e`, so a new violation fails the suite.
Assertions are soft, so one run lists every violating state.

**Violations found and fixed.**
1. `color-contrast` (serious), "API unreachable" state, both themes: stale sections were dimmed
   with `opacity: 0.55`, which pushed all their text under the 4.5:1 minimum. Now stale sections
   keep full contrast and get a dashed rule on the left plus the existing "Not current" note
   (DESIGN.md updated).
2. `scrollable-region-focusable` (serious), phone width: the attempts, usage and event tables
   scroll sideways but keyboard users couldn't reach them. A `TableScroll` component makes each
   wrapper a named, focusable region (`role="region"`, `aria-label`, `tabIndex=0`).

**Also.** A `medium.pdf` sample (40 pages, ~40 s in summarize with the demo delay) replaces
`large.pdf` for the audit's running-job states: a cancelled `large.pdf` job keeps a worker busy
for ~8 minutes until its summarize stage ends, which starved the next test run.

**Plain language.** A robot checks every screen, light and dark, for problems that make the app
hard to use with a screen reader, a keyboard, or weak eyesight. It found two kinds; both are
fixed, and the robot now runs with the other browser tests.

## 4. Evidence

### 4.1 Benchmark

**Decision.** `devtools/benchmark.py` (run in the api container) submits N PDF and N repository
jobs through the real API (one upload per kind, jobs alternating, as fast as it can), waits, and
reads from the database: queue wait = first attempt `started_at - queued_at` (the same definition
as the `tasklane_queue_wait_seconds` metric), workflow duration = `finished_at -` first attempt
`started_at`, end to end = `finished_at - created_at`; median, p95 (nearest rank) and max per
workflow. It refuses to run unless the provider is the stub. It reports the machine as the
containers see it (CPU model, CPUs, memory, kernel) and the settings that matter, and times
`GET /metrics` with all those rows present.

**Why from the database.** The timestamps are written by the database clock in the same
transactions that change the job, so they are what actually happened, not what a client
observed. Host details the containers can't see (Windows version, Docker Desktop version) are
added to docs/performance.md from `docker version` / `docker info`.

**Results** are in docs/performance.md, with the exact command, inputs and settings, labelled as
local measurements.

### 4.2 Partial index on job_events(event_type)

The carry-forward item asked to measure the `/metrics` scrape with many jobs and add a partial
index if needed. Measured: 48.7 ms median, 109 ms max with 411 jobs and 16,797 events
(docs/performance.md). **No index added**: far below the 5 s statement timeout and the 15 s
scrape interval, and the 30-day retention bounds the tables. Re-measure with much more data.

### 4.3 Recovery demo as a PowerShell script

**Decision.** `scripts/demo-recovery.ps1` (PowerShell 5.1 compatible): restarts the worker with
`STUB_DELAY_SECONDS=1`, submits a 40-page PDF, waits until summarize is half done, runs
`docker compose kill worker`, starts a new worker, waits for the job to finish, prints the
attempts and `devtools.job_events`, and restores the worker's settings in a `finally` block.

**Alternatives.** A Python devtool inside the api container (it can't kill another container:
no Docker socket inside, on purpose); shortening the lease for the demo (would need three
settings changed and restored; the default 60 s lease shows the real behavior).

### 4.4 Acceptance criteria

docs/acceptance.md maps each of the brief's ten criteria to the tests that prove it (file and test
names) and to manual steps on the API or dashboard.

## 5. Presentation and repository

- **README** rewritten for a reviewer: two-sentence summary, screenshot, Mermaid architecture
  diagram, the reliability ideas in plain language, a quickstart, tests, links, known
  limitations. Mermaid renders on GitHub without an image file.
- **Docs moved:** `PHASE*_NOTES.md` → `docs/decisions/`, `WALKTHROUGH_*.md` → `docs/walkthroughs/`
  (plain moves, not `git mv`, so nothing is staged; Git detects the renames). Every Markdown link
  was checked by a script; code comments now name the new paths.
- **Removed:** `existing_job_ids` (unused, written during this phase) and an empty stray
  `frontend/backend/` folder. A scan for functions and exports referenced only once found nothing
  else unused (route handlers, validators and signal handlers are used through decorators).
- **CLAUDE.md** is now a maintainer guide: working rules, module boundaries, the design rules that
  every change must keep, commands, and pointers to the decision notes, instead of a phase log.

### 5.1 CI

**Decision.** `.github/workflows/ci.yml`, on push and pull request: backend job on
`ubuntu-24.04` with `postgres:17.11-alpine` and `redis:7.4.12-alpine` service containers (the same
images as Compose), Python 3.13.18, `uv sync --frozen`, ruff check and format check, `pytest -m
"not e2e"`; frontend job with Node 24.21.0, `npm ci`, typecheck, Vitest. Actions pinned to full
commit SHAs (checkout v7.0.1, setup-python v7.1.0, setup-node v7.1.0) with the tag in a comment;
uv pinned to 0.13.6. `permissions: contents: read`.

**Not in CI:** the backend `e2e` tests (real worker and dispatcher processes, minutes of
runtime) and the Playwright suites including the accessibility audit (they need the whole
Compose stack with demo settings). They run locally; the workflow file says so.

**Verified how.** GitHub Actions itself can't run here, so the backend job's steps were
reproduced in a fresh `python:3.13.18-slim` container as a non-root user against the Compose
Postgres and Redis (it caught four lines the doc-path rewrite had made too long). The frontend
steps are the same commands run locally.

## 5.2 Code review findings (and what was done)

A read-only review of the Phase 12 changes (the `code-reviewer` agent) found:

| Finding | Action |
|---|---|
| The cleanup could delete the upload of a failed job that a manual retry reopened between the cleanup's select and its delete (the job then fails `input_unavailable`) | Fixed: before deleting or marking an upload, the cleanup share-locks every job using it and re-checks that all are finished (`lock_jobs_of_upload`); a retry either committed first (upload kept) or waits for the cleanup's commit. Test `test_an_upload_reopened_by_a_manual_retry_after_selection_is_kept` |
| Skipping the lost-message check while the queue is non-empty could hide a lost message forever under constant load | Fixed: a 4× longer grace instead of a skip (2.3). Test `test_a_queue_that_never_drains_does_not_hide_a_lost_message_forever` |
| One folder whose deletion fails (or takes longer than the idle-transaction timeout) aborted the whole run | Fixed: each job folder is handled through `_safely` (logged, counted, skipped) |
| A Zip64 locator claiming several disks crashed the ZIP child (`internal_error`) instead of `zip_corrupt` | Fixed (any error reading the end record falls through to `open_archive`). Test `test_a_zip64_locator_naming_several_disks_is_corrupt_not_a_crash` |
| The Celery task layer didn't treat SQLSTATE `25P03` as a database outage (message acked instead of redelivered) | Fixed: one `database_unavailable()` check in `services/execution.py`, used by `_classify` and `queue/tasks.py`. The API still answers `500` for it: API transactions never sit idle, so it can't happen there in practice |
| The work-retention decision was made from an unlocked read | Fixed: re-checked under the lock in both transactions |
| A dry run and a real run can count rows differently when an interrupted upload is also past its TTL (the real run marks it rejected, then deletes the row in the same run) | Kept, documented: the files are the same; only the row category differs |
| Retry is still offered for a failed job whose upload was already deleted (it then fails at once with `input_unavailable`) | Kept, documented in the README's Known limitations: refusing it would need the upload's status in the state machine's retry rule |

## 6. Carry-forward items: how each was resolved

| Item (from CLAUDE.md) | Resolution |
|---|---|
| Every stage has its own time limit; providers ≤ lease/2; checkpoint rules | Kept as permanent rules in CLAUDE.md ("Design rules"); no new stage was added |
| e2e isolation | Kept as a rule; `test_worker_and_dispatcher_use_the_test_storage_root_never_the_real_volume` still passes |
| Repo work folders are large | Cleanup deletes `work/` after `RETENTION_WORK_DAYS` (1.3). The extra extraction cap near the call limit: **left out** (README "Known limitations": `filter_files` already stops early, and a byte cap before filtering would misfire on files the filter skips) |
| Huge central directories | Fixed (2.2) |
| Interrupted uploads; never delete files of uploads used by unfinished jobs | Fixed (1.4, tested in `test_unfinished_jobs_are_never_touched`) |
| Work folders and stray `.tmp` files | Fixed (1.2, 1.3) |
| Vanished worker hosts | Fixed (2.1) |
| Long queues | Fixed (2.3); default grace kept at 300 s |
| Event retention | Events are deleted only with their whole finished job (1.9) |
| Dashboard rules | Kept as rules; the accessibility fixes add no upload-derived text and keep the CSP (no inline styles or scripts) |
| Telemetry rules | Kept; the cleanup creates no spans and no metric labels |
| Retention vs metrics; old worker rows; scrape cost | Documented (1.9); worker rows deleted (1.8); scrape measured (4.2) |
| e2e data in the development database | Cleanup with `RETENTION_FINISHED_JOB_DAYS=0` removes them (docs/operations.md). A separate demo database: **left out** (README "Known limitations") |
| Artifact folders per attempt; pre-Phase 11 layout | Fixed: unreferenced files under `artifacts/{job_id}/` of finished jobs are deleted |
| Summary cache files | Deleted with `work/` |
| Provider rules | Kept as rules in CLAUDE.md |
| Live provider measurements | **Left out**: needs a paid key and sends data to a third party; README "Known limitations" |
| Worker restart interrupts running jobs (dev note) | Kept as a note in CLAUDE.md and the README |
