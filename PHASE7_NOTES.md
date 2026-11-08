# Phase 7 notes: leases, heartbeats, recovery, cancellation, manual retry

Each decision: **what** was decided, the **alternatives**, **why**, and a **plain-language**
version. The short form of every decision is in `CLAUDE.md` ("Decisions made (Phase 7)"); the
reliability story end to end is in `docs/reliability.md`.

Node comparison for the whole phase: a *lease* is like a lock in Redis with a TTL (`SET key
NX PX 60000`) that the owner keeps extending; the *recovery loop* is a cron job that looks for
locks whose TTL ran out; *fencing* is checking "do I still hold the lock?" atomically with every
write, so a process that lost the lock while paused can't overwrite the new owner's work.

---

## 1. The heartbeat is a thread inside the task, on its own connection

**Decision.** `app/services/heartbeat.py`: one daemon thread per running attempt, started by
`JobExecutionService.run()` right after the claim commits and stopped (`Event.set()` + `join`) by a
`with` block when the attempt ends, however it ends. Every `HEARTBEAT_INTERVAL_SECONDS` (default
lease / 4 = 15 s) it runs `UPDATE job_attempts SET heartbeat_at = now(), lease_expires_at = now() +
lease WHERE id = :attempt AND ended_at IS NULL` in its own short transaction, on a connection
checked out from the worker process's pool (never the one the stage is using). The worker
process pool grew from 2 to 3 connections (task, heartbeat, spare).

