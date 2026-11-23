# Phase 8 walkthrough: the live event stream (SSE) with replay

Windows PowerShell 5.1, one window, from any folder (every path is a variable; no `cd`).
The stack must be running with the new image: `docker compose up --build -d` in the repo (the
API's start command changed in this phase). Demo settings are **shell variables only** (`.env` is
never edited); the last step removes them again.

Demo settings: the stub summarizer waits 2 s per call (so you can watch a job), keepalive comments
every 5 s, at most **2** open streams (to see the limit), a 10 s lease with recovery every 2 s (to
watch a killed worker being recovered), retries after 1-2 s, and fault injection on.

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
function New-DemoJob($file, [int]$Faults = 0) {
    $upload = curl.exe -s -X POST "$api/v1/uploads?filename=$(Split-Path $file -Leaf)" -H "Content-Type: application/pdf" --data-binary "@$file" | ConvertFrom-Json
    $body = @{ workflow_type = "pdf_ingest"; upload_id = $upload.id }
    if ($Faults) { $body.options = @{ fault_injection = @{ mode = "transient"; failures = $Faults } } }
    $body | ConvertTo-Json -Depth 5 -Compress | Set-Content -Encoding ascii "$demo\job.json"
    (curl.exe -s -X POST "$api/v1/jobs" -H "Content-Type: application/json" -H "Idempotency-Key: demo-$([guid]::NewGuid())" --data-binary "@$demo\job.json" | ConvertFrom-Json).id
}
function Watch-Events($id) { dc exec -T api python -m devtools.sse_client "http://localhost:8000/v1/jobs/$id/events" @args; "exit code $LASTEXITCODE" }
function Get-LastId($lines) { [regex]::Match(($lines -join "`n"), "last id (\d+)").Groups[1].Value }
function Show-History($id, $after = 0, $limit = 4) {
    $page = curl.exe -s "$api/v1/jobs/$id/events/history?after=$after&limit=$limit" | ConvertFrom-Json
    $page.items | ForEach-Object { "{0,4}  {1}" -f $_.seq, $_.event_type }
    "next_after=$($page.next_after)  last_seq=$($page.last_seq)"
}
function Wait-Stage($id, $stage) {
    $deadline = (Get-Date).AddSeconds(60)
    while (($job = Get-DemoJob $id).current_stage -ne $stage) {
        if ($job.status -in "succeeded", "failed", "cancelled") { throw "job already $($job.status) before reaching ${stage}: start a new job and wait in the same paste" }
        if ((Get-Date) -gt $deadline) { throw "no $stage within 60 s (status $($job.status))" }
        Start-Sleep -Milliseconds 200
    }
}
function Reset-Demo {
    "STUB_DELAY_SECONDS", "SSE_KEEPALIVE_SECONDS", "SSE_MAX_STREAMS", "LEASE_SECONDS", "PROVIDER_TIMEOUT_SECONDS", "RECOVERY_INTERVAL_SECONDS", "RETRY_BASE_SECONDS", "ENABLE_FAULT_INJECTION" | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
    Get-Job | Remove-Job -Force
    dc up -d worker dispatcher api
}
```
Expected: no output.

**3. Demo settings, for this PowerShell window only.**
```powershell
$env:STUB_DELAY_SECONDS = "2"; $env:SSE_KEEPALIVE_SECONDS = "5"; $env:SSE_MAX_STREAMS = "2"; $env:LEASE_SECONDS = "10"; $env:PROVIDER_TIMEOUT_SECONDS = "5"; $env:RECOVERY_INTERVAL_SECONDS = "2"; $env:RETRY_BASE_SECONDS = "2"; $env:ENABLE_FAULT_INJECTION = "true"
```
Expected: no output.

**4. Restart the backend containers with those settings** (Compose reads shell variables
before `.env`).
```powershell
dc up -d worker dispatcher api
```
Expected (among the lines): `Container tasklane-worker-1  Started`, `...dispatcher-1  Started`,
`...api-1  Started` (or `Running` if they already had these settings). Compose may also re-run the
one-shot `migrate` service first (`migrate-1  Exited`); that is harmless.

**5. Sample PDFs.**
```powershell
dc run --rm -v "${demo}:/out" api python -m devtools.pdf_samples /out
```
Expected: `wrote /out/report.pdf` ... `wrote /out/large.pdf`.

## (a) Watch a PDF job live with curl.exe

**6. Start a job and follow its stream.** `-N` makes curl print each event as it arrives. Stages
stream in over about 10 seconds, then the server ends the stream by itself.
```powershell
$a = New-DemoJob "$demo\report.pdf"; curl.exe -s -N "$api/v1/jobs/$a/events"
```
Expected: the raw stream, one block per event, arriving in bursts as the stages run (the
summarize stage takes about 6 s). Ids, timestamps and the worker name differ for you:
```text
retry: 3000

