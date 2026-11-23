# Phase 7 walkthrough: recovery, cancellation, manual retry

Windows PowerShell 5.1, one window, from any folder (every path is a variable; no `cd`).
The stack must be running (`docker compose up --build -d` in the repo). Demo settings are
**shell variables only** (`.env` is never edited); step 30 removes them again.

Demo settings: lease 20 s (heartbeat every 5 s), recovery every 2 s, lost-message grace 15 s,
first retry after 5–10 s, the stub summarizer waits 3 s per call (so jobs are slow enough to
interrupt), and fault injection on (for the retry_wait example).

## Setup

**1. Paths and URLs.**
```powershell
$repo = "D:\Projects\Tasklane"; $api = "http://127.0.0.1:8000"; $demo = "$env:USERPROFILE\tasklane-demo"; New-Item -ItemType Directory -Force $demo | Out-Null
```
Expected: no output.

**2. Helper functions** (paste the whole block once).
```powershell
function dc { docker compose -f "$repo\compose.yaml" @args }
function Get-DemoJob($id) { curl.exe -s "$api/v1/jobs/$id" | ConvertFrom-Json }
function Show-Events($id) { dc exec -T api python -m devtools.job_events $id }
function New-DemoJob($file, [switch]$Faults) {
    $type = if ($file -like "*.pdf") { "pdf_ingest" } else { "repo_index" }
    $mime = if ($type -eq "pdf_ingest") { "application/pdf" } else { "application/zip" }
    $upload = curl.exe -s -X POST "$api/v1/uploads?filename=$(Split-Path $file -Leaf)" -H "Content-Type: $mime" --data-binary "@$file" | ConvertFrom-Json
    $body = @{ workflow_type = $type; upload_id = $upload.id }
    if ($Faults) { $body.options = @{ fault_injection = @{ mode = "transient"; failures = 3 } } }
    $body | ConvertTo-Json -Depth 5 -Compress | Set-Content -Encoding ascii "$demo\job.json"
    (curl.exe -s -X POST "$api/v1/jobs" -H "Content-Type: application/json" -H "Idempotency-Key: demo-$([guid]::NewGuid())" --data-binary "@$demo\job.json" | ConvertFrom-Json).id
}
function Wait-Status($id, $status, $seconds = 120) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while (($job = Get-DemoJob $id).status -ne $status) {
        if ((Get-Date) -gt $deadline) { throw "job $id is '$($job.status)', not '$status'" }
        Start-Sleep -Milliseconds 300
    }
    [pscustomobject]@{ status = $job.status; attempts = ($job.attempts | ForEach-Object { "$($_.attempt_no):$($_.trigger)->$($_.outcome)" }) -join "  "; error = $job.error_summary }
}
function Cancel-DemoJob($id) { curl.exe -s -X POST "$api/v1/jobs/$id/cancel" | ConvertFrom-Json | Select-Object status, can_cancel, cancel_blocked_reason }
function Retry-DemoJob($id) { curl.exe -s -w "`nHTTP %{http_code}`n" -X POST "$api/v1/jobs/$id/retry" }
function Stop-AttemptWorker($id, $attemptNo) {
    $deadline = (Get-Date).AddSeconds(120)
    do { $lease = (Get-DemoJob $id).lease; Start-Sleep -Milliseconds 300 } until (($lease -and $lease.attempt_no -eq $attemptNo) -or (Get-Date) -gt $deadline)
    $workerPid = $lease.worker_id.Split(":")[-1]
    dc exec -T worker sh -c "kill -9 $workerPid"
    "killed worker process $workerPid while it ran attempt $attemptNo"
}
function Stop-DuringStage($id, $stage) {
    do { $job = Get-DemoJob $id } until ($job.current_stage -eq $stage -and $job.lease)
    $workerPid = $job.lease.worker_id.Split(":")[-1]
    dc exec -T worker sh -c "kill -9 $workerPid"
    "killed worker process $workerPid during $stage"
    Start-Sleep -Milliseconds 500
    dc exec -T worker python -m devtools.ps
}
function Reset-Demo {
    "LEASE_SECONDS", "PROVIDER_TIMEOUT_SECONDS", "RECOVERY_INTERVAL_SECONDS", "LOST_MESSAGE_GRACE_SECONDS", "STUB_DELAY_SECONDS", "RETRY_BASE_SECONDS", "ENABLE_FAULT_INJECTION" | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
    dc up -d worker dispatcher api
}
```
Expected: no output.