**Alternatives.**
- *Keep renewing only from stage code* (Phase 5's `keep_alive` and progress reports). Rejected:
  every stage must remember to do it, and a slow silent call (a provider call, a bug) lets the
  lease lapse.
- *Celery's own mechanisms* (worker heartbeats, `task_track_started`). Rejected: they live in
  Redis/Celery, not in PostgreSQL (the source of truth), and know nothing about our attempts.
- *A separate heartbeat process* (sidecar). Rejected: it could stay alive while the worker
  process is dead or stuck, which is the opposite of what a heartbeat should prove.
- *One long-lived dedicated connection for the heartbeat.* Rejected: a pooled checkout per beat
  gets `pool_pre_ping` (survives a Postgres restart) and costs nothing at one beat per 15 s.

**Why, and how it is safe in a Celery prefork worker.** Celery forks its worker processes at
startup, before any task runs. The thread is created inside a task, in the already-forked process,
so no thread ever crosses a fork (the classic hazard: a forked child inherits a lock some other
thread held, and deadlocks). The sandboxed parsers are started with `subprocess` (fork + exec in
C), which is safe in a threaded process; the launcher already avoids `preexec_fn` for this reason.
The thread is a daemon, so it can never keep a worker process alive by itself; a SIGKILL or the
Celery hard time limit kills it with the process.

**If the thread itself dies.** Each beat catches its own errors (database briefly down: log, try
again next interval; a lease covers ~3 missed beats). If the thread somehow dies anyway (a bug),
it logs the error and sets `died`. The lease is then renewed only by the stage's own writes;
a long silent stage would have its lease expire, recovery would close the attempt and retry the
job, and the worker would be fenced. So a dead heartbeat can cost an unnecessary retry, never two
owners. Tested (`test_if_the_heartbeat_thread_dies_the_job_still_finishes`).

**Trade-off.** An independent heartbeat keeps a *stuck* stage alive. Every stage already has its
own time limit (parser deadlines, `stage_timeout`), and `CELERY_TASK_TIME_LIMIT_SECONDS` kills the
process as a last resort, after which the lease expires normally.

**Plain language.** While a job runs, a small helper inside the worker says "still here" to the
database every 15 seconds, whatever the job is doing. If the worker dies, the helper dies with
it and the "still here" messages stop; that silence is how the rest of the system learns the
worker is gone.

## 2. Fencing: a flag set by the heartbeat, checked before every write

**Decision.** If a renewal (heartbeat or stage write) matches no open attempt, the worker is
fenced out. The heartbeat sets `fenced` (a `threading.Event`). The execution service checks it at
each stage boundary and before every write (`_renew`, `record`, `record_provider_call`,
`save_artifact`); stage writes still renew the lease in their own transaction (a database-level
check, in addition to the flag). `StageContext.keep_alive()` became a flag check (no database),
so the parser sandbox can call it about once a second (`FENCE_CHECK_SECONDS`, was lease / 4) and
kill its child as soon as it raises. Finishing starts with a conditional close of the attempt,
which fails for a fenced worker, so it never registers artifacts or changes the job.

**Alternatives.** The heartbeat kills the child itself (it would need to know about the
sandbox: couples layers). Checking the database on every `keep_alive` (one query per second per
running job, for no benefit now that the heartbeat writes).

**Why.** One authority (the open attempt row) decides ownership; the flag only makes the worker
notice faster and stop *before* trying to write.

**Latency.** The flag is set by the heartbeat, so a fenced worker notices within one heartbeat
interval (15 s by default) plus up to a second in the sandbox, or at its next database write,
whichever comes first. While it can't reach the database at all, it can't learn that it was
fenced; its current stage runs on (to the parser deadline at most), and every write it then tries
fails. Safe for the same reasons as decision 3.

**Plain language.** If the system decided this worker was dead and gave the job to someone else,
the worker's helper notices on its next "still here" (nobody answers to that name any more). It
raises a flag, the worker sees the flag, stops the PDF/ZIP reader it was waiting on, and goes
quiet without writing anything.

## 3. Files written by a fenced worker (carry-forward: fenced writes to files)

**Decision.** Documented, not changed: a fenced worker may finish one file write that was
already under way under `work/` or `artifacts/`. Safe while outputs are deterministic: the new
attempt writes the same bytes to the same path, every write is atomic (temp file + `os.replace`),
checkpoints are verified by size and SHA-256 before reuse, and artifact *rows* are written only in
the successful attempt's finishing transaction. With the heartbeat flag and the 1 s sandbox check,
a fenced worker stops within one heartbeat interval of being fenced (15 s by default) plus about a second; while the worker can't reach the database at all, it can't learn that it was fenced, so its current stage runs on (to the parser deadline at most), or at its next stage call, whichever comes first. Per-attempt files remain a
Phase 11 requirement (a real LLM is not deterministic).

**Alternatives.** Per-attempt work folders now (more disk, more cleanup, and checkpoint reuse
would need copying across folders) for a risk that can't produce different content today.

**Plain language.** Two workers might both save the same file for a moment, but they would save
exactly the same bytes, so it doesn't matter who saves last.

## 4. Recovery: its own module and thread, one job per transaction

**Decision.** `app/queue/recovery.py` (`Recovery`, `run_forever`, `start_in_thread`), started by
the dispatcher's `main()` in a second thread with its own `threading.Event` (both set by
SIGTERM/SIGINT; each finishes its current pass). Every `RECOVERY_INTERVAL_SECONDS` (5 s) a pass
handles up to 100 expired attempts and 100 lost jobs, each in its own transaction:
`SELECT ... LIMIT 1 FOR UPDATE SKIP LOCKED`, act, commit, repeat. No Celery, no HTTP imports
(boundary test); messages go out through the outbox. The dispatcher's pool grew from 2 to 3.

**Alternatives.**
- *One big transaction per pass.* Rejected: one bad row would roll back all the others, and locks
  would be held for the whole pass.
- *Recovery in the service layer, called by a thin loop.* The dispatcher (also in `queue`)
  already owns its transactions this way; the shared rules come from `domain`
  (`status_after_lease_expired`, `run_attempt_no`, `RetryPolicy`) and the repositories
  (`enqueue_retry`, `transition_job`), so nothing was duplicated except "compute `now() + delay`".
- *Its own process now.* Not needed; `start_in_thread` and the separate stop flag make it a small
  change later (the decision from Phase 0).

**Plain language.** A second loop in the dispatcher wakes every 5 seconds and asks the database
"is any job held by a worker that stopped saying 'still here'?" and "is any job waiting for a
message that will never come?", and fixes each one it finds, one at a time.

## 5. What recovery does with an expired lease (carry-forward: lease-expiry retries)

**Decision.** Close the attempt as `lease_expired` (error class `lease_expired`, a new
*transient* entry in `ERRORS`), append `attempt.lease_expired` (worker id, last heartbeat, expiry
time: no paths, no payload text), lock the job, then `status_after_lease_expired`:
`running` + attempts left → `retry_wait` with `retry_at` and the outbox `available_at` from one
`now()` (`enqueue_retry`), the usual backoff, `retry_in_seconds` in the event; `running` + budget
used → `failed` with "... Gave up after N attempts." (`gave_up_after_attempts` in the event);
`cancel_requested` → `cancelled`. A job that isn't executing (shouldn't happen) only gets its
stray attempt closed. The next attempt's trigger is `recovery` (the claim reads `error_class`).
`ck_jobs_claimable_has_attempt_left` would reject a budget violation.

