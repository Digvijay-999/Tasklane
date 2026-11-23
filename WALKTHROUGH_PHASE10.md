# Phase 10 walkthrough: traces, metrics and logs

Windows PowerShell 5.1, one window, every path is a variable, no `cd`. Demo settings are **shell
variables only** (`.env` is never edited); step 25 removes them. Ids, trace ids, times and counts
will differ from the examples. Background: [docs/observability.md](docs/observability.md).

Demo settings: traces exported to the local Jaeger, the dashboard's "Open trace" link pointing
at it, fault injection on, first automatic retry after 0.5–1 s.

## Setup

**1. Paths and a short name for `docker compose`.**
```powershell
$repo = "D:\Projects\Tasklane"; $demo = "$env:USERPROFILE\tasklane-demo"; $api = "http://127.0.0.1:8000"; $jaeger = "http://127.0.0.1:16686"; New-Item -ItemType Directory -Force $demo | Out-Null; function dc { docker compose -f "$repo\compose.yaml" @args }
```
Expected: no output.

**2. Helper functions** (create a job, wait for it, read metrics, print a trace from Jaeger).
```powershell
function New-DemoJob($File, $Workflow = "pdf_ingest", $Options = $null) {
  $type = if ($Workflow -eq "repo_index") { "application/zip" } else { "application/pdf" }
  $upload = Invoke-RestMethod -Method Post -Uri "$api/v1/uploads?filename=$(Split-Path $File -Leaf)" -InFile $File -ContentType $type
  $body = @{ workflow_type = $Workflow; upload_id = $upload.id }
  if ($Options) { $body.options = $Options }
  Invoke-RestMethod -Method Post -Uri "$api/v1/jobs" -ContentType "application/json" -Body ($body | ConvertTo-Json -Depth 5) -Headers @{ "Idempotency-Key" = [guid]::NewGuid().ToString() }
}
function Wait-DemoJob($Id, $Seconds = 120) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  do {
    $job = Invoke-RestMethod "$api/v1/jobs/$Id"
    if ($job.status -in "succeeded", "failed", "cancelled") { return $job }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)
  throw "job $Id is still $($job.status)"
}
function Get-DemoMetrics($Pattern) { (Invoke-WebRequest "$api/metrics" -UseBasicParsing).Content -split "`n" | Where-Object { $_ -match $Pattern } }
function Get-MetricMap { $map = @{}; Get-DemoMetrics '^tasklane_' | ForEach-Object { $i = $_.LastIndexOf(' '); $map[$_.Substring(0, $i)] = [double]$_.Substring($i + 1) }; $map }
function Show-Trace($TraceId, $MinAttempts = 1) {
  for ($try = 0; $try -lt 20; $try++) {
    try { $trace = (Invoke-RestMethod "$jaeger/api/traces/$TraceId").data[0] } catch { $trace = $null }
    if ($trace -and @($trace.spans | Where-Object { $_.operationName -eq "job.attempt" }).Count -ge $MinAttempts) { break }
    Start-Sleep -Seconds 1
  }
  $trace.spans | Sort-Object startTime | Where-Object { $_.operationName -notlike "db *" } | ForEach-Object {
    $tags = @{}; $_.tags | ForEach-Object { $tags[$_.key] = $_.value }
    [pscustomobject]@{ service = $trace.processes.($_.processID).serviceName; span = $_.operationName; attempt = $tags["attempt_no"]; outcome = $tags["outcome"]; error_class = $tags["error_class"] }
  } | Format-Table -AutoSize
  "database spans: " + @($trace.spans | Where-Object { $_.operationName -like "db *" }).Count
}
```
Expected: no output.

**3. Start the stack with the observability profile and the demo settings** (rebuilds the backend
image the first time: new Python packages).
```powershell
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://jaeger:4318"; $env:TRACE_VIEWER_URL = "http://127.0.0.1:16686/trace/{trace_id}"; $env:ENABLE_FAULT_INJECTION = "true"; $env:RETRY_BASE_SECONDS = "1"; dc --profile observability up --build -d
```
Expected: ends with lines like `Container tasklane-jaeger-1  Started` and
`Container tasklane-prometheus-1  Started`. (PowerShell may print Docker's progress in red; that
is normal.)

**4. Sample files** (a text PDF and a scanned PDF, generated in code).
```powershell
dc run --rm -v "${demo}:/out" api python -m devtools.pdf_samples /out
```
Expected: `wrote /out/report.pdf`, `wrote /out/scanned.pdf`, ... (files in
`%USERPROFILE%\tasklane-demo`).

## (a) The viewers

**5. Are they up?**
```powershell
Start-Sleep -Seconds 5; (Invoke-WebRequest "$jaeger/" -UseBasicParsing).StatusCode; (Invoke-WebRequest "http://127.0.0.1:9090/-/ready" -UseBasicParsing).Content; (Invoke-RestMethod "$api/v1/meta").trace_viewer_url
```
Expected:
```
200
Prometheus Server is Ready.
http://127.0.0.1:16686/trace/{trace_id}
```

**6. Open http://127.0.0.1:16686** (Jaeger). In *Service*, the list has `tasklane-api` (and
`tasklane-dispatcher`, `tasklane-worker` once a job ran). **Open http://127.0.0.1:9090**
(Prometheus), *Status → Target health*: `tasklane-api` (`http://api:8000/metrics`) is **UP**.