**3. Demo settings, for this PowerShell window only.**
```powershell
$env:LEASE_SECONDS = "20"; $env:PROVIDER_TIMEOUT_SECONDS = "10"; $env:RECOVERY_INTERVAL_SECONDS = "2"; $env:LOST_MESSAGE_GRACE_SECONDS = "15"; $env:STUB_DELAY_SECONDS = "3"; $env:RETRY_BASE_SECONDS = "10"; $env:ENABLE_FAULT_INJECTION = "true"
```
Expected: no output.

**4. Restart the backend containers with those settings** (Compose reads shell variables
before `.env`).
```powershell
dc up -d worker dispatcher api
```
Expected (among the lines): `Container tasklane-worker-1  Started`, `...dispatcher-1  Started`,
`...api-1  Started`. Compose may also re-run the one-shot `migrate` service first (`migrate-1
Exited`); that is harmless.

**5. Sample PDFs** (`report.pdf`, `scanned.pdf`, `large.pdf` with 500 pages, ...).
```powershell
dc run --rm -v "${demo}:/out" api python -m devtools.pdf_samples /out
```
Expected: `wrote /out/report.pdf` ... `wrote /out/large.pdf`.

## (a) The job stuck in `running` since Phase 4

**6. Its history.** Recovery found its expired lease as soon as the new dispatcher started.
```powershell
Show-Events e5a4bdc7-4bfa-4b42-8286-39e9ef320b26
```
Expected: `attempt.lease_expired` for attempt 1, `running -> retry_wait ... [lease_expired]`,
attempt 2 `trigger=recovery`, then `pdf_corrupt` and `running -> failed`. (Its Phase 4 upload
was not a real PDF, so once it could run again it failed honestly instead of hanging.)

## (b) Kill the worker container mid-job

**7. Start a job and wait until a worker has it.**
```powershell
$b = New-DemoJob "$demo\report.pdf"; Wait-Status $b running
```
Expected: `status: running`, `attempts: 1:initial->`.

**8. Kill the whole worker container** (no warm shutdown).
```powershell
dc kill worker
```
Expected: `Container tasklane-worker-1  Killed`.

**9. Start it again.**
```powershell
dc start worker
```
Expected: `Container tasklane-worker-1  Started`.

**10. Wait.** The lease runs out (≤ 20 s), recovery closes the attempt, the job waits 5–10 s in
`retry_wait`, then runs again.
```powershell
Wait-Status $b succeeded
```
Expected: `succeeded`, `attempts: 1:initial->lease_expired  2:recovery->succeeded`.

**11. The story in events.**
```powershell
Show-Events $b
```
Expected: `attempt.lease_expired`, `running -> retry_wait (retry in ...) [lease_expired]`,
`retry_wait -> running`, `attempt.started trigger=recovery`, then the stages (a stage that had
finished before the kill shows as `stage.skipped`), `running -> succeeded`.

## (c) A lost message, and (d) cancelling a queued job

**12. Stop the worker**, so published messages wait in Redis.
```powershell
dc stop worker
```
Expected: `Container tasklane-worker-1  Stopped`.

**13. Create a job, let the dispatcher publish it, then delete it from Redis** ("Redis lost it").
```powershell
$c = New-DemoJob "$demo\report.pdf"; Start-Sleep 3; dc exec -T redis redis-cli DEL tasklane
```
Expected: `1` (the queue existed and is now gone).

**14. Create another job and cancel it while it is queued.** Its message is already in Redis.
```powershell
$d1 = New-DemoJob "$demo\report.pdf"; Start-Sleep 3; Cancel-DemoJob $d1
```
Expected: `status: cancelled`, `can_cancel: False`, `cancel_blocked_reason: already_finished`.

**15. Start the worker again.**
```powershell
dc start worker
```
Expected: `Container tasklane-worker-1  Started`.

**16. The lost job is re-queued by recovery** after the 15 s grace period, and runs.
```powershell
Wait-Status $c succeeded
```
Expected: `succeeded`, `attempts: 1:initial->succeeded`.

