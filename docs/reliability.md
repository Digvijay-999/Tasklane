# Reliability design

This document explains how Tasklane keeps job state correct when several processes touch the same
job at once, and what each database constraint protects.

> **Status (Phase 4):** implemented and tested: the state machine, the schema and its
> constraints, conditional status changes and per-job events (Phase 2), idempotent job creation
> with the outbox write (Phase 3), and the delivery pipeline: dispatcher, Celery worker, claim,
> duplicate handling, progress with lease renewal and fencing (Phase 4). **Not yet:** retries
> (Phase 5), and lease-expiry recovery, cancellation, and manual retry (Phase 7). See
> [Known gaps](#known-gaps-until-phase-7).

Tasklane does **not** promise "exactly once" execution. Messages may be delivered more than once,
and a worker may die halfway through a job. The goal is that duplicates and interruptions can't
corrupt state: every change is either applied once or detectably rejected.

## Job state machine

Code: `backend/app/domain/state_machine.py` (pure Python, no database or framework imports; a test
enforces that).

| From | To | Trigger |
|---|---|---|
| `queued` | `running` | A worker claims the job |
| `queued` | `cancelled` | User cancels before any worker has it |
| `running` | `succeeded` | All stages finished |
| `running` | `retry_wait` | Transient error, or expired lease, with attempts left |
| `running` | `failed` | Permanent error, or attempts exhausted |
| `running` | `cancel_requested` | User cancels |
| `retry_wait` | `running` | A worker claims the due retry |
| `retry_wait` | `cancelled` | User cancels (no worker involved) |
| `cancel_requested` | `cancelled` | Worker reaches a stage boundary, hits a transient error, or its lease expires |
| `cancel_requested` | `succeeded` | Last stage finished first |
| `cancel_requested` | `failed` | Permanent error happened first |
| `failed` | `queued` | Manual retry |

Every other pair (37 of the 49 possible) is illegal and raises `IllegalTransitionError` before any
SQL runs. Repeating an action, such as cancelling an already-cancelled job, is a no-op rather than a
transition: `cancel_decision()` returns "no change" and the API reports the current state.

### Rules and why

- **Queued or waiting jobs are cancelled directly.** No worker holds them, so there is nothing to
  wait for. If their queue message is still delivered later, the worker's claim (a conditional
  update from `queued`/`retry_wait`) matches nothing and the message is dropped.
- **A definite outcome beats a pending cancel; a retryable one doesn't.** If the job succeeds or
  fails permanently before the worker reaches its next stage boundary, that real outcome is kept
  (`succeeded` / `failed`) and a `job.cancel_superseded` event explains it. Hiding a real error
  behind "cancelled" would make failures harder to debug. A transient error would only lead to a
  retry the user no longer wants, so the job ends `cancelled`.
- **Expired lease** (the worker stopped heartbeating): `retry_wait` if attempts remain, otherwise
  `failed` with error class `lease_expired`; `cancelled` if cancellation had been requested.
- **"Terminal" means no automatic process moves the job again.** `succeeded` and `cancelled` are
  final. `failed` can be reopened only by a user's manual retry, which moves the *same* job back to
  `queued`. The next claim creates a new attempt with the next attempt number; all earlier attempts
  and events are kept.
- **A manual retry starts a clean run.** It clears the finish time, error, stage, progress, and
  any old cancel request (a CHECK forbids a cancel timestamp on an active job, so a leftover
  request can't make the new run cancel itself). A success also clears errors left by earlier
  failed attempts. The history stays in `job_attempts` and `job_events`.

### Manual retry

`can_manual_retry(status, error_class, attempt_count)` returns whether a retry is allowed, why not,
and the new budget:

- Only `failed` jobs can be retried.
- Not offered when the failure was caused by the input (`invalid_input`, `unsupported_input`):
  rerunning would fail the same way.
- A retry grants a fresh budget of 3 automatic attempts: `max_attempts = attempt_count + 3`.
  `transition_job` refuses a `failed -> queued` change that doesn't set `max_attempts`, so a job
  can't be requeued with no attempts left to run.
- The database caps `max_attempts` at 20. A retry that would exceed the cap is refused with reason
  `retry_limit_reached`, so the constraint is never what stops it. A test checks every
  `attempt_count` from 1 to 20 against the real database.

## Conditional updates: how lost races are detected

Code: `transition_job()` in `backend/app/db/repositories/jobs.py`.

A status change is a single statement:

```sql
UPDATE jobs
SET status = :to, updated_at = now(), last_event_seq = last_event_seq + 1, ...
WHERE id = :id AND status = :from
RETURNING last_event_seq
```

Postgres locks the row while updating it. If two processes try the same change, the second waits
for the first to commit, then re-checks its `WHERE` clause against the new row. The status no
longer matches, so it updates zero rows and `RETURNING` gives back nothing. (In MongoDB terms:
`findOneAndUpdate({_id, status: "queued"}, ...)` returning `null`.) If the committed status
already differs when the second process arrives, Postgres skips the row without waiting at all.

This relies on Postgres's default isolation level, READ COMMITTED. (Under REPEATABLE READ or
SERIALIZABLE, the losing update raises a serialization error instead of matching zero rows.)