## (e) Metrics, before

**7. Remember the current metric values** (your database already has jobs from earlier phases).
```powershell
$before = Get-MetricMap; Get-DemoMetrics '^tasklane_(jobs_created_total|workers_alive|metrics_database_up)'
```
Expected (counts differ):
```
tasklane_metrics_database_up 1.0
tasklane_jobs_created_total{workflow_type="pdf_ingest"} 147.0
tasklane_jobs_created_total{workflow_type="repo_index"} 42.0
tasklane_workers_alive 2.0
```

## (b) A PDF job's full trace

**8. Run a PDF job.**
```powershell
$job = New-DemoJob "$demo\report.pdf"; $done = Wait-DemoJob $job.id; "$($done.status) trace_id=$($done.trace_id)"
```
Expected: `succeeded trace_id=<32 hex digits>`.

**9. Print its trace from Jaeger** (database spans are counted, not listed).
```powershell
Show-Trace $done.trace_id
```
Expected: one trace with the whole story, from three processes:
```
service             span                   attempt outcome   error_class
-------             ----                   ------- -------   -----------
tasklane-api        POST /v1/jobs
tasklane-api        job.create                     created
tasklane-dispatcher outbox.publish                 published
tasklane-worker     job.attempt            1       succeeded
tasklane-worker     stage validate_pdf     1       completed
tasklane-worker     stage extract_text     1       completed
tasklane-worker     stage chunk_document   1       completed
tasklane-worker     stage summarize        1       completed
tasklane-worker     provider.summarize     1       success
tasklane-worker     provider.summarize     1       success
tasklane-worker     provider.summarize     1       success
tasklane-worker     stage write_artifacts  1       completed
database spans: 126
```
(Three provider calls: two chunks, then one combine.)

**10. The same in the Jaeger UI.** Open `http://127.0.0.1:16686/trace/<trace_id>` (paste the id
from step 8). The timeline shows `POST /v1/jobs` at the top, `job.create` under it, then
`outbox.publish` (dispatcher), `job.attempt` (worker) with its five stages, `provider.summarize`
inside *stage summarize*, and `db ...` spans. Click a span: its tags are ids, enums and timings
(`job_id`, `attempt_no`, `workflow_type`, `stage`, `outcome`, `provider`, `model`); `db.statement`
shows SQL with `%(...)s` placeholders, never values.

## (c) A failed attempt, a retry and a success: one story

**11. A job whose first provider call fails with a transient error.**
```powershell
$job = New-DemoJob "$demo\report.pdf" -Options @{ fault_injection = @{ mode = "transient"; failures = 1 } }; $done = Wait-DemoJob $job.id; $done.attempts | Format-Table attempt_no, trigger, outcome, error_class; "$($done.status) trace_id=$($done.trace_id)"
```
Expected:
```
attempt_no trigger    outcome          error_class
---------- -------    -------          -----------
         1 initial    failed_transient injected_transient
         2 auto_retry succeeded
succeeded trace_id=<32 hex digits>
```

