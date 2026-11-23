# Phase 6 walkthrough: indexing repository ZIPs safely

For Windows PowerShell 5.1. Open a new PowerShell window and run the steps in order, in that
same window. Docker Desktop must be running. Your job ids and retry delays will differ from the
examples. Every step below was run, as written, twice in a row in Windows PowerShell 5.1.

## Setup

**1. Set the paths and the API address.** Every path is held in a variable, so you never need to `cd`.
```powershell
$repo = "D:\Projects\Tasklane"
$compose = "$repo\compose.yaml"
$demo = "$env:USERPROFILE\tasklane-demo"
$api = "http://127.0.0.1:8000"
```
Expected: no output.

**2. Define the helper functions.** They exist only in this PowerShell window. `Send-Upload` uploads a sample ZIP from the demo folder; `New-RepoJob` creates a `repo_index` job for an upload (JSON goes through a file, because PowerShell 5.1 mangles quotes in inline JSON); `Wait-TasklaneJob` waits until a job has finished; `Invoke-RepoJob` does all three; `Save-Artifacts` downloads a job's artifacts; `Get-Manifest` fetches its manifest; `Get-StorageSnapshot` lists every file and folder under `/data` in the worker container (the storage volume and its parent); `Show-TasklaneEvents` prints a job's events (`backend/devtools/job_events.py`); `Wait-TasklaneReady` waits for the API and the worker.
```powershell
function Send-Upload($Zip) {
    curl.exe -s -X POST "$api/v1/uploads?filename=$Zip" -H "Content-Type: application/zip" --data-binary "@$demo\$Zip" | ConvertFrom-Json
}
function New-RepoJob($UploadId, $Options) {
    $body = @{ workflow_type = "repo_index"; upload_id = $UploadId }
    if ($Options) { $body.options = $Options }
    $body | ConvertTo-Json -Depth 5 -Compress | Set-Content -Encoding ascii "$demo\job.json"
    $key = "demo-" + [guid]::NewGuid()
    curl.exe -s -X POST "$api/v1/jobs" -H "Content-Type: application/json" -H "Idempotency-Key: $key" --data-binary "@$demo\job.json" | ConvertFrom-Json
}
function Wait-TasklaneJob($JobId) {
    $deadline = (Get-Date).AddSeconds(120)
    do {
        Start-Sleep -Milliseconds 500
        $job = curl.exe -s "$api/v1/jobs/$JobId" | ConvertFrom-Json
    } while (($job.status -notin @("succeeded", "failed", "cancelled")) -and ((Get-Date) -lt $deadline))
    $job
}
function Invoke-RepoJob($Zip, $Options) {
    $upload = Send-Upload $Zip
    $job = New-RepoJob $upload.id $Options
    Wait-TasklaneJob $job.id
}
function Save-Artifacts($Job, $Folder) {
    New-Item -ItemType Directory -Force $Folder | Out-Null
    foreach ($a in $Job.artifacts) { curl.exe -s -o "$Folder\$($a.filename)" "$api$($a.download_url)" }
    Get-ChildItem $Folder | Format-Table Name, Length
}
function Get-Manifest($Job) {
    $url = ($Job.artifacts | Where-Object artifact_type -eq "manifest").download_url
    curl.exe -s "$api$url" | ConvertFrom-Json
}
function Get-StorageSnapshot {
    docker compose -f $compose exec -T worker sh -c "cd /data && find . -type f -printf 'file %p %s\n' -o -printf '%y %p\n' | sort"
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

**3. Turn on fault injection for this PowerShell window only.** Needed only for scenario (f). Compose reads this shell variable instead of `.env`, and only in this window; step 27 removes it.
```powershell
$env:ENABLE_FAULT_INJECTION = "true"
```
Expected: no output.

**4. Start the stack, or update it.** Compose recreates the containers whose settings changed (Phase 6 added the `ZIP_*`, `REPO_*` and `STUB_DELAY_SECONDS` settings, and step 3 turned on fault injection). There is no new migration.
```powershell
docker compose -f $compose up -d
```
Expected (order and wording vary; Docker prints these lines in red, which is normal):

```
 Container tasklane-redis-1 Running
 Container tasklane-postgres-1 Running
 Container tasklane-migrate-1 Recreated
 Container tasklane-dispatcher-1 Recreated
 Container tasklane-api-1 Recreated
 Container tasklane-worker-1 Recreated
 Container tasklane-postgres-1 Healthy
 Container tasklane-migrate-1 Exited
 Container tasklane-worker-1 Started
 Container tasklane-api-1 Started
 Container tasklane-dispatcher-1 Started