id: 1
event: job.created
data: {"seq":1,"event_type":"job.created","attempt_no":null,"created_at":"2026-11-10T08:28:13.702532Z","payload":{"upload_id":"69fcbd59-...","workflow_type":"pdf_ingest"}}

id: 2
event: job.status_changed
data: {"seq":2,"event_type":"job.status_changed","attempt_no":1,"created_at":"...","payload":{"to":"running","from":"queued","worker_id":"worker@0ae749ffcabb:31","attempt_no":1}}
...
id: 17
event: stage.started
data: {"seq":17,...,"payload":{"stage":"summarize"}}

id: 18
event: stage.progress
data: {"seq":18,...,"payload":{"stage":"summarize","percent":33}}
...
id: 26
event: job.status_changed
data: {"seq":26,...,"payload":{"to":"succeeded","from":"running","attempt_no":1}}

event: stream.end
data: {"reason":"job_finished","status":"succeeded","last_seq":26}
```
and then the prompt returns: the server closed the stream. Every event is three lines (`id`,
`event`, one line of JSON) plus a blank line; `stream.end` has no `id`.

## (b) The raw SSE format, with headers

**7. The last two events of the finished job, with the response headers** (`-i`).
```powershell
curl.exe -s -i "$api/v1/jobs/$a/events?after=24"
```
Expected:
```text
HTTP/1.1 200 OK
date: ...
server: uvicorn
cache-control: no-cache
x-accel-buffering: no
connection: keep-alive
content-type: text/event-stream; charset=utf-8
x-request-id: ...
transfer-encoding: chunked

retry: 3000

id: 25
event: stage.completed
data: {"seq":25,"event_type":"stage.completed",...,"payload":{"stage":"write_artifacts","duration_ms":17}}

id: 26
event: job.status_changed
data: {"seq":26,"event_type":"job.status_changed",...,"payload":{"to":"succeeded","from":"running","attempt_no":1}}

event: stream.end
data: {"reason":"job_finished","status":"succeeded","last_seq":26}
```
`?after=24` means "everything after event 24".

## (c) Disconnect, then resume with Last-Event-ID, then with ?after=

**8. New job; read 5 events, then hang up** (`--stop-after 5` closes the connection; `--all`
shows progress events too, so the numbers are consecutive).
```powershell
$c = New-DemoJob "$demo\report.pdf"; $out = Watch-Events $c --all --stop-after 5; $out; $last = Get-LastId $out
```
Expected:
```text
 seq  attempt  event                  details
   1        -  job.created
   2        1  job.status_changed     queued -> running
   3        1  attempt.started        trigger=initial
   4        1  stage.started          validate_pdf
   5        1  stage.progress         validate_pdf 100%
