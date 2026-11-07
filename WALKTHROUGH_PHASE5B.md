# Phase 5 Part B walkthrough: retries, resume, fault injection

For Windows PowerShell 5.1. Open a new PowerShell window and run the steps in order, in that
same window. Every path is held in a variable, so you never need to `cd`. Your job ids, times,
and exact retry delays will differ from the examples below; the delays are random within the
ranges shown.

## Setup

**1. Set the paths and the API address.**
```powershell
$repo = "D:\Projects\Tasklane"
$compose = "$repo\compose.yaml"
$demo = "$env:USERPROFILE\tasklane-demo"
$api = "http://127.0.0.1:8000"
```
Expected: no output.

**2. Define four helper functions** (they exist only in this PowerShell window).
`New-TasklaneJob` uploads `report.pdf` and creates a `pdf_ingest` job with the options you
give it, returning the HTTP status and the response body. `Wait-TasklaneJob` waits until a job
finishes. `Show-TasklaneEvents` prints a job's event history (`backend/devtools/job_events.py`).
`Wait-TasklaneReady` waits until the API and the worker have started.
```powershell
function New-TasklaneJob($Options) {
    $upload = curl.exe -s -X POST "$api/v1/uploads?filename=report.pdf" -H "Content-Type: application/pdf" --data-binary "@$demo\report.pdf" | ConvertFrom-Json
    $body = @{ workflow_type = "pdf_ingest"; upload_id = $upload.id }
    if ($Options) { $body.options = $Options }
    $body | ConvertTo-Json -Depth 5 -Compress | Set-Content -Encoding ascii "$demo\job.json"
    $key = "demo-" + [guid]::NewGuid()
    $status = curl.exe -s -o "$demo\response.json" -w "%{http_code}" -X POST "$api/v1/jobs" -H "Content-Type: application/json" -H "Idempotency-Key: $key" --data-binary "@$demo\job.json"
    [pscustomobject]@{ status = [int]$status; body = (Get-Content "$demo\response.json" -Raw | ConvertFrom-Json) }
}
function Wait-TasklaneJob($JobId) {
    $deadline = (Get-Date).AddSeconds(120)
    do {
        Start-Sleep -Milliseconds 500
        $job = curl.exe -s "$api/v1/jobs/$JobId" | ConvertFrom-Json
    } while (($job.status -notin @("succeeded", "failed", "cancelled")) -and ((Get-Date) -lt $deadline))
    $job
}
function Show-TasklaneEvents($JobId) {
    docker compose -f $compose exec -T api python -m devtools.job_events $JobId
}
function Wait-TasklaneReady {
    do { Start-Sleep -Seconds 2; $ready = curl.exe -s "$api/health/ready" } until ($ready -like '*"status":"ready"*')
    do { Start-Sleep -Seconds 2; $log = docker compose -f $compose logs worker | Out-String } until ($log -match "ready\.")
    "api and worker are ready"
}
```
Expected: no output.

**3. Start the stack, or update it.** Compose recreates the containers whose settings changed
(Part B added `RETRY_BASE_SECONDS`, `RETRY_MAX_SECONDS` and `ENABLE_FAULT_INJECTION`), and the
one-shot `migrate` service applies migration 0004.
```powershell
docker compose -f $compose up -d
```
Expected (the order and wording vary):

```
 Container tasklane-dispatcher-1 Recreated
 Container tasklane-api-1 Recreated
 Container tasklane-worker-1 Recreated
 Container tasklane-postgres-1 Healthy
 Container tasklane-migrate-1 Exited
 Container tasklane-dispatcher-1 Started
 Container tasklane-worker-1 Started
 Container tasklane-api-1 Started
```

**4. Wait until the API and the worker are ready.**
```powershell
Wait-TasklaneReady
```
Expected:

```
api and worker are ready
```

**5. Check which migration the database is on.**
```powershell
docker compose -f $compose run --rm migrate alembic current
```
Expected: the last line is

```
0004 (head)
```

**6. Create a folder for the sample files.**
```powershell
New-Item -ItemType Directory -Force $demo | Out-Null
```
Expected: no output.

