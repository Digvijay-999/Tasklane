# Acceptance criteria: where each one is proven

The ten acceptance criteria of [PROJECT_BRIEF.md](../PROJECT_BRIEF.md) (section 9), the
automated tests that prove each one, and how to see it yourself. Backend test files are under
`backend/tests/`; run one with `docker compose run --rm api pytest <file>::<test>`. Playwright
tests are in `frontend/e2e/` (`npm run e2e`, needs the demo settings: README.md, "Tests").

The PowerShell snippets assume `$api = "http://127.0.0.1:8000"`, a running stack, and the sample
files from `docker compose run --rm -v "${env:USERPROFILE}\tasklane-demo:/out" api python -m devtools.pdf_samples /out`
(and `devtools.zip_samples`), as in the README's "Try the API".

## 1. Same idempotency key and request → one job; different request → 409

**Tests**
- `integration/test_jobs_api.py`: `test_replay_returns_200_with_the_same_job_and_writes_nothing_new`,
  `test_replay_matches_regardless_of_key_order_whitespace_and_uuid_case`,
  `test_same_key_with_a_different_request_is_409`, `test_missing_key_is_400`,
  `test_malformed_key_is_400`, `test_a_rejected_request_does_not_use_up_the_key`
- `integration/test_job_idempotency_race.py`: `test_burst_of_identical_requests_creates_exactly_one_job`,
  `test_concurrent_different_request_with_the_same_key_gets_409`
- `unit/test_idempotency.py` (request normalization and hashing)

**See it:** README "Try the API": send the same `POST /v1/jobs` twice with one key (`201`, then
`200` with `Idempotent-Replayed: true` and the same job id); change `workflow_type` or the upload
and send it with the same key → `409 idempotency_key_reused`. On the dashboard's New job page a
replayed submission says "already had this submission" with a link to the job.
Keys are kept as long as the job row (docs/operations.md).

## 2. A temporary provider failure retries with bounded backoff, then succeeds or fails clearly

**Tests**
- `integration/test_retries.py`: `test_two_transient_failures_then_success`,
  `test_three_transient_failures_use_up_the_budget`, `test_retry_at_equals_the_outbox_available_at`,
  `test_a_retry_skips_stages_whose_checkpoints_verify`
- `integration/test_e2e_pipeline.py` (real worker and dispatcher):
  `test_two_transient_failures_are_retried_with_backoff_then_succeed`,
  `test_three_transient_failures_give_up_after_three_attempts`
- `unit/test_retries_pure.py` (backoff ranges), `unit/test_openai_provider.py` (timeouts, 429 with
  `Retry-After`, 5xx are transient)
- Playwright `jobs.spec.ts`: "a fault-injected job shows its retry live"

**See it:** with `$env:ENABLE_FAULT_INJECTION="true"` (then `docker compose up -d`), create a job
on the New job page with "Fail the first attempts (transient)" = 2: the timeline shows attempts 1
and 2 failing with `injected_transient`, a retry-wait note between the rows (backoff 2.5–5 s, then
5–10 s), and attempt 3 succeeding while reusing the checkpoints. With 3 failures the job ends `failed` with "Gave up
after 3 attempts."

## 3. A permanent input/validation error does not retry

**Tests**
- `integration/test_retries.py`: `test_input_errors_are_not_retried_even_with_attempts_left`,
  `test_an_injected_permanent_failure_is_not_retried`
- `integration/test_pdf_workflow.py`: `test_unsupported_pdf_fails_once_with_a_clear_message`
- `integration/test_repo_workflow.py`: `test_a_permanent_failure_is_never_retried`
- `integration/test_e2e_pipeline.py`: `test_unsupported_pdf_fails_permanently_through_the_real_worker`
- `unit/test_state_machine.py`: `test_manual_retry_not_offered_for_input_errors`
- Playwright `jobs.spec.ts`: "a scanned PDF fails with the right message and Retry disabled with its reason"

**See it:** upload `scanned.pdf` and start a job: it fails after one attempt with `pdf_no_text`,
and the Retry button is disabled with the reason. `curl.exe -s -X POST "$api/v1/jobs/<id>/retry"`
returns `409 job_not_retryable`.

## 4. A restarted worker leaves job state visible; expired work recovers or fails clearly