```

**5. Wait until the API and the worker are ready.**
```powershell
Wait-TasklaneReady
```
Expected:

```
api and worker are ready
```

**6. Create the folder for the samples.** `tasklane-demo` in your user folder.
```powershell
New-Item -ItemType Directory -Force $demo | Out-Null
```
Expected: no output.

**7. Generate the sample ZIPs into it.** `backend/devtools/zip_samples.py` builds them in code; nothing binary is stored in the repository.
```powershell
docker compose -f $compose run --rm -v "${demo}:/out" api python -m devtools.zip_samples /out
```
Expected (after Docker's container lines):

```
wrote /out/repo.zip
wrote /out/zipslip.zip
wrote /out/symlink.zip
wrote /out/bomb.zip
wrote /out/encrypted.zip
wrote /out/corrupt.zip
wrote /out/nested.zip
wrote /out/empty_repo.zip
```

## (a) A realistic repository is indexed

**8. Index repo.zip.** Uploads it, creates the job, and waits until it has finished (a few seconds). The repository has sources, tests and a README, plus files that must be skipped for every reason: `node_modules/`, `.git/`, a binary, a 6 MiB file, minified files, a symlink, a lockfile, `.env`, ...
```powershell
$jobA = Invoke-RepoJob "repo.zip"
```
Expected: no output.

**9. Show the result.**
```powershell
$jobA | Format-List status, current_stage, progress_percent, attempt_count
```
Expected:

```
status           : succeeded
current_stage    : write_artifacts
progress_percent : 100
attempt_count    : 1
```

**10. Download all three artifacts.** Into `tasklane-demo\a`.
```powershell
Save-Artifacts $jobA "$demo\a"
```
Expected:

```
Name          Length
----          ------
chunks.jsonl    2880
manifest.json   5828
summary.md      4107
```

**11. Show how many files were skipped for each reason.** From `manifest.json`. All 14 skip reasons occur in this repository.
```powershell
(Get-Content "$demo\a\manifest.json" -Raw | ConvertFrom-Json).skip_reasons
```
Expected:

```
binary           : 1
empty            : 1
excluded_dir     : 2
hidden           : 1
lockfile         : 1
minified         : 2
nested_archive   : 1
path_too_deep    : 1
path_too_long    : 1
special_file     : 1
symlink          : 1
system_file      : 1
too_large        : 1
unsupported_type : 1
```

**12. Show every skipped file with its reason.** The long name is cut to the 240-character limit, and PowerShell shortens it further with `...`.
```powershell
(Get-Content "$demo\a\manifest.json" -Raw | ConvertFrom-Json).skipped | Format-Table reason, path
```
Expected:

```
reason           path
------           ----
system_file      .DS_Store
hidden           .env
excluded_dir     .git/HEAD
nested_archive   assets/fixtures.zip
unsupported_type assets/logo.png
too_large        data/huge.txt
path_too_deep    deep/d/d/d/d/d/d/d/d/d/d/d/d/d/d/d/d/d/d/d/d/file.py
binary           docs/notes.txt
path_too_long    long/nnnnnnnnnnnnnnnnnnnn...
excluded_dir     node_modules/left-pad/index.js
lockfile         package-lock.json
empty            src/shop/__init__.py
symlink          src/shop/current.py
special_file     src/shop/queue.py
minified         src/web/app.min.js
minified         src/web/widget.js
```

**13. Show the top of summary.md.** Title, counts, and the notice that the stub provider (not an AI model) wrote the summaries. Open the whole file in an editor to see the overview and one section per file.
```powershell
Get-Content "$demo\a\summary.md" -TotalCount 5
```
Expected:

```
# Repository summary of repo.zip

8 files indexed, 16 skipped; 47 lines in 8 chunks. Summarized by `stub` / `stub-extractive-v1`.