**12. Both attempts are in the same trace.**
```powershell
Show-Trace $done.trace_id 2
```
Expected: one `job.create`, then `outbox.publish` → `job.attempt 1 retry_scheduled
injected_transient` (its *stage summarize* `failed`, its `provider.summarize` `failure`), then a
second `outbox.publish` → `job.attempt 2 succeeded` (the first three stages `reused`, then
summarize and write_artifacts `completed`). In Jaeger the second `outbox.publish` is drawn
*under* attempt 1, because attempt 1's failure is what scheduled it.

## (d) A failed job, from the dashboard to the logs

**13. A scanned PDF (no text: fails permanently).**
```powershell
$job = New-DemoJob "$demo\scanned.pdf"; $failed = Wait-DemoJob $job.id; "$($failed.status) $($failed.error_class) trace_id=$($failed.trace_id)"; "http://127.0.0.1:5173/jobs/$($failed.id)"
```
Expected: `failed pdf_no_text trace_id=<...>` and the job's dashboard URL.

**14. On the dashboard,** open that URL. *Why it failed* shows `pdf_no_text`; the *Attempts* table
shows attempt 1 `Failed (permanent)`; *Outputs* says outputs appear only for succeeded jobs. In
the facts, **Trace id** shows the same id with **Copy trace id** and **Open trace**. Click *Copy
trace id* (the button says "Copied"), then **Open trace**: Jaeger opens on this job's trace in a
new tab, with *stage extract_text* and *job.attempt* in red (`error_class = pdf_no_text`).

**15. Filter the logs by the trace id.**
```powershell
dc logs --no-log-prefix api dispatcher worker | Select-String $failed.trace_id
```
Expected: four lines, every one with `"trace_id": "<the same id>"`: the dispatcher's
`outbox message published`, the worker's `job claimed` (with `"queue_wait_seconds"`) and
`job attempt failed` (with `"error_class": "pdf_no_text"`, `"stage": "extract_text"`,
`"attempt_no": 1`, `"worker_id": "worker@..."`), and the API's `request` line for
`POST /v1/jobs`. The order of the lines can vary: `docker compose logs` mixes the services'
output.

## (e) Metrics, after

**16. What changed since step 7.**
```powershell
$after = Get-MetricMap; $after.Keys | Where-Object { $after[$_] -ne $before[$_] -and $_ -match 'jobs_created|jobs_finished|job_retries|queue_wait_seconds_count|workflow_runtime_seconds_count|provider_call_failures' } | Sort-Object | ForEach-Object { "{0}  {1} -> {2}" -f $_, $before[$_], $after[$_] }
```
Expected (your starting numbers differ): `tasklane_jobs_created_total{workflow_type="pdf_ingest"}`
+3; `tasklane_jobs_finished_total{status="succeeded",...}` +2 and `{status="failed",...}` +1;
`tasklane_job_retries_total{error_class="injected_transient",...}` +1;
`tasklane_queue_wait_seconds_count{workflow_type="pdf_ingest"}` +4 (four attempts);
`tasklane_workflow_runtime_seconds_count` +3 for `outcome="succeeded"`, +1 for
`failed_transient`, +1 for `failed_permanent`;
`tasklane_provider_call_failures_total{error_class="injected_transient",...}` +1.