When nothing comes back, `transition_job` re-reads the latest committed status and returns
`TransitionResult(applied=False, status=<current>)`, or raises `JobNotFoundError` if the job doesn't
exist. That status may change again a moment later, so callers treat it as the basis for a new
decision, never as final. Callers decide what losing means:

- A worker whose claim lost treats its message as a duplicate and drops it.
- An API cancel that lost re-evaluates from the status it now sees (for example, `running` →
  `cancel_requested`).

Integration tests make the race deterministic: connection A updates without committing,
connection B starts the same update in a thread, the test waits until Postgres reports B as
*blocked by A's process* (`pg_blocking_pids`), then A commits. B must lose, and exactly one status
event must exist.

`transition_job` also keeps the status-dependent fields consistent: it sets `finished_at` on
terminal states, sets `started_at` once, records `cancel_requested_at`, requires `retry_at` for
`retry_wait`, `error_summary` for `failed` and `max_attempts` for a manual retry, resets the run
on a manual retry (see above), and only accepts a fixed list of other columns. A test runs every
one of the 12 legal transitions against the real schema.

Because these functions use plain SQL rather than the ORM, they flush pending ORM changes first
(so a just-added job is visible to the `UPDATE`) and mark any cached `Job` object stale afterwards,
on both the applied and the lost path. They never commit; the caller does.

## Per-job event sequence numbers

Each event's number comes from `jobs.last_event_seq`, incremented by an `UPDATE` on the job row.
That `UPDATE` holds the row lock until the transaction commits, so for any one job, numbers become
visible in the same order they were handed out.

**Why not a table-wide auto-increment?** An auto-increment number is assigned when a row is
*inserted*, but other readers see the row only when it is *committed*, and those happen in
different orders:

1. Transaction A inserts an event and gets number 101; transaction B gets 102.
2. B commits first.
3. An SSE client reads up to 102, disconnects, and reconnects with `Last-Event-ID: 102`.
4. A commits event 101.
5. Replay asks for "events after 102", so **101 is never delivered**.

With per-job numbers, event 2 can't even be created until event 1 is committed, so no reader can see
2 without 1. A rollback also undoes the counter increment, so numbers are always 1, 2, 3… with no
gaps, and a client that receives 7 right after 5 knows it missed 6. `Last-Event-ID` is per job
stream anyway, so "event 7 of this job" is the natural meaning.

**Cost:** event writes for one job take turns, which is fine because one worker runs a job at a
time. Different jobs never block each other (tested with a 1-second lock timeout). Transactions that
write events must stay short, since they hold the job's row lock until commit.

**Order by `seq`, never by time.** `created_at` uses Postgres's `now()`, which is the start time of
the *transaction*, so an event can carry an earlier timestamp than the one before it. The SSE
replay in Phase 8 orders by `seq` only.

## Database constraints

Every constraint has an integration test proving it rejects the bad data it targets, identified by
constraint name; two-way ("if and only if") checks are tested in both directions. A meta-test
fails if a constraint exists without a test.

The migrations and the models are checked against each other strictly: a test builds one database
from the migrations and another directly from the models, then compares Postgres's own definition
of every constraint (including CHECK bodies), every index (including partial-index conditions),
and every column (type, nullability, default, identity). Alembic's built-in comparison alone
misses CHECK bodies and partial-index conditions.