**Tests**
- `integration/test_e2e_pipeline.py`: `test_worker_process_killed_mid_stage_is_recovered_and_succeeds`,
  `test_killing_the_worker_in_every_attempt_uses_up_the_budget`, `test_a_lost_message_is_requeued_by_recovery`
- `integration/test_recovery.py`: `test_expired_lease_with_attempts_left_moves_the_job_to_retry_wait`,
  `test_expired_lease_with_no_attempts_left_fails_the_job`,
  `test_two_recovery_loops_at_once_handle_each_job_exactly_once`,
  `test_a_worker_finishing_while_recovery_runs_never_deadlocks`
- `integration/test_hardening.py`: `test_a_vanished_worker_cannot_block_recovery_for_long`
- `integration/test_execution.py`: `test_worker_whose_attempt_was_closed_stops_without_writing`

**See it:** `powershell -ExecutionPolicy Bypass -File scripts\demo-recovery.ps1` (docs/operations.md):
it kills the worker mid-job and prints the stored events: `attempt.lease_expired`, `retry_wait`,
attempt 2 reusing checkpoints, `succeeded`. During the outage `GET /v1/jobs/<id>` and the
dashboard keep showing the job as `running` with its lease; the Overview says "No worker is
running" until the new worker reports in.

## 5. Cancelling a queued job prevents execution; a running job stops at a stage boundary

**Tests**
- `integration/test_cancel_and_retry.py`: `test_cancelling_a_queued_job_removes_its_pending_message`,
  `test_a_cancelled_queued_job_never_runs_even_if_its_message_was_delivered`,
  `test_cancelling_a_running_job_stops_it_at_the_next_stage_boundary`,
  `test_a_cancel_committed_while_the_last_stage_completes_is_superseded_by_success`
- `integration/test_e2e_pipeline.py`: `test_cancelling_a_running_job_stops_the_real_worker_at_a_stage_boundary`
- `unit/test_state_machine.py`: `test_cancel_decision`, `test_cancel_decision_covers_claimable_statuses`
- Playwright `jobs.spec.ts`: "cancelling a running job stops it at a stage boundary"

**See it:** with `$env:STUB_DELAY_SECONDS="1"` (then `docker compose up -d worker`), start a job
on `report.pdf`, press **Cancel job** while it is summarizing: the status becomes
`cancel_requested`, then `cancelled` when the stage ends; the timeline's last row says
"Cancelled before" the next stage. Stop the worker (`docker compose stop worker`), create a job and cancel it
while `queued`: it ends `cancelled` at once and never runs after the worker starts again.

## 6. Reconnecting to SSE with Last-Event-ID replays the missing events in order

**Tests**
- `integration/test_events_e2e.py` (real uvicorn): `test_reconnect_receives_exactly_the_missing_events_through_stream_end`,
  `test_a_real_job_streams_from_start_to_end_exactly_as_stored`,
  `test_disconnecting_never_affects_a_running_job`,
  `test_finished_job_then_manual_retry_reopens_the_stream`
- `integration/test_events_api.py`: `test_events_committed_right_after_a_read_are_sent_by_the_next_poll`,
  `test_a_seq_gap_is_logged_and_never_silently_skipped`, `test_history_pages_through_events_in_seq_order`
- `unit/test_events_pure.py` (resume position parsing, frame format)
- Playwright `jobs.spec.ts`: "reloading mid-job resumes the timeline without duplicates or gaps"

**See it:** `curl.exe -N -H "Last-Event-ID: 5" "$api/v1/jobs/<id>/events"` prints events 6, 7, …
in order and ends with `event: stream.end` for a finished job. Reload a running job's page in the
dashboard: the timeline continues without repeating or missing a row.

## 7. Duplicate Celery delivery creates no duplicate artifacts and doesn't corrupt state

**Tests**
- `integration/test_e2e_pipeline.py`: `test_duplicate_delivery_runs_the_job_only_once`
- `integration/test_execution.py`: `test_concurrent_duplicate_deliveries_run_the_job_once`,
  `test_second_delivery_after_success_is_dropped`, `test_lost_claim_race_writes_nothing`
- `integration/test_dispatcher.py`: `test_crash_after_publish_before_commit_causes_a_duplicate_that_the_worker_drops`
- `integration/test_recovery.py`: `test_duplicate_deliveries_after_recovery_start_one_attempt`
- `integration/test_artifacts_api.py`: `test_upserting_the_same_artifact_type_replaces_the_row`
- `integration/test_constraints.py`: the one-open-attempt-per-job and one-artifact-per-type constraints
- `unit/test_pdf_stages_rerun.py`: every stage produces byte-identical output when run twice

