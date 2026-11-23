# Failure and recovery demo: submit a job, kill the worker in the middle of it, and print how
# Tasklane recovered, from the job's stored events.
#
#   powershell -ExecutionPolicy Bypass -File scripts\demo-recovery.ps1
#
# Needs the stack running (docker compose up -d) on http://127.0.0.1:8000. Works in Windows
# PowerShell 5.1 and PowerShell 7. It restarts the worker twice: once with STUB_DELAY_SECONDS=1
# (so the job runs long enough to be interrupted), and at the end with your own settings again.
# It takes about two minutes, most of it waiting for the killed worker's 60-second lease to run
# out: that is how recovery knows a worker is gone.

$ErrorActionPreference = "Stop"
$api = "http://127.0.0.1:8000"
$repo = Split-Path -Parent $PSScriptRoot
$samples = Join-Path $env:TEMP "tasklane-demo"
$previousDelay = $env:STUB_DELAY_SECONDS

function Say($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
# Docker writes progress to stderr. Windows PowerShell 5.1 turns redirected stderr lines into
# errors, which $ErrorActionPreference = "Stop" would make fatal, so run it with "Continue" and
# check the exit code instead.
function Invoke-Docker {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & docker @args 2>&1 | Out-Null } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "docker $args failed (exit code $LASTEXITCODE)" }
}
function Get-Job($id) { Invoke-RestMethod -Uri "$api/v1/jobs/$id" }
function Wait-Until($id, $what, [scriptblock]$done, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        $job = Get-Job $id
        if (& $done $job) { return $job }
        Start-Sleep -Milliseconds 500
    }
    throw "Timed out waiting for: $what"
}

Push-Location $repo
try {
    Say "Checking the stack"
    $ready = Invoke-RestMethod -Uri "$api/health/ready"
    Write-Host "API ready: database $($ready.checks.database), queue $($ready.checks.queue)"

    Say "Restarting the worker with STUB_DELAY_SECONDS=1 (each summarization call takes 1 s)"
    $env:STUB_DELAY_SECONDS = "1"
    Invoke-Docker compose up -d worker

    Say "Generating a 40-page sample PDF"
    New-Item -ItemType Directory -Force $samples | Out-Null
    Invoke-Docker compose run --rm -v "${samples}:/out" api python -m devtools.pdf_samples /out
    $pdf = Join-Path $samples "medium.pdf"

    Say "Uploading it and creating a pdf_ingest job"
    $upload = Invoke-RestMethod -Method Post -Uri "$api/v1/uploads?filename=medium.pdf" `
        -ContentType "application/pdf" -InFile $pdf
    $body = @{ workflow_type = "pdf_ingest"; upload_id = $upload.id } | ConvertTo-Json -Compress
    $job = Invoke-RestMethod -Method Post -Uri "$api/v1/jobs" -ContentType "application/json" `
        -Headers @{ "Idempotency-Key" = "demo-recovery-$([guid]::NewGuid())" } -Body $body
    $id = $job.id
    Write-Host "Job $id is $($job.status)"

    Say "Waiting until the worker is in the middle of the summarize stage"
    $job = Wait-Until $id "summarize stage" { param($j) $j.current_stage -eq "summarize" -and $j.progress_percent -ge 50 } 180
    Write-Host "Job is $($job.status), stage $($job.current_stage), $($job.progress_percent)% done"

    Say "Killing the worker container (SIGKILL: no clean shutdown, like a crash or power cut)"
    Invoke-Docker compose kill worker
    $killedAt = Get-Date
    $job = Get-Job $id
    Write-Host "The job is still visible in PostgreSQL: $($job.status), stage $($job.current_stage)"
    Write-Host "Its attempt holds a lease until $($job.lease.lease_expires_at); nobody renews it now."

    Say "Starting a new worker"
    Invoke-Docker compose start worker

    Say "Waiting for recovery (the lease runs out, then the job is retried)"
    $job = Wait-Until $id "job to finish" { param($j) @("succeeded", "failed", "cancelled") -contains $j.status } 240
    $took = [int]((Get-Date) - $killedAt).TotalSeconds
    Write-Host "Job ended $($job.status) $took s after the worker was killed, after $($job.attempt_count) attempts."
    foreach ($attempt in $job.attempts) {
        Write-Host ("  attempt {0}: {1} (worker {2})" -f $attempt.attempt_no, $attempt.outcome, $attempt.worker_id)
    }

    Say "The recovery story, from the job's stored events (progress events hidden)"
    docker compose exec -T api python -m devtools.job_events $id

    Write-Host ""
    Write-Host "Read it as: attempt 1 started and its stages ran; the worker died; recovery found the"
    Write-Host "expired lease (attempt.lease_expired), put the job in retry_wait with a backoff, and"
    Write-Host "attempt 2 skipped the stages whose checkpoints still verified (stage.skipped) and"
    Write-Host "finished the job. Open it in the dashboard: http://127.0.0.1:5173/jobs/$id"
}
finally {
    Say "Restoring the worker's settings"
    if ($null -eq $previousDelay) { Remove-Item Env:STUB_DELAY_SECONDS -ErrorAction SilentlyContinue }
    else { $env:STUB_DELAY_SECONDS = $previousDelay }
    Invoke-Docker compose up -d worker
    Pop-Location
}