**Plain language.** If the worker vanished, the job is treated like any other "might work if
tried again" failure: wait a few seconds and try again, up to three tries in total, then give up
with a clear message. If the user had asked to cancel, it just ends cancelled.

## 6. Lock order: attempt, then job (carry-forward: lock order)

**Decision.** Recovery locks the attempt (`FOR UPDATE SKIP LOCKED`), then the job (`append_event`
updates the job row; `lock_job` re-reads it `FOR UPDATE`), the same order as a worker's progress
report, failure, success and cancel-at-boundary. The claim locks the job first but never touches
an existing open attempt; cancel and manual retry lock only the job (then the outbox); the
dispatcher locks only outbox rows. No cycle exists. SKIP LOCKED makes recovery skip an attempt a
worker is renewing at that moment.

**Test.** `test_a_worker_finishing_while_recovery_runs_never_deadlocks` runs a worker's finish
(success, and transient failure) and a recovery pass at the same instant 15 times each: no
deadlock error, and each round ends with exactly one of them in charge (recovery closed it and the
worker was fenced, or the worker finished and recovery found nothing).

**Plain language.** Two people who both need the same two keys always pick them up in the same
order, so neither can end up holding one key while waiting forever for the other.

## 7. Lost messages (carry-forward: lost messages)

**Decision.** A job is "lost" when it is `queued` (or `retry_wait` whose `retry_at` passed more
than the grace period ago), hasn't changed within `LOST_MESSAGE_GRACE_SECONDS` (default 300 s),
has no open attempt, and has no outbox row that is unpublished or was published within the grace
period. Recovery inserts a new outbox row (`enqueue_run_job`) and appends `job.requeued`
(`reason: lost_message`, `status`, `last_published_at`). Covers Redis losing a message, a manual
retry whose message was lost, a worker killed after receiving a message but before claiming it,
and a task failing before its claim committed.

**Alternatives.** Inspect Redis (recovery would need Celery/kombu internals and couldn't see
messages reserved by a worker). Use the visibility timeout alone (900 s, and it doesn't cover
messages Redis truly lost).

**Trade-off.** A job that waits longer than the grace period behind a long queue gets a
duplicate message each grace period; the claim drops them. Documented; raise the grace if queues
are routinely that long.

**Plain language.** If a job has been "waiting to start" for five minutes and nobody is working
on it and no message is on its way, the message probably got lost, so a new one is sent. If the
old one turns up after all, the worker sees the job is already taken and ignores it.

## 8. Recovery is safe twice and concurrently

**Decision.** SKIP LOCKED gives concurrent passes different rows; after a commit the row no
longer matches (attempt closed; job has a fresh pending or recent outbox row). Inside the
lost-message transaction, every change that could make the job stop matching (a claim, a cancel,
a retry, another requeue) locks or updates the same job row, and `enqueue_run_job` is
`ON CONFLICT DO NOTHING` on the one-pending-message index as a last guard. Tests: two loops over
20 expired attempts and over 10 lost jobs, each handled exactly once; `run_once` twice in a row
acts once.

**Plain language.** Two copies of the recovery loop can run at once (or the same one twice): each
job is fixed by exactly one of them.

## 9. Cancellation

**Decision.** `POST /v1/jobs/{id}/cancel` → `JobService.cancel` → `cancel_job(session, id)`: lock
the job, `cancel_decision(status)`:
- `queued` / `retry_wait` → `cancelled`, and `DELETE` the job's unpublished outbox row in the same
  transaction (`outbox_removed` in the event). A message already in Redis is dropped by the claim.
- `running` → `cancel_requested`. The worker reads the status before every stage; on
  `cancel_requested` it closes the attempt as `cancelled` and moves the job to `cancelled`
  (`before_stage` in the event), one transaction, attempt then job.
- otherwise → no change. The route always answers 200 with the job detail as it now is.