**17. Its events show the requeue.**
```powershell
Show-Events $c
```
Expected: a `job.requeued` line `lost_message (was queued)` between `job.created` and
`queued -> running`.

**18. The cancelled job's message was delivered and dropped, never run.**
```powershell
dc logs worker --since 5m | Select-String $d1
```
Expected: a `"message dropped"` line with `"reason": "not_claimable", "job_status": "cancelled"`.

## (d) Cancelling a running job and a job in retry_wait

**19. Cancel a running job.**
```powershell
$d2 = New-DemoJob "$demo\report.pdf"; Wait-Status $d2 running | Out-Null; Cancel-DemoJob $d2
```
Expected: `status: cancel_requested`, `cancel_blocked_reason: already_cancel_requested`.

**20. It stops at the next stage boundary.**
```powershell
Wait-Status $d2 cancelled
```
Expected: `cancelled`, `attempts: 1:initial->cancelled`.

**21. Cancel a job while it waits to retry** (its first summarizer call fails on purpose).
```powershell
$d3 = New-DemoJob "$demo\report.pdf" -Faults; Wait-Status $d3 retry_wait | Out-Null; Cancel-DemoJob $d3
```
Expected: `status: cancelled` (straight away: no worker holds it).

## (e) Kill the worker in every attempt

**22. Kill the worker process running attempt 1, then 2, then 3** (Celery replaces it each time).
```powershell
$e = New-DemoJob "$demo\report.pdf"; 1..3 | ForEach-Object { Stop-AttemptWorker $e $_ }
```
Expected (takes about a minute): `killed worker process N while it ran attempt 1`, `... 2`, `... 3`.

**23. The budget is used up.**
```powershell
Wait-Status $e failed 180
```
Expected: `failed`, three `lease_expired` attempts, and `The worker running the job stopped
responding before it finished (its lease expired). Gave up after 3 attempts.`

## (f) Manual retry

**24. Retry it.**
```powershell
Retry-DemoJob $e
```
Expected: JSON with `"status":"queued"`, `"max_attempts":6`, `"manual_retry_count":1`,
`"can_retry":false,"retry_blocked_reason":"not_failed"`, then `HTTP 200`.

**25. It runs again on the same job, resuming after the stages that already finished.**
```powershell
Wait-Status $e succeeded
```
Expected: `succeeded`, attempts 1-3 `lease_expired`, `4:manual_retry->succeeded`.

**26. An input error is not retryable.**
```powershell
$s = New-DemoJob "$demo\scanned.pdf"; Wait-Status $s failed | Out-Null; Retry-DemoJob $s
```
Expected: `{"error":{"code":"job_not_retryable","message":"The job failed because of its input;
...","details":{"reason":"input_error","status":"failed"},...}}` and `HTTP 409`.

## (g) No orphaned parser after a killed worker

**27. Kill the worker process while it parses a 500-page PDF, then list the container's processes.**
```powershell
$g = New-DemoJob "$demo\large.pdf"; Stop-DuringStage $g extract_text
```
Expected: `killed worker process N during extract_text`, then a process list with only
`/sbin/docker-init`, the reload runner and `celery` processes: no `limited_launcher.py` / `parse_child.py`.

**28. Cancel the large job right away.** Its worker is dead, so recovery finishes the cancel when
the lease expires (expired lease while `cancel_requested` → `cancelled`).
```powershell
Cancel-DemoJob $g | Out-Null; Wait-Status $g cancelled 60
```
Expected: `cancelled`, `attempts: 1:initial->lease_expired`.

**29. The same guarantee, proven with a parser that never writes** (one that would otherwise sleep
for 120 s), by the tests that kill a real parent process.
```powershell
dc run --rm api pytest tests/unit/test_pdf_sandbox.py -q -p no:cacheprovider -k "worker_process_is_killed or died_before"
```
Expected: `2 passed`.

## Reset

**30. Remove the demo settings and restart the containers with your `.env` values.**
```powershell
Reset-Demo
```
Expected: the three containers `Recreated` and started again (plus, possibly, `migrate-1 Exited`).
Check with `dc exec -T dispatcher printenv LEASE_SECONDS`: `60`.