**See it:** the worker logs `message dropped` with a reason for a duplicate
(`docker compose logs worker | Select-String "message dropped"`); a job never has two artifacts
of one type (`GET /v1/jobs/<id>`).

## 8. Invalid, oversized, corrupt or path-traversal ZIP uploads are rejected safely

**Tests**
- `integration/test_uploads_api.py`: `test_oversized_body_without_content_length_is_rejected_while_streaming`,
  `test_lying_content_length_does_not_bypass_the_limit`,
  `test_unsupported_extension_is_rejected_before_anything_is_stored`,
  `test_a_stalled_upload_is_cut_off_and_cleaned_up`, and the signature tests
- `unit/test_repo_archive.py` (real sandboxed reader): `test_unsafe_names_reject_the_whole_archive`,
  `test_encrypted_or_corrupt_archives_are_rejected`, `test_a_high_ratio_bomb_is_rejected`,
  `test_a_header_that_understates_the_size_is_caught`, `test_too_many_entries_is_decided_from_the_end_record`,
  `test_symlinks_and_special_files_are_detected_from_their_mode_bits`
- `integration/test_repo_workflow.py`: `test_malicious_archives_fail_permanently_and_write_nothing_outside_the_work_folder`,
  `test_a_symlink_to_a_secret_is_skipped_and_never_followed`
- `integration/test_e2e_pipeline.py`: `test_a_zip_slip_archive_fails_permanently_through_the_real_worker`

**See it:** generate the sample ZIPs (`devtools.zip_samples`) and start a `repo_index` job for
each: `zipslip.zip` → `zip_unsafe_path`, `bomb.zip` → `zip_bomb`, `encrypted.zip` →
`zip_encrypted`, `corrupt.zip` → `zip_corrupt`, `symlink.zip` succeeds with the link skipped
(`symlink` in the manifest's skip reasons). A 101 MiB upload gets `413 upload_too_large`. Threat
model: [security.md](security.md).

## 9. A no-key local run completes both workflows with the stub provider

**Tests**
- `integration/test_e2e_pipeline.py`: `test_pdf_job_succeeds_with_the_stub_provider_and_no_api_key`,
  `test_repo_job_succeeds_with_the_stub_provider_and_all_three_artifacts_download`
- `integration/test_pdf_workflow.py`: `test_text_pdf_runs_every_stage_and_produces_downloadable_artifacts`
- `integration/test_repo_workflow.py`: `test_a_realistic_repository_is_indexed_and_all_three_artifacts_download`
- Playwright `jobs.spec.ts`: "a PDF job runs live to succeeded and its summary downloads",
  "a repository ZIP shows the manifest viewer with skip reasons"

**See it:** a fresh clone, `Copy-Item .env.example .env`, `docker compose up --build -d`, then
upload `report.pdf` and `repo.zip` on the New job page. Both succeed; the job pages offer
`summary.md`, `manifest.json` (and `chunks.jsonl` for the repository). The benchmark
([performance.md](performance.md)) runs 40 such jobs.

## 10. A reviewer can find attempts, error, artifacts, and trace/log information for a failed job

**Tests**
- `integration/test_observability_e2e.py`: `test_a_failed_job_is_fully_findable_from_its_trace_id`,
  `test_one_trace_connects_request_dispatch_attempts_stages_and_provider_calls`
- `integration/test_cancel_and_retry.py`: `test_detail_says_whether_and_why_a_failed_job_can_be_retried`
- `integration/test_pdf_workflow.py`: `test_failure_details_stay_out_of_the_database` (tracebacks
  only in logs, a safe summary in the job)
- Playwright `jobs.spec.ts`: "a scanned PDF fails with the right message and Retry disabled with its reason"

**See it:** open a failed job in the dashboard: error class and message, what you can do, the
attempts table (worker, outcome, error class per attempt), the timeline, the full event history,
downloads (none for a failed job), and its **Trace id** with a copy button.
`docker compose logs --no-log-prefix api dispatcher worker | Select-String <trace_id>` prints
every log line of that job across the three services; with the `observability` Compose profile,
"Open trace" shows it in Jaeger. Walkthrough: [observability.md](observability.md#investigating-a-failed-job).
