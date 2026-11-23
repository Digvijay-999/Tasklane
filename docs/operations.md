# Operations: retention, cleanup, limits, demo and benchmark

How to keep a running Tasklane's disk and database in check, which time and size limits protect
it, and how to run the failure demo and the benchmark. Decisions and alternatives:
[decisions/PHASE12_NOTES.md](decisions/PHASE12_NOTES.md).

## Retention and cleanup

Nothing is deleted automatically unless you turn it on. The cleanup command deletes what is past
its retention period, and never anything an unfinished job uses.

```powershell
docker compose run --rm api python -m app.maintenance.cleanup --dry-run   # what WOULD be deleted
docker compose run --rm api python -m app.maintenance.cleanup             # delete it
```

The dry run prints every row id and every file or folder (with its size) it would remove and
changes nothing. (One small difference: an upload left `receiving` that is also past its expiry
is counted as "interrupted" by a dry run, and as both "interrupted" and "expired" by a real run,
which marks it rejected and then deletes its row. The files are the same.) A real run prints the same list for what it did remove. Exit code 1 means some
path could not be handled (the reason is in the log above the report); everything else was done.

To run it on a schedule, set `CLEANUP_ENABLED=true` (shell variable or `.env`) and restart the
dispatcher: it then runs the cleanup every `CLEANUP_INTERVAL_SECONDS` in its own thread, next to
the dispatch and recovery loops, and logs `cleanup finished` with counts when it removed anything.

### What it removes