**17. One histogram in full.**
```powershell
Get-DemoMetrics '^tasklane_stage_duration_seconds_(bucket|count|sum)\{.*stage="summarize",workflow_type="pdf_ingest"'
```
Expected: `_bucket` lines with cumulative counts per upper bound `le="0.05"` ... `le="900.0"`,
`le="+Inf"`, then `_count` and `_sum` (seconds). Every label is a workflow type or stage, never
an id. In Prometheus (http://127.0.0.1:9090, *Query*), try
`histogram_quantile(0.9, sum by (le, stage) (tasklane_stage_duration_seconds_bucket))`.

## (f) No worker running

**18. Stop the worker** (a warm shutdown: each process marks itself stopped).
```powershell
dc stop worker; (Invoke-RestMethod "$api/v1/overview").workers; Get-DemoMetrics '^tasklane_workers_alive'
```
Expected:
```
alive             : 0
last_seen_seconds : 2.65787
tasklane_workers_alive 0.0
```

**19. On the dashboard Overview** (http://127.0.0.1:5173) a red notice says **No worker is
running.** Queued jobs wait until a worker starts (one was last seen N s ago), and the Queue
section's *Workers* row says "No worker is running".

**20. Start it again.**
```powershell
dc start worker; $deadline = (Get-Date).AddSeconds(60); do { Start-Sleep -Seconds 2; $alive = (Invoke-RestMethod "$api/v1/overview").workers.alive } while ($alive -lt 2 -and (Get-Date) -lt $deadline); "alive=$alive"; Get-DemoMetrics '^tasklane_workers_alive'
```
Expected: `alive=2` and `tasklane_workers_alive 2.0`. The notice disappears from the Overview.

## (g) The viewer is down: nothing else changes

**21. Stop Jaeger, then run a job.**
```powershell
dc stop jaeger; $t0 = Get-Date; $job = New-DemoJob "$demo\report.pdf"; $done = Wait-DemoJob $job.id; "$($done.status) in $([int]((Get-Date) - $t0).TotalSeconds) s, trace_id=$($done.trace_id)"
```
Expected: `succeeded in 2 s` (about the same as in step 8). The trace id is
still recorded; only the export fails, in the background.

**22. The failed exports are logged, but at most once a minute per message.**
```powershell
Start-Sleep -Seconds 5; dc logs --since 2m --no-log-prefix api dispatcher worker | Select-String '"logger": "opentelemetry' | Select-Object -First 5; dc start jaeger
```
Expected: a few lines, at most one per message per process per minute (not one per second),
from each process that had spans to send (API, dispatcher, worker). Mostly
`{"level": "ERROR", "logger": "opentelemetry.exporter.otlp.proto.http.trace_exporter",
"message": "Failed to export spans batch due to timeout, max retries or shutdown."}`, sometimes
also a `"level": "WARNING"` line starting `"message": "Transient error HTTPConnectionPool(host='jaeger', port=4318)"`. Then
`Container tasklane-jaeger-1  Started`. (Spans of the job in step 21 were dropped; new jobs are
traced again.)

## (h) Redaction

**23. Log a line containing a fake API key and a database URL with a password.**
```powershell
dc exec -T api python -c "import logging; from app.observability.logging import configure_logging; configure_logging(); logging.getLogger('demo').warning('calling the provider with sk-demo-AbCdEfGhIjKlMnOp1234 via postgresql://tasklane:hunter2pass@db/x')"
```
Expected:
```
{"ts": "...", "level": "WARNING", "logger": "demo", "message": "calling the provider with [REDACTED] via postgresql://[REDACTED]@db/x"}
```

**24. The real database password is masked too** (it is registered at startup from
`POSTGRES_PASSWORD`).
```powershell
dc exec -T api python -c "import logging, os; from app.observability.logging import configure_logging; configure_logging(); logging.getLogger('demo').warning('password in a driver error: ' + os.environ['POSTGRES_PASSWORD'])"
```
Expected: `"message": "password in a driver error: [REDACTED]"`.

## Reset

**25. Remove the demo settings, stop the viewers, and recreate the backend containers without
them.**
```powershell
Remove-Item Env:OTEL_EXPORTER_OTLP_ENDPOINT, Env:TRACE_VIEWER_URL, Env:ENABLE_FAULT_INJECTION, Env:RETRY_BASE_SECONDS -ErrorAction SilentlyContinue; dc --profile observability stop jaeger prometheus; dc up -d; Start-Sleep -Seconds 10; (Invoke-RestMethod "$api/v1/meta").trace_viewer_url -eq $null
```
Expected: Docker recreates `api`, `dispatcher` and `worker`, then `True` (no trace viewer
configured; telemetry is back to "on, no exporter"). The viewer containers keep their data until
`dc --profile observability down`.

## Run the tests

```powershell
dc run --rm api pytest                       # all backend tests (unit, integration, e2e)
dc run --rm api pytest tests/integration/test_observability_e2e.py   # Phase 10 end to end
```
Frontend: from `frontend\` with Node 24, `npm test`, `npm run typecheck`, and `npm run e2e`
(needs the Phase 9 demo settings; see `WALKTHROUGH_PHASE9.md`, "Run the tests").