**7. Generate the sample PDFs into it.**
```powershell
docker compose -f $compose run --rm -v "${demo}:/out" api python -m devtools.pdf_samples /out
```
Expected (after Docker's container lines):

```
wrote /out/report.pdf
wrote /out/scanned.pdf
wrote /out/encrypted.pdf
wrote /out/owner_locked.pdf
wrote /out/corrupt.pdf
```

## (a) Fault injection is off: the API refuses it

**8. Confirm the flag is off inside the API container.**
```powershell
docker compose -f $compose exec -T api printenv ENABLE_FAULT_INJECTION
```
Expected:

```
false
```

**9. Describe "fail the first two attempts with a transient error".**
```powershell
$faults2 = @{ fault_injection = @{ mode = "transient"; failures = 2 } }
```
Expected: no output.

**10. Try to create a job that asks for it.**
```powershell
$refused = New-TasklaneJob $faults2
```
Expected: no output.

**11. Show the HTTP status.**
```powershell
$refused.status
```
Expected:

```
422
```

**12. Show the error.**
```powershell
$refused.body.error | Format-List code, message
```
Expected:

```
code    : fault_injection_disabled
message : This job asks for fault injection, but fault injection is disabled on this server. It is a development
          feature, enabled with ENABLE_FAULT_INJECTION=true.
```

## (b) Turn fault injection on for this window only

**13. Set the flag as a shell variable.** Compose reads it instead of `.env`, and only in this
window. `.env` is not edited.
```powershell
$env:ENABLE_FAULT_INJECTION = "true"
```
Expected: no output.

**14. Recreate the API and worker containers so they pick it up.**
```powershell
docker compose -f $compose up -d --force-recreate api worker
```
Expected (wording varies):

```
 Container tasklane-api-1 Recreated
 Container tasklane-worker-1 Recreated
 Container tasklane-migrate-1 Exited
 Container tasklane-worker-1 Started
 Container tasklane-api-1 Started
```

**15. Wait until both are ready again.**
```powershell
Wait-TasklaneReady
```
Expected:

```
api and worker are ready
```

**16. Confirm the worker sees the flag.**
```powershell
docker compose -f $compose exec -T worker printenv ENABLE_FAULT_INJECTION
```
Expected:

```
true
```

**17. Find the startup warnings both services log when the flag is on.**
```powershell
docker compose -f $compose logs api worker | Select-String "fault injection is ENABLED"
```
Expected: one line from `api` and one from `worker`, similar to

```
api-1     | {"ts": "2026-11-07T14:42:22.899861+00:00", "level": "WARNING", "logger": "app.main", "message": "fault injection is ENABLED: jobs may request injected failures (development only; set ENABLE_FAULT_INJECTION=false to turn it off)"}
worker-1  | {"ts": "2026-11-07T14:42:23.821807+00:00", "level": "WARNING", "logger": "app.queue.worker_main", "message": "fault injection is ENABLED: jobs may request injected failures (development only; set ENABLE_FAULT_INJECTION=false to turn it off)"}
```

## (c) Two transient failures, then success

**18. Create the job.** The same options as step 10, now accepted.
```powershell
$jobC = (New-TasklaneJob $faults2).body
```
Expected: no output.

**19. Show its first status.**
```powershell
$jobC.status
```
Expected:

```
queued
```

**20. Wait for it to finish.** This takes about 10–20 seconds: two retry delays (2.5–5 s, then
5–10 s) plus the work.
```powershell
$doneC = Wait-TasklaneJob $jobC.id
```
Expected: no output (it returns when the job is finished).

**21. Show the final status.**
```powershell
$doneC.status
```
Expected:

```
succeeded
```

**22. Show the three attempts.**
```powershell
$doneC.attempts | Format-Table attempt_no, trigger, outcome, error_class
```
Expected:

```
attempt_no trigger    outcome          error_class
---------- -------    -------          -----------
         1 initial    failed_transient injected_transient
         2 auto_retry failed_transient injected_transient
         3 auto_retry succeeded
```

**23. Show the status history with the retry delays.**
```powershell
Show-TasklaneEvents $jobC.id | Select-String "status_changed"
```
Expected: running, retry_wait, running, retry_wait, running, succeeded. The first delay is
2.5–5 s, the second 5–10 s.

```
   2        1  job.status_changed    queued -> running
  19        1  job.status_changed    running -> retry_wait  (retry in 3.66 s)  [injected_transient]
  20        2  job.status_changed    retry_wait -> running
  27        2  job.status_changed    running -> retry_wait  (retry in 6.53 s)  [injected_transient]
  28        3  job.status_changed    retry_wait -> running
  42        3  job.status_changed    running -> succeeded
```

## (d) Three transient failures: the job gives up

**24. Describe "fail the first three attempts".**
```powershell
$faults3 = @{ fault_injection = @{ mode = "transient"; failures = 3 } }
```
Expected: no output.

**25. Create the job.**
```powershell
$jobD = (New-TasklaneJob $faults3).body
```
Expected: no output.

**26. Wait for it to finish** (about 10–20 seconds).
```powershell
$doneD = Wait-TasklaneJob $jobD.id
```
Expected: no output.

**27. Show the outcome.**
```powershell
$doneD | Format-List status, error_class, error_summary, attempt_count
```
Expected:

```
status        : failed
error_class   : injected_transient
error_summary : Injected transient failure (fault injection test). Gave up after 3 attempts.
attempt_count : 3
```

**28. Show the attempts.**
```powershell
$doneD.attempts | Format-Table attempt_no, trigger, outcome, error_class
```
Expected:

```
attempt_no trigger    outcome          error_class
---------- -------    -------          -----------
         1 initial    failed_transient injected_transient
         2 auto_retry failed_transient injected_transient
         3 auto_retry failed_transient injected_transient
```

## (e) A permanent failure is never retried

**29. Describe "fail permanently".**
```powershell
$permanent = @{ fault_injection = @{ mode = "permanent" } }
```
Expected: no output.

**30. Create the job.**
```powershell
$jobE = (New-TasklaneJob $permanent).body
```
Expected: no output.

**31. Wait for it to finish** (a few seconds; there is no retry delay).
```powershell
$doneE = Wait-TasklaneJob $jobE.id
```
Expected: no output.

**32. Show the outcome: one attempt, no retry.**
```powershell
$doneE | Format-List status, error_class, error_summary, attempt_count
```
Expected:

```
status        : failed
error_class   : injected_permanent
error_summary : Injected permanent failure (fault injection test).
attempt_count : 1
```

## (f) Resume: the retries skipped the stages that had already finished

**33. Show the stages each attempt ran or skipped** for the job from (c).
```powershell
Show-TasklaneEvents $jobC.id | Select-String "attempt.started|stage.started|stage.skipped"
```
Expected: attempt 1 runs four stages and fails in `summarize`. Attempts 2 and 3 skip
`validate_pdf`, `extract_text` and `chunk_document`, whose saved outputs were verified by size
and SHA-256, and start at `summarize`.

```
   3        1  attempt.started       trigger=initial
   4        1  stage.started         validate_pdf
   7        1  stage.started         extract_text
  14        1  stage.started         chunk_document
  17        1  stage.started         summarize
  21        2  attempt.started       trigger=auto_retry
  22        2  stage.skipped         validate_pdf (checkpoint_reused)
  23        2  stage.skipped         extract_text (checkpoint_reused)
  24        2  stage.skipped         chunk_document (checkpoint_reused)
  25        2  stage.started         summarize
  29        3  attempt.started       trigger=auto_retry
  30        3  stage.skipped         validate_pdf (checkpoint_reused)
  31        3  stage.skipped         extract_text (checkpoint_reused)
  32        3  stage.skipped         chunk_document (checkpoint_reused)
  33        3  stage.started         summarize
  39        3  stage.started         write_artifacts
```

**34. Show the full history of that job**, including the failures (`stage.failed ... retryable`).
(Progress events are hidden; add `--all` to the command inside `Show-TasklaneEvents` to see them. Sequence numbers jump where they were.)
```powershell
Show-TasklaneEvents $jobC.id
```
Expected:

```
 seq  attempt  event                 details
   1        -  job.created
   2        1  job.status_changed    queued -> running
   3        1  attempt.started       trigger=initial
   4        1  stage.started         validate_pdf
   6        1  stage.completed       validate_pdf
   7        1  stage.started         extract_text
  13        1  stage.completed       extract_text
  14        1  stage.started         chunk_document
  16        1  stage.completed       chunk_document
  17        1  stage.started         summarize
  18        1  stage.failed          summarize: injected_transient (retryable)
  19        1  job.status_changed    running -> retry_wait  (retry in 3.66 s)  [injected_transient]
  20        2  job.status_changed    retry_wait -> running
  21        2  attempt.started       trigger=auto_retry
  22        2  stage.skipped         validate_pdf (checkpoint_reused)
  23        2  stage.skipped         extract_text (checkpoint_reused)
  24        2  stage.skipped         chunk_document (checkpoint_reused)
  25        2  stage.started         summarize
  26        2  stage.failed          summarize: injected_transient (retryable)
  27        2  job.status_changed    running -> retry_wait  (retry in 6.53 s)  [injected_transient]
  28        3  job.status_changed    retry_wait -> running
  29        3  attempt.started       trigger=auto_retry
  30        3  stage.skipped         validate_pdf (checkpoint_reused)
  31        3  stage.skipped         extract_text (checkpoint_reused)
  32        3  stage.skipped         chunk_document (checkpoint_reused)
  33        3  stage.started         summarize
  38        3  stage.completed       summarize
  39        3  stage.started         write_artifacts
  41        3  stage.completed       write_artifacts
  42        3  job.status_changed    running -> succeeded
```

## (g) Turn fault injection off again

**35. Remove the shell variable.**
```powershell
Remove-Item Env:ENABLE_FAULT_INJECTION
```
Expected: no output.

**36. Recreate the API and worker containers without it.**
```powershell
docker compose -f $compose up -d --force-recreate api worker
```
Expected: the same kind of lines as step 14.

**37. Wait until both are ready.**
```powershell
Wait-TasklaneReady
```
Expected:

```
api and worker are ready
```

**38. Confirm the flag is off again.**
```powershell
docker compose -f $compose exec -T worker printenv ENABLE_FAULT_INJECTION
```
Expected:

```
false
```

**39. Confirm the API refuses fault injection again.**
```powershell
(New-TasklaneJob $faults2).status
```
Expected:

```
422
```

Closing this PowerShell window also forgets the variable. `.env` never changed, so the next
`docker compose up -d` from any window starts with fault injection off.