| Constraint | Protects |
|---|---|
| `uq_jobs_idempotency_key` | Idempotent submission: two requests with the same key can't both create a job |
| `uq_job_attempts_one_open_per_job` (partial unique index) | At most one live attempt per job, even if a message is delivered twice |
| `pk_job_events` `(job_id, seq)` | Each per-job event number exists exactly once |
| `uq_artifacts_job_id_artifact_type` | A repeated stage replaces its artifact instead of duplicating it |
| `uq_outbox_one_pending_per_job` (partial unique index) | A job can't be queued twice at once by recovery, automatic retry, and manual retry |
| `ck_jobs_attempt_budget` | No more attempts than the budget; budget capped at 20 |
| `ck_jobs_finished_iff_terminal` | A job looks finished exactly when it has a finish time |
| `ck_jobs_failed_has_error` | Every failed job carries a non-empty explanation (uses `coalesce`, because a bare `char_length(NULL) > 0` is NULL and CHECK treats NULL as a pass) |
| `ck_jobs_retry_wait_has_retry_at` | A waiting job always knows when it will run again |
| `ck_jobs_cancel_has_timestamp` | A record of when cancellation was requested |
| `ck_jobs_no_cancel_while_active` | A queued, waiting, or running job never carries a leftover cancel request |
| `ck_job_attempts_ended_iff_outcome` | An attempt is open, or closed with an outcome; never in between |
| `ck_job_attempts_lease_after_start`, `..._heartbeat_after_start` | Lease and heartbeat times are sane |
| `fk_jobs_upload_id_uploads` (RESTRICT) | A job's input record can't be deleted while the job exists |
| `uq_uploads_storage_path`, `uq_artifacts_storage_path` | Two rows never point at the same file |
| `ck_uploads_deleted_consistent` | An upload marked deleted has a deletion time, and vice versa |
| `ck_*_status`, `ck_*_kind`, `ck_*_type`, `ck_*_trigger`, `ck_*_outcome`, `ck_outbox_task_name` | Only known values are stored. The lists are frozen in the migration; a test checks them against the Python enums |
| `ck_*_sha256_format`, `ck_jobs_request_hash_format` | Hashes are 64 lowercase hex characters |
| `ck_*_payload_object`, `ck_jobs_options_object` | JSON columns hold objects, never bare values |
| Other range/length checks | Progress 0–100, positive sizes and sequence numbers, bounded key and filename lengths |

The lease and heartbeat live on `job_attempts`, not `jobs`, so frequent heartbeats never compete
with cancel requests or event writes for the job row. In Phase 7, recovery will close an expired
attempt; if its worker was only slow, its next heartbeat (`... WHERE id = :mine AND ended_at IS
NULL`) matches nothing, telling it to stop without writing anything.

## Delivery: outbox → dispatcher → Redis → worker

```mermaid
sequenceDiagram
    participant API
    participant DB as PostgreSQL
    participant D as Dispatcher
    participant R as Redis
    participant W as Worker
    API->>DB: one transaction: job (queued) + job.created + outbox row
    loop every 1 s
        D->>DB: lock due rows (FOR UPDATE SKIP LOCKED)
        D->>R: publish {"job_id": ...}
        D->>DB: mark published, commit
    end
    R->>W: deliver (possibly more than once)
    W->>DB: claim: queued -> running + attempt + lease (or drop the message)
    loop each progress step
        W->>DB: renew lease (fencing check) + progress + event
    end
    W->>DB: close attempt + succeeded
    W->>R: ack
```

### The dispatcher

One pass is one database transaction: lock up to 50 due rows (`published_at IS NULL AND
available_at <= now()`, oldest first) with `FOR UPDATE SKIP LOCKED`, publish each to Redis while
holding the locks, mark them published, commit. `SKIP LOCKED` makes a second dispatcher skip
locked rows instead of waiting, so two dispatchers never publish the same row (tested with two
dispatchers running at once). It polls every second; LISTEN/NOTIFY isn't used because delayed
retries (`available_at` in the future) need polling anyway and notifications are lost while the
dispatcher is disconnected.

| Crash point | Message in Redis? | Outbox row | What happens |
|---|---|---|---|
| After locking, before publishing | No | Unchanged (rolled back) | Next pass publishes it. Nothing lost. |
| After publishing, before commit | Yes | Still unpublished | Next pass publishes it **again**: a duplicate, which the worker drops (tested). |
| After commit | Yes | Published | Normal. |
| Redis accepted the message, then lost it before saving | No | Published | Job stays `queued`. Redis append-only persistence (flushed every second) makes this unlikely; Phase 7 recovery re-queues such jobs. |

**Redis unavailable:** the failed row and the rest of its batch get `dispatch_attempts + 1`,
`last_dispatch_error` (the error *type*), and `available_at` pushed back by 1 s, 2 s, 4 s, ... up to
60 s, measured from the moment of failure. The loop backs off as well and never gives up; jobs stay
`queued` and visible through the API and go out when Redis returns. The dispatcher's Celery app
doesn't retry connections internally, but a failed publish can still take a few seconds (the
connect timeout doesn't cover DNS, which is slow for a stopped container), during which the pass
holds its row locks. Workers, in contrast, keep reconnecting for as long as Redis is down.

### Celery settings