`cancel_decision` gained a reason (`already_cancel_requested`, `already_finished`) for the detail
view. Earlier decisions kept: success or a permanent failure before the next boundary wins
(`job.cancel_superseded`); a transient failure or an expired lease during `cancel_requested` ends
`cancelled`.

**Alternatives.** *Neutralise* the outbox row (mark it published or flag it) instead of deleting
it: more states for no benefit, since the claim already ignores cancelled jobs. *202 Accepted* for
`running → cancel_requested`: rejected for one simple rule (always 200, read `status`). *Stopping
mid-stage* (the heartbeat could also read the status): not asked for; a stage boundary is where
the worker's state is consistent.

**Plain language.** Cancelling a job that hasn't started removes its "please run me" note. For a
running job, the worker is told to stop at the next safe point, between two steps.

## 10. Manual retry (carry-forwards: backoff after a manual retry, manual retry and checkpoints)

**Decision.** `POST /v1/jobs/{id}/retry` → `retry_job(session, id)`: lock the job,
`can_manual_retry(...)`; refused → `409 job_not_retryable` with `details.reason` (`not_failed`,
`input_error`, `retry_limit_reached`) and `details.status`; allowed → `failed → queued` with
`max_attempts = attempt_count + 3`, `manual_retry_count + 1` (transition_job clears error,
progress, stage, finish time and cancel timestamp), a `job.manual_retry` event, and a new outbox
row. A second concurrent request waits for the row lock, sees `queued`, and gets `409 not_failed`
(never two queued runs; tested with the blocked-connection helper).

- **Backoff resets:** `run_attempt_no(attempt_no, max_attempts) = attempt_no - (max_attempts - 3)`
  (never below 1) is used by the worker *and* recovery, so the first automatic retry after a manual
  retry waits 2.5–5 s again. It relies on `max_attempts` only ever being 3 or `attempt_count + 3`,
  which `transition_job` and `can_manual_retry` guarantee; no migration needed.
- **Checkpoints: resume.** The manual retry's first attempt resumes from checkpoints that still
  verify (stage version, fingerprint, size and SHA-256 of each file). The input is unchanged, and
  a stage fixed after the failure must bump its `stage_version`, which invalidates its checkpoint.
  Tested: `test_a_manual_retry_resumes_from_verified_checkpoints`.

**Alternatives.** Clear `stage_checkpoints` on manual retry (always start fresh): safer against
an unversioned stage fix, but pays again for every finished stage; verification already catches
damaged files. Store `run_started_at_attempt` in a new column: needs a migration for a value the
budget already encodes.

**Plain language.** "Retry" gives a failed job three more tries on the same job, picks up after
the steps that already finished correctly, and starts the waiting-between-tries clock from the
beginning.

## 11. Job detail: lease and allowed actions computed by the server