disconnected after 5 events; last id 5 (resume with --last-event-id 5)
exit code 2
```

**9. Stay away 3 seconds (the job keeps running), then resume with the `Last-Event-ID` header**
and hang up again after 5 events.
```powershell
Start-Sleep 3; $out = Watch-Events $c --all --last-event-id $last --stop-after 5; $out; $last = Get-LastId $out
```
Expected: exactly the next five, starting at 6 (nothing repeated, nothing skipped):
```text
 seq  attempt  event                  details
   6        1  stage.completed        validate_pdf
   7        1  stage.started          extract_text
   8        1  stage.progress         extract_text 25%
   9        1  stage.progress         extract_text 50%
  10        1  stage.progress         extract_text 75%
disconnected after 5 events; last id 10 (resume with --last-event-id 10)
exit code 2
```

**10. Resume the rest with `?after=`** (what a reloaded browser page uses).
```powershell
Watch-Events $c --all --after $last
```
Expected: 11 through 26 (some replayed, the rest live), then the end:
```text
 seq  attempt  event                  details
  11        1  stage.progress         extract_text 100%
  ...
  17        1  stage.started          summarize
  18        1  stage.progress         summarize 33%
  ...
  26        1  job.status_changed     running -> succeeded
stream.end: job_finished (status succeeded, last seq 26); the server closed the stream
exit code 0
```

## (d) A job that already finished

**11. Full replay, then `stream.end`, straight away** (progress events hidden by default).
```powershell
Watch-Events $a
```
Expected: everything at once (seq numbers jump where progress events are hidden):
```text
 seq  attempt  event                  details
   1        -  job.created
   2        1  job.status_changed     queued -> running
   3        1  attempt.started        trigger=initial
   4        1  stage.started          validate_pdf
   6        1  stage.completed        validate_pdf
  ...
  25        1  stage.completed        write_artifacts
  26        1  job.status_changed     running -> succeeded
stream.end: job_finished (status succeeded, last seq 26); the server closed the stream
exit code 0
```

## (e) The history route, page by page

**12. First page of 4.**
```powershell
Show-History $a 0 4
```
Expected:
```text
   1  job.created
   2  job.status_changed
   3  attempt.started
   4  stage.started
next_after=4  last_seq=26
```

**13. Next page: pass `next_after` as `after`.**
```powershell
Show-History $a 4 4
```
Expected:
```text
   5  stage.progress
   6  stage.completed
   7  stage.started
   8  stage.progress
next_after=8  last_seq=26
```

**14. The last page.**
```powershell
Show-History $a 24 4
```
Expected: `next_after` is empty (null): you have everything.
```text
  25  stage.completed
  26  job.status_changed
next_after=  last_seq=26
```

## (f) Retries, live

**15. A job whose first two summarizer calls fail** (fault injection), followed live.
```powershell
$f = New-DemoJob "$demo\report.pdf" -Faults 2; Watch-Events $f
```
Expected, over about 20 s (the retry delays vary):
```text
 seq  attempt  event                  details
   ...
  17        1  stage.started          summarize
  18        1  stage.failed           summarize: injected_transient (retryable)
  19        1  job.status_changed     running -> retry_wait  (retry in 1.77 s)  [injected_transient]
  20        2  job.status_changed     retry_wait -> running
  21        2  attempt.started        trigger=auto_retry
  22        2  stage.skipped          validate_pdf (checkpoint_reused)
  23        2  stage.skipped          extract_text (checkpoint_reused)
  24        2  stage.skipped          chunk_document (checkpoint_reused)
  25        2  stage.started          summarize
  26        2  stage.failed           summarize: injected_transient (retryable)
  27        2  job.status_changed     running -> retry_wait  (retry in 3.67 s)  [injected_transient]
  28        3  job.status_changed     retry_wait -> running
  29        3  attempt.started        trigger=auto_retry
  ...
  42        3  job.status_changed     running -> succeeded