| What | When | Settings (default) |
|---|---|---|
| Finished jobs (succeeded, failed, cancelled) with their events, attempts, artifacts, provider calls, checkpoints and outbox rows, and their `work/` and `artifacts/` folders | `finished_at` older than the retention | `RETENTION_FINISHED_JOB_DAYS` (30) |
| Intermediate files of finished jobs (`work/{job_id}/`, including a repository job's `files.bin`, up to ~500 MiB each) and their checkpoint rows. The job, its events and artifacts stay. | finished longer ago than the retention | `RETENTION_WORK_DAYS` (7) |
| Uploads left `receiving` by a crash or a killed request: marked `rejected` (`interrupted`), files deleted | created longer ago than the retention | `RETENTION_INTERRUPTED_UPLOAD_MINUTES` (60) |
| Expired uploads no unfinished job uses: the row is deleted, or marked `deleted` while finished jobs still point at it (their pages show its filename); the file is deleted | `expires_at` passed | `UPLOAD_TTL_HOURS` (24) |
| Published outbox rows (the record of messages sent to Redis) | published longer ago than the retention | `RETENTION_PUBLISHED_OUTBOX_HOURS` (24) |
| `worker_heartbeats` rows of worker processes that stopped or died | not seen for the retention | `RETENTION_WORKER_HEARTBEAT_DAYS` (7) |
| Leftovers: stray `.*.tmp` files of finished jobs (a crash mid-write), files under `artifacts/{job_id}/` that no artifact row references (the folder of an attempt whose `write_artifacts` failed, the pre-Phase 11 layout), and any upload/work/artifact folder whose row no longer exists | always | |

Other settings: `CLEANUP_BATCH_SIZE` (100 rows per transaction), `CLEANUP_INTERVAL_SECONDS`
(3600). `RETENTION_PUBLISHED_OUTBOX_HOURS` must cover `LOST_MESSAGE_GRACE_SECONDS`, and
`UPLOAD_MAX_SECONDS` must be shorter than `RETENTION_INTERRUPTED_UPLOAD_MINUTES` (both checked at
startup).

To clear out **every** finished job now (for example the jobs the Playwright suite and the
benchmark created in your development database), override the retention for one run:

```powershell
docker compose run --rm -e RETENTION_FINISHED_JOB_DAYS=0 api python -m app.maintenance.cleanup --dry-run
docker compose run --rm -e RETENTION_FINISHED_JOB_DAYS=0 api python -m app.maintenance.cleanup
```

### Why it is safe

- **Unfinished jobs are never touched.** A job that is `queued`, `running`, `retry_wait` or
  `cancel_requested` matches no query; its folders are skipped; its upload never counts as
  unused, however old (re-checked under a lock on the jobs right before an upload is deleted, so
  a manual retry at that moment keeps it). Tested for every status with every retention set to
  zero.
- **Rows first, files after.** A file is deleted only once the rows that point at it are
  deleted or marked in a committed transaction. A crash in between leaves files without rows;
  the next run finds folders whose row is gone and removes them. The work folder of a job that
  still exists is deleted while its row is locked (after its checkpoint rows were deleted and
  committed), so a manual retry can't start an attempt that writes into it at the same moment.
- **Twice or at the same time is fine.** Rows are locked `FOR UPDATE SKIP LOCKED`, so two runs
  take different rows, and a row someone else is busy with (a job being retried, an upload a
  job is being created from) waits for the next run. Every row a run handles stops matching.
  Deleting a file that is already gone is not an error.
- **Bounded.** One transaction per `CLEANUP_BATCH_SIZE` rows, or per job for folders.
- **Only our own paths.** Only canonical UUID names in `uploads/`, `uploads/.incoming/`, `work/`
  and `artifacts/` are considered. Every path is resolved against the storage root and refused
  if it leaves it or goes through a symlink (a refused path is logged and counted, never
  followed). A test snapshots the whole temporary folder around the storage root and checks that
  exactly the expected paths disappear.

### Side effects to know

- **Idempotency keys** live as long as their job row: once the cleanup deletes a job, its key
  can be used again for a new job (default: 30 days after the job finished).
- **Metrics:** job, attempt, event, provider-call and worker counters in `GET /metrics` are
  totals over the rows that exist. Deleting rows lowers them, which Prometheus treats as a
  counter reset (`rate()` and `increase()` handle it). Histograms (queue wait, runtime, stage
  duration) lose the deleted jobs' observations the same way.
- **Manual retry after the work retention** reruns every stage (the checkpoints are gone). After
  the upload expired and was deleted, it fails at once with `input_unavailable` ("upload it
  again").
- **A job's page after its upload is deleted** still shows the original filename (the row stays,
  marked `deleted`, until the last job using it is deleted).

## Time and size limits

Every limit, where it is enforced, and what a client sees when it is hit. Phase 12 reviewed each
one; the two gaps it found are marked **new**.

| Limit | Default | Enforced in | When exceeded |
|---|---|---|---|
| PDF / ZIP upload size | 50 / 100 MiB | API, on bytes received while streaming | `413 upload_too_large` |
| **Upload idle time** (no bytes arriving) | 30 s (`UPLOAD_IDLE_TIMEOUT_SECONDS`) | API | `408 upload_timeout`, upload `rejected` (`interrupted`), files deleted |
| **Upload total time** | 1800 s (`UPLOAD_MAX_SECONDS`) | API | same |
| JSON request body | 64 KiB | API middleware (also for chunked bodies) | `413 request_too_large` |
| Request line and headers | 16 KiB | uvicorn (h11) | connection closed |
| Page size / cursor | 100 / bounded by the URL limit | API | `422` / `400 invalid_cursor` |
| Open event streams per API process / stream lifetime / unread client | 50 / 3600 s / 30 s | API | `503 stream_limit_reached` / clean close with last id / disconnect |
| Database connect / pool wait | 2 s / 10 s | every process | `503` (API) or transient `database_unavailable` (worker) |
| **Idle in transaction** | 60 s (`DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS`) | PostgreSQL, per session | session ended, locks released; a worker sees transient `database_unavailable` |
| Lease / heartbeat | 60 s / 15 s | worker, recovery | recovery retries or fails the job |
| PDF parser: validate / extract / memory / output | 60 s / 300 s / 1 GiB / 64 MiB | sandboxed child | `pdf_too_complex` |
| ZIP reader: validate / extract / memory | 60 s / 300 s / 512 MiB | sandboxed child | `zip_too_complex` |
| **ZIP entry count** | 5000 | ZIP child, from the end-of-central-directory record before the directory is read (then again on the real count) | `zip_too_many_files` (even for millions of entries) |
| ZIP expanded size / per file / ratio | 500 MiB / 5 MiB / 100:1 | ZIP child, on real bytes | `zip_too_large` / skipped `too_large` / `zip_bomb` |
| Provider call / summarize stage / calls per repository | 20 s / 900 s / 1000 | worker | transient `provider_timeout` / `stage_timeout` / `repo_too_large` |
| Celery hard limit | 3600 s | Celery | process killed, recovered via the lease |
| Metrics scrape | 5 s statement timeout | API | `503` |

## Dependency audit

Last run 2026-11-23: no known vulnerabilities (details in [security.md](security.md)). To repeat
it (the Python audit runs in a throwaway container and installs nothing on your machine):

```powershell
docker run --rm -v "${PWD}\backend:/src:ro" -w /src python:3.13.18-slim sh -c "pip install -q uv==0.13.6 pip-audit && uv export --frozen --no-hashes --all-groups --no-emit-project -q > /tmp/req.txt && pip-audit -r /tmp/req.txt --disable-pip --no-deps"
cd frontend; npm audit
```

## Failure and recovery demo

A script that shows Tasklane surviving a worker crash, start to finish (about two minutes):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\demo-recovery.ps1
```

It restarts the worker with `STUB_DELAY_SECONDS=1` (so the job runs long enough), generates a
40-page PDF, submits a `pdf_ingest` job, waits until the job is halfway through `summarize`,
**kills the worker container** (`docker compose kill worker`: SIGKILL, no clean shutdown),
starts a new one, waits until the job finishes, and prints the job's stored events. You will see
attempt 1 end in `attempt.lease_expired` once its 60-second lease runs out, the job go to
`retry_wait` and back to `running`, attempt 2 skip the stages whose checkpoints still verify
(`stage.skipped ... checkpoint_reused`), and the job end `succeeded`. Finally it restarts the
worker with your own settings. The dashboard shows the same story on the job's page.

## Benchmark

```powershell
docker compose exec -T api python -m devtools.benchmark --jobs 20
```

Submits N PDF and N repository jobs through the API with the stub provider, waits for all of
them, and prints queue wait, workflow duration and end-to-end time (median, p95, max) from the
database, the machine as the containers see it, the settings that matter, and how long
`GET /metrics` takes with those jobs in the database. Results measured on the development
machine: [performance.md](performance.md).