**Decision.** `GET /v1/jobs/{id}` (and the cancel/retry responses) add `lease`
(`attempt_no, worker_id, heartbeat_at, lease_expires_at, expired`, `null` without an open attempt;
`expired` judged against the database's `now()` read in the same session), `can_cancel` +
`cancel_blocked_reason`, `can_retry` + `retry_blocked_reason`, all from the state machine
(`cancel_decision`, `can_manual_retry`).

**Plain language.** The page asks the server "can I cancel this? can I retry it?" instead of
guessing from the status, so the buttons can never disagree with what the server will do.

## 12. Orphaned parser processes (carry-forward)

**Decision.** `limited_launcher.py` calls `prctl(PR_SET_PDEATHSIG, SIGKILL)` (via `ctypes`, stdlib)
first thing, so the kernel kills the child the moment its parent dies, then exits with code 1 if
`os.getppid()` is no longer the parent pid it was given (the parent died before the `prctl`). The
sandbox passes `os.getpid()` as a new first argument. Linux only (like the RLIMITs). PDEATHSIG
follows the parent *thread*; `run_sandboxed` waits for its child, so that thread outlives it.

**Alternatives.** The child polls `getppid()` (needs a thread or hooks in the parse loop, and
reacts slower); a subreaper or cgroup per job (heavier, needs privileges). Killing the whole
container already kills everything; the case that matters is one worker *process* killed (OOM)
while the container lives on.

**Tests.** `test_the_parser_dies_when_the_worker_process_is_killed` (a real parent process
started via `run_sandboxed`, killed with SIGKILL; the parser is gone within 5 s instead of
sleeping 120 s), `test_a_parser_whose_worker_died_before_it_started_exits_at_once`.

**Plain language.** The PDF/ZIP reader is told by the operating system "if your parent dies, you
die too", so killing a worker never leaves a reader running on its own.

## 13. Settings and startup validation

`HEARTBEAT_INTERVAL_SECONDS` (unset = lease / 4; at most lease / 3), `RECOVERY_INTERVAL_SECONDS`
(5; at most `LEASE_SECONDS`), `LOST_MESSAGE_GRACE_SECONDS` (300; at least 5 and at least twice
both the recovery interval and the dispatch poll). Existing checks kept: visibility timeout >
lease, provider timeout ≤ lease / 2. `Settings` now uses `env_ignore_empty=True`, because Compose
passes an unset `${HEARTBEAT_INTERVAL_SECONDS:-}` as an empty string. `keep_alive_interval_seconds`
was replaced by `heartbeat_seconds`.

**Plain language.** The app refuses to start with timings that contradict each other, e.g. a
"still here" interval so long that a healthy worker would look dead.

## 14. Review follow-ups

- **One bad row can't stall recovery:** if handling one expired attempt or lost job raises, its
  transaction rolls back, it is skipped for the rest of the pass (`skip=` in the repository
  queries) and retried next pass; everything else is still recovered. Tested.
- **A worker host that vanishes mid-transaction** leaves its row lock in Postgres until the
  server notices the dead connection (TCP keepalive, about 2 hours on Linux); SKIP LOCKED makes
  recovery skip that job meanwhile. Not fixed here (it means a server setting on every
  connection); `idle_in_transaction_session_timeout` is a Phase 12 carry-forward.
- `retry_job` checks its transition applied (like `cancel_job`); the heartbeat doesn't log
  "fenced" for a beat racing its own worker's finish; `HEARTBEAT_INTERVAL_SECONDS` ≥ 0.5.

## 15. Duplicate delivery stays harmless (acceptance #7)

No new mechanism: the claim (job row lock, `queued` or due `retry_wait` only) drops duplicates.
New tests run duplicates against recovery and cancellation: during a running attempt; the dead
worker's message redelivered while the job waits in backoff (`retry_not_due`); two deliveries
racing for a recovered job's due retry (one attempt); recovery after a duplicate already restarted
the job (leaves the live attempt alone); a cancelled job's already-published message. The e2e
kill test adds a duplicate during recovery and checks attempts, artifacts and event numbering.

---

## Carry-forward items from CLAUDE.md

| Item | Resolution |
|---|---|
| Phase 7 — lease-expiry retries | **Resolved** (decision 5): `enqueue_retry` with one `now()`, `RetryPolicy` backoff via `run_attempt_no`, budget via `status_after_lease_expired` and the CHECK. Tests in `test_recovery.py`. |
| Phase 7 — backoff after a manual retry | **Resolved** (decision 10): backoff counts attempts within the current run, in the worker and recovery. `test_backoff_after_a_manual_retry_starts_from_the_first_retry_again`. |
| Phase 7 — manual retry and checkpoints | **Resolved** (decision 10): resume from verified checkpoints. `test_a_manual_retry_resumes_from_verified_checkpoints`. |
| Phase 7/11 — fenced writes to files | **Phase 7 part resolved** (decisions 2-3): fenced workers stop within one heartbeat interval (or at their next write) and never register artifacts; a late deterministic file write is harmless and documented. **Stays open for Phase 11** (non-deterministic provider → per-attempt files). |
| Phase 7 — orphaned parser processes | **Resolved** (decision 12): PDEATHSIG + parent check, tested with a real killed parent. |
| Phase 7 — heartbeats independent of progress updates | **Resolved** (decision 1). |
| Phase 7 — lost messages | **Resolved** (decision 7). |
| Phase 7 — dead workers (killed, hard time limit) | **Resolved** (decision 5); e2e `test_worker_process_killed_mid_stage_is_recovered_and_succeeds`, `test_killing_the_worker_in_every_attempt_uses_up_the_budget`. |
| Phase 7 — lock order | **Resolved** (decision 6), with the repeated-race test. |
| Dev note: restarting the worker leaves jobs `running` | **Resolved**: they are recovered after their lease expires (60 s by default). |

New items for later phases are listed in `CLAUDE.md` ("Carry-forward requirements").