| Setting | Value | Why |
|---|---|---|
| Serialization | JSON only | Pickle can run code while unpacking; messages carry only `{"job_id": ...}`. |
| Result backend | none | PostgreSQL is the source of truth. |
| `task_acks_late` | on | A message is acknowledged after the task, so a worker dying before it claims the job doesn't lose the message. Safe because the claim drops duplicates. |
| `task_reject_on_worker_lost` | on | A killed worker process puts its message back instead of acknowledging it (tested: the redelivered message is then dropped because the job is already `running`). |
| `worker_prefetch_multiplier` | 1 | Each process reserves one message at a time, so a busy or dying process can't hoard messages. |
| Redis `visibility_timeout` | 900 s | An unacknowledged message is redelivered after 15 minutes. Long jobs see harmless duplicate deliveries; a message whose worker died before claiming waits at most this long. Must exceed the lease (checked at startup). |
| `task_time_limit` | 3600 s | Last-resort kill; the job is left `running` for Phase 7 recovery. |

The **lease** (60 s, in the database) protects work that has been *claimed*; the **visibility
timeout** (in Redis) protects messages that were *received but not yet claimed*. Duplicates from
either are dropped by the claim.

### The claim

One transaction: read the job with `SELECT ... FOR UPDATE` (so a concurrent claimer waits, then
sees `running`); if it is `queued`, or `retry_wait` with `retry_at <= now()`, move it to `running`
with a conditional update (`attempt_count + 1`), insert an attempt row with
`lease_expires_at = now() + 60 s`, and append `attempt.started`. The unique index "one open attempt
per job" backs this up.

**If the database is unavailable** while a task runs, the task asks Celery to deliver the message
again after a backoff (1 s doubling to 60 s, indefinitely) instead of failing. With late acks, a
task that raises is still acknowledged, so failing would lose the message and leave the job
`queued` forever. If the claim had already committed before the error, the redelivered message is
simply dropped as a duplicate.

Every other case means the message is a duplicate or stale, and it is **dropped without writing
anything** (logged as `message dropped` with a reason):

| The job is... | Reason |
|---|---|
| `running` / `cancel_requested` | Another attempt holds it (a duplicate). |
| `succeeded` / `failed` / `cancelled` | Stale, including a job cancelled while queued. |
| `retry_wait` with `retry_at` in the future | An early duplicate: it must not cut the backoff short. |
| Changed between our read and our update | Lost the race to another worker. |
| Missing | Deleted. |

### Progress, leases, and fencing

Each progress report is one short transaction: renew the lease with `UPDATE job_attempts ... WHERE
id = :attempt AND ended_at IS NULL`, update the job's stage and progress, append `stage.progress`.
If the lease renewal matches nothing, the attempt was closed by someone else (Phase 7 recovery
deciding the worker was dead), so the worker is **fenced out**: it stops without writing anything
more. Finishing closes the attempt and transitions the job in one transaction.

### Worker processes and database connections

Celery forks worker processes from a parent. Forked processes must not share database sockets, so
no engine is created at import time or in the parent: each process creates its own on first use,
and if it ever finds state inherited from a parent (a fork after creation), it discards it without
closing the parent's connections (tested with a real `fork`).

### Known gaps until Phase 7

- **Unexpected errors** fail the job permanently (`internal_error`); there are no retries until
  Phase 5.
- **A worker killed mid-stage**, or a task hitting the hard time limit, leaves the job `running`
  with an open attempt whose lease simply lapses. Nothing acts on an expired lease until Phase 7.
  A redelivered message for that job is dropped, because the job is still `running`.
- **A message lost by Redis** after the outbox row was marked published leaves the job `queued`
  until Phase 7 recovery re-queues it. The same happens if a task fails with a non-database error
  before its claim commits (the message is acknowledged); that would be a bug, and it is logged.
- **Leases are renewed only by progress reports.** A stage that runs longer than the lease without
  reporting progress lets it lapse (harmless until Phase 7; independent heartbeats come in
  Phase 7).

## Test isolation

- Integration tests use a separate database named `<POSTGRES_DB>_test`, dropped, recreated, and
  migrated at the start of each run. A guard refuses any name that doesn't end in `_test` or that
  equals `POSTGRES_DB`.
- Every table is emptied before each test. Wrapping each test in a rolled-back transaction isn't
  used, because the race tests need two real connections that see each other's commits.
- The migration round-trip test (upgrade → downgrade → upgrade) uses its own database,
  `<POSTGRES_DB>_migrations_test`.
- Unit tests need no database at all.
- Anything that touches the broker uses Redis **database 15** and the queue `tasklane-test`, emptied
  before and after each test, so a running dev worker (database 0, queue `tasklane`) never sees test
  messages. End-to-end tests start real dispatcher and worker processes against the test database
  and that Redis database. Celery's eager mode is never used.