> This summary was produced by the built-in stub provider, which extracts leading sentences and frequent terms. It is a placeholder, not an AI-generated summary.
```

## (b) A zip-slip archive is rejected, and nothing escapes

**14. Upload zipslip.zip (no job yet).** It contains an entry named `../../evil.txt`. Uploading only stores the file under a generated name; nothing reads the archive yet.
```powershell
$slip = Send-Upload "zipslip.zip"
```
Expected: no output.

**15. Take a snapshot of everything in the storage volume's parent folder.** Taken after the upload and before the job runs.
```powershell
$before = Get-StorageSnapshot
```
Expected: no output.

**16. Create the job and wait for it.**
```powershell
$jobB = Wait-TasklaneJob (New-RepoJob $slip.id).id
```
Expected: no output.

**17. Show the outcome.** The whole archive is rejected after one attempt, with a fixed message that never repeats names from inside the archive.
```powershell
$jobB | Format-List status, error_class, error_summary, attempt_count
```
Expected:

```
status        : failed
error_class   : zip_unsafe_path
error_summary : The archive contains an entry with an unsafe or conflicting name (for example an absolute path, a '..' segment, a drive letter, a backslash, or two names that differ only in letter case), so the whole archive was rejected.
attempt_count : 1
```

**18. Compare the snapshot with the current state.** The only difference is the job's own, empty work folder (your job id differs). Nothing else was created, changed or deleted anywhere under `/data`.
```powershell
Compare-Object $before (Get-StorageSnapshot)
```
Expected:

```
InputObject                                           SideIndicator
-----------                                           -------------
d ./storage/work/af6c2aa1-83b2-469b-ba63-45e59e27d7d7 =>
```

**19. Search the whole worker container for evil.txt.** Counts the files named `evil.txt` anywhere in the container.
```powershell
docker compose -f $compose exec -T worker sh -c "find / -name evil.txt -not -path '/proc/*' 2>/dev/null | wc -l"
```
Expected:

```
0
```

## (c) A symlink is skipped, never followed

**20. Index symlink.zip.** `src/shop/settings.py` in this archive is a symlink to `../../../secret.txt`. Prints the job's status.
```powershell
($jobC = Invoke-RepoJob "symlink.zip").status
```
Expected:

```
succeeded
```

**21. Show the skipped symlink.** It was detected from the entry's Unix mode bits: never created, never followed, its target never read.
```powershell
(Get-Manifest $jobC).skipped | Where-Object reason -eq "symlink" | Format-Table reason, path
```
Expected:

```
reason  path
------  ----
symlink src/shop/settings.py
```

## (d) A zip bomb is rejected

**22. Index bomb.zip.** `bomb.zip` is about 2 KB, but one entry holds 2 MiB of zeros (about 1000:1). It is rejected before that entry is decompressed.
```powershell
Invoke-RepoJob "bomb.zip" | Format-List status, error_class, error_summary, attempt_count
```
Expected:

```
status        : failed
error_class   : zip_bomb
error_summary : The archive is compressed more than 100:1, which is how zip bombs work, so it was not extracted.
attempt_count : 1
```

## (e) Encrypted, corrupt and empty archives fail at once, without retrying

**23. Index encrypted.zip, corrupt.zip and empty_repo.zip.** One after another (a few seconds). Each fails permanently after one attempt, although 3 were allowed (`max_attempts`).
```powershell
"encrypted.zip", "corrupt.zip", "empty_repo.zip" | ForEach-Object { Invoke-RepoJob $_ } | Format-List status, error_class, error_summary, attempt_count, max_attempts
```
Expected:

```
status        : failed
error_class   : zip_encrypted
error_summary : The archive contains password-protected entries. Upload an archive without encryption.
attempt_count : 1
max_attempts  : 3

status        : failed
error_class   : zip_corrupt
error_summary : The archive is damaged or is not a valid ZIP file, so it could not be read.
attempt_count : 1
max_attempts  : 3

status        : failed
error_class   : repo_no_sources
error_summary : None of the 4 files in the archive can be indexed (unsupported file types, excluded folders, binary, empty, or too large files).
attempt_count : 1
max_attempts  : 3
```

## (f) A transient failure is retried and resumes

**24. Index repo.zip with one injected transient failure.** The first summarizer call of attempt 1 fails with `injected_transient`; the job waits 2.5–5 s in `retry_wait` and runs again. Takes about 10 seconds; prints the final status.
```powershell
($jobF = Invoke-RepoJob "repo.zip" @{ fault_injection = @{ mode = "transient"; failures = 1 } }).status
```
Expected:

```
succeeded
```

**25. Show its attempts.**
```powershell
$jobF.attempts | Format-Table attempt_no, trigger, outcome, error_class
```
Expected:

```
attempt_no trigger    outcome          error_class
---------- -------    -------          -----------
         1 initial    failed_transient injected_transient
         2 auto_retry succeeded
```

**26. Show the stage events of both attempts.** Attempt 2 reuses the verified outputs of the four stages that attempt 1 finished (`stage.skipped ... checkpoint_reused`) and runs only `summarize` and `write_artifacts`. Columns: event number, attempt, event, details. Your retry delay differs.
```powershell
Show-TasklaneEvents $jobF.id | Select-String "stage\.(started|failed|skipped)|retry_wait"
```
Expected:

```
   4        1  stage.started         validate_archive
   7        1  stage.started         extract_files
  21        1  stage.started         filter_files
  38        1  stage.started         chunk_sources
  41        1  stage.started         summarize
  42        1  stage.failed          summarize: injected_transient (retryable)
  43        1  job.status_changed    running -> retry_wait  (retry in 3.50 s)  [injected_transient]
  44        2  job.status_changed    retry_wait -> running
  46        2  stage.skipped         validate_archive (checkpoint_reused)
  47        2  stage.skipped         extract_files (checkpoint_reused)
  48        2  stage.skipped         filter_files (checkpoint_reused)
  49        2  stage.skipped         chunk_sources (checkpoint_reused)
  50        2  stage.started         summarize
  62        2  stage.started         write_artifacts
```

## Clean up

**27. Turn fault injection off again.** Removes the shell variable from step 3.
```powershell
Remove-Item Env:ENABLE_FAULT_INJECTION
```
Expected: no output.

**28. Recreate the API and worker without it.** Compose sees the setting change back to `false`.
```powershell
docker compose -f $compose up -d
```
Expected: the same kind of lines as step 4, ending with `Started`.
