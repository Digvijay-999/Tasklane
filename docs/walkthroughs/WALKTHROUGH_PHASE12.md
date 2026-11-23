# Walkthrough: Phase 12 (retention, recovery demo, benchmark, README)

Fourteen steps, about 15 minutes. Windows PowerShell 5.1 (or PowerShell 7), from the repository
folder, with Docker Desktop running. Every command was run twice on this machine before this file
was finished. Your job ids and timings will differ.

**Before you start:** if another project uses ports 8000 or 5173, stop it first. Open a **new**
PowerShell window, so no demo settings from earlier walkthroughs are left in the session.

**1. Start the stack with the default settings.**

```powershell
docker compose up --build -d
docker compose exec -T worker printenv STUB_DELAY_SECONDS CLEANUP_ENABLED DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS
while (-not (curl.exe -s http://127.0.0.1:8000/health/ready)) { Start-Sleep -Seconds 2 }
curl.exe -s http://127.0.0.1:8000/health/ready
```

Expected: `0`, `false`, `60`, then (once the API has restarted, which the loop waits for)
`{"status":"ready","checks":{"database":"ok","queue":"ok"}}`.

**2. Remember how many jobs there are.**

```powershell
$api = "http://127.0.0.1:8000"
(curl.exe -s "$api/v1/overview" | ConvertFrom-Json).total
```

**3. Cleanup dry run with the default retention.** Nothing is changed.

```powershell
docker compose run --rm api python -m app.maintenance.cleanup --dry-run > "$env:TEMP\cleanup-dry-run.txt"
$LASTEXITCODE
Get-Content "$env:TEMP\cleanup-dry-run.txt" | Select-String "DRY RUN|^\s+\d+  |would be removed"
```

Expected: `0`, then `Tasklane cleanup (DRY RUN: nothing was changed)`, five row counts and
`Files and folders that would be removed: N (size)`. On a database that has been in use for a
while you see expired uploads, published outbox rows older than 24 hours, and `work/` folders of
jobs that finished more than 7 days ago; on a fresh one, zeros. `Get-Content "$env:TEMP\cleanup-dry-run.txt"`
shows every id and path, each with its reason and size.

**4. Dry run of "delete every finished job now".** Still nothing is changed.

```powershell
docker compose run --rm -e RETENTION_FINISHED_JOB_DAYS=0 api python -m app.maintenance.cleanup --dry-run | Select-String "^\s+\d+  finished jobs"
(curl.exe -s "$api/v1/overview" | ConvertFrom-Json).total
```

Expected: the first line counts every succeeded, failed and cancelled job; the total is the same
number as in step 2.

**5. Real cleanup with the default retention.**

```powershell
docker compose run --rm api python -m app.maintenance.cleanup | Select-String "^\s+\d+  |were removed"
$LASTEXITCODE
```

Expected: the same counts and the same number of files and bytes as the dry run in step 3
(`Files and folders that were removed: ...`), and exit code `0`.

**6. Run it again: nothing is left to do.**

```powershell
docker compose run --rm api python -m app.maintenance.cleanup | Select-String "^\s+\d+  |were removed"
```

Expected: five zeros and `Files and folders that were removed: 0 (0 bytes)`.

**7. Old jobs are still readable.** The cleanup removed old uploads and intermediate files, never
a job younger than 30 days or its artifacts.

```powershell
$old = (curl.exe -s "$api/v1/jobs?limit=100&status=succeeded" | ConvertFrom-Json).items[-1]
"$($old.created_at) $($old.upload_filename)"
$detail = curl.exe -s "$api/v1/jobs/$($old.id)" | ConvertFrom-Json
foreach ($a in $detail.artifacts) { curl.exe -s -o NUL -w "$($a.artifact_type): %{http_code}`n" "$api$($a.download_url)" }
```

Expected: the job's date and original filename, then `200` for each artifact.

**8. The scheduled cleanup loop.**

```powershell
$env:CLEANUP_ENABLED = "true"; docker compose up -d dispatcher
docker compose logs dispatcher | Select-String '"cleanup started"' | Select-Object -Last 1
```

Expected (wait a few seconds and repeat the second line if it prints nothing): a JSON log line
with `"message": "cleanup started", "interval_seconds": 3600.0`. Turn it off again:

```powershell
Remove-Item Env:CLEANUP_ENABLED; docker compose up -d dispatcher
docker compose exec -T dispatcher printenv CLEANUP_ENABLED
```

Expected: `false`.

**9. The failure and recovery demo.** About two minutes.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\demo-recovery.ps1
```

Expected, in order: `Job ... is queued`; `Job is running, stage summarize, 50% done` (or a bit
more); the kill, with "The job is still visible in PostgreSQL: running"; then after roughly 70–100
seconds `Job ended succeeded ... after 2 attempts.` with `attempt 1: lease_expired` and
`attempt 2: succeeded`. The event list shows `attempt.lease_expired`, `running -> retry_wait
(retry in 2.5–5 s) [lease_expired]`, `attempt.started trigger=recovery`, three
`stage.skipped ... (checkpoint_reused)`, and `running -> succeeded`. The script ends with
"Restoring the worker's settings".

**10. See the same story in the dashboard.** Open the link the script printed
(`http://127.0.0.1:5173/jobs/<id>`): the timeline has two attempt rows; attempt 1 ends in
"Worker lost" during summarize, the note between the rows says "The worker stopped renewing its
lease (last heartbeat ...). Recovery took the job back at ...", and attempt 2 shows the first
three stages as reused.

**11. The worker is back to your settings.**

```powershell
docker compose exec -T worker printenv STUB_DELAY_SECONDS
```

Expected: `0`.

**12. The benchmark.** About half a minute with the default settings.

```powershell
docker compose exec -T api python -m devtools.benchmark --jobs 20
```

Expected: the machine line (CPU model, CPUs and memory as Docker sees them), the settings
(`PROVIDER=stub`, `STUB_DELAY_SECONDS=0`, `WORKER_CONCURRENCY=2`, ...), and a table with
`20 succeeded` for each workflow. Compare with [docs/performance.md](../performance.md): queue
wait grows with the queue (40 jobs shared by 2 worker processes), while the workflow duration per
job stays around a second; your numbers will differ.

**13. The README's diagram.** After you push (Claude Code never pushes), open the repository on
GitHub: the "Architecture" section shows a rendered diagram (boxes for Dashboard, API,
PostgreSQL, Local storage, Redis, Worker and a "Dispatcher process" group with the outbox
dispatcher, recovery loop and cleanup loop), not a code block. Click through the
"Documentation" table: every link opens a file.

**14. CI.** On the same push, the repository's Actions tab shows the "CI" workflow with two jobs,
`backend` and `frontend`, both green.