stream.end: job_finished (status succeeded, last seq 42); the server closed the stream
exit code 0
```

## (g) The stream limit (2 in this demo)

**16. Open two streams in the background** (each gives up after 8 s by itself).
```powershell
$g = New-DemoJob "$demo\report.pdf"; $url = "$api/v1/jobs/$g/events"; $streams = 1..2 | ForEach-Object { Start-Job { curl.exe -s -N --max-time 8 $using:url } }; Start-Sleep 2; "background streams: $(($streams | ForEach-Object State) -join ', ')"
```
Expected: `background streams: Running, Running`

**17. A third stream is refused.**
```powershell
curl.exe -s -w "`nHTTP %{http_code}`n" $url
```
Expected:
```text
{"error":{"code":"stream_limit_reached","message":"Too many event streams are open. Try again shortly, or poll the history route.","details":{"limit":2},"request_id":"..."}}
HTTP 503
```

**18. Once the two background streams have ended, there is room again.**
```powershell
$streams | Wait-Job | Remove-Job; curl.exe -s -o NUL --max-time 2 -w "HTTP %{http_code}`n" "${url}?after=999"
```
Expected: `HTTP 200` (the job has finished by now, so this stream ends at once with
`stream.end`).

## (h) Kill the worker mid-job while streaming

**19. Start a job, stream it in the background, and kill the worker process during the
summarize stage** (`kill -9`: no clean shutdown). One paste: the whole job takes only about 9 s,
and summarize runs from about 3 s to 9 s after the job is created, so a pause between creating the
job and waiting for summarize would miss it.
```powershell
$h = New-DemoJob "$demo\report.pdf"; $composeFile = "$repo\compose.yaml"; $hUrl = "http://localhost:8000/v1/jobs/$h/events"; $watch = Start-Job { docker compose -f $using:composeFile exec -T api python -m devtools.sse_client $using:hUrl }; Wait-Stage $h summarize; $p = (Get-DemoJob $h).lease.worker_id.Split(":")[-1]; dc exec -T worker sh -c "kill -9 $p"; "killed worker process $p during summarize"
```
Expected: `killed worker process 31 during summarize` (your number differs). Celery starts a
replacement worker process by itself. The stream is being collected in the background.

**20. Watch the stream** (what it printed so far, then live until `stream.end`). The lease
(10 s) runs out, recovery closes the attempt, and the job runs again.
```powershell
Receive-Job $watch -Wait -AutoRemoveJob
```
Expected, finishing about 20 s after the kill:
```text
 seq  attempt  event                  details
   ...
  17        1  stage.started          summarize
  18        1  attempt.lease_expired  worker worker@0ae749ffcabb:31 last seen 2026-11-10T08:28:57.281798+00:00
  19        1  job.status_changed     running -> retry_wait  (retry in 1.22 s)  [lease_expired]
  20        2  job.status_changed     retry_wait -> running
  21        2  attempt.started        trigger=recovery
  22        2  stage.skipped          validate_pdf (checkpoint_reused)
  23        2  stage.skipped          extract_text (checkpoint_reused)
  24        2  stage.skipped          chunk_document (checkpoint_reused)
  25        2  stage.started          summarize
  30        2  stage.completed        summarize
  31        2  stage.started          write_artifacts
  33        2  stage.completed        write_artifacts
  34        2  job.status_changed     running -> succeeded
stream.end: job_finished (status succeeded, last seq 34); the server closed the stream
```
Nobody restarted the stream: the same connection showed the crash, the recovery and the
success.

## Reset

**21. Remove the demo settings and restart the backend with your normal `.env` values.**
```powershell
Reset-Demo
```
Expected (among the lines): `Container tasklane-api-1  Recreated`, `...worker-1  Recreated`,
`...dispatcher-1  Recreated`, then `Started` for each. Compose may also re-run the one-shot
`migrate` service first (`migrate-1  Exited`); that is harmless. `$env:SSE_MAX_STREAMS` and the
other demo variables are gone (`Get-ChildItem Env:SSE*` prints nothing).

