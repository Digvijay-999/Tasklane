# Phase 9 walkthrough: the dashboard

Mostly browser steps. Windows PowerShell 5.1 for the few commands (one window, every path is a
variable, no `cd`). Demo settings are **shell variables only** (`.env` is never edited); step 29
removes them. Each expected view has a reference screenshot in `docs/screenshots/` (light and
dark versions: `<name>-light.png`, `<name>-dark.png`); times, ids and counts will differ.

Demo settings: fault injection on, the stub summarizer waits 1 s per call (jobs take a few
seconds, long enough to cancel or reload), lease 20 s and recovery every 2 s (a killed worker is
noticed within about 25 s), first retry after 1–2 s.

## Setup

**1. Paths and a short name for `docker compose`.**
```powershell
$repo = "D:\Projects\Tasklane"; $demo = "$env:USERPROFILE\tasklane-demo"; New-Item -ItemType Directory -Force $demo | Out-Null; function dc { docker compose -f "$repo\compose.yaml" @args }
```
Expected: no output.

**2. Start the stack with the demo settings** (builds the new `frontend` image the first time).
```powershell
$env:ENABLE_FAULT_INJECTION = "true"; $env:STUB_DELAY_SECONDS = "1"; $env:LEASE_SECONDS = "20"; $env:PROVIDER_TIMEOUT_SECONDS = "10"; $env:RECOVERY_INTERVAL_SECONDS = "2"; $env:RETRY_BASE_SECONDS = "2"; dc up --build -d
```
Expected: ends with lines like `Container tasklane-frontend-1  Started`. (PowerShell may print
Docker's progress in red; that is normal.)

**3. Sample files** (generated in code: a text PDF, a scanned PDF, a large PDF, a repository ZIP).
```powershell
dc run --rm -v "${demo}:/out" api python -m devtools.pdf_samples /out; dc run --rm -v "${demo}:/out" api python -m devtools.zip_samples /out
```
Expected: `wrote /out/report.pdf`, ..., `wrote /out/repo.zip`, ... The files are in
`%USERPROFILE%\tasklane-demo`.

## Overview

**4. Open http://127.0.0.1:5173.** The Overview: one row of counts per job state (each with its
own glyph and a label, zeros in a lighter gray), a Queue section (Database, Redis broker and
Dispatcher reachable; oldest queued job; messages waiting; scheduled retries) and Recent jobs.
In the top bar: "API ready". Screenshot: `overview-*.png`.

**5. The empty state.** Your database already has jobs, and emptying it would delete them, so
the empty Overview is shown in `overview-empty-*.png` (made with a mocked API by
`npm run screenshots`): "No jobs yet. Upload a PDF or a repository ZIP to start one" and a
**New job** button. You can see the same kind of empty state live in step 21.

## A PDF, live

**6. Click New job.** Two workflows with their limits read from the server (".pdf up to 50 MiB",
".zip up to 100 MiB"), a File row, and, because fault injection is on, a "Fault injection
(development only)" group. Screenshot: `new-job-*.png`.

**7. Validation before uploading.** Keep "PDF ingestion", choose `repo.zip` from
`%USERPROFILE%\tasklane-demo`. Expected, in red under the file: "This workflow needs a PDF file
(.pdf)." and the button stays disabled. Screenshot: `new-job-validation-*.png`.

**8. Choose `report.pdf` and click Upload and start job.** An upload progress bar flashes by
(the file is small; `new-job-uploading-*.png` shows it), then the job page opens.

**9. Watch the timeline.** Next to "Timeline": "● Live". One row, "Attempt 1 / First run", five
stage columns. Cells turn from a faint dot to a blue running cell (stage percent and seconds) to
a gray cell with a check, its duration and a bar showing its share of the time. Summarize takes
a few seconds. It ends with "✓ Succeeded in N s." and "Job finished. Live updates stopped."
Screenshots: `job-running-live-*.png` (during), `job-succeeded-pdf-*.png` (after).

**10. Download the summary.** Under Outputs, click **Download summary**: the browser saves
`summary.md`. Below it, "Summary preview" shows the same file as plain text (no rendered
Markdown, no links).

**11. The event history.** At the bottom: every stored event with its seq, time, attempt, type
and details. Tick "Show progress events": the hidden `stage.progress` rows appear. Type
`completed` in the filter box: only the `stage.completed` rows remain.

## Failures

**12. A scanned PDF.** New job, choose `scanned.pdf`, Upload and start job. Within a few seconds:
Extract text turns red with `pdf_no_text` and "permanent"; the final line says "Failed in Extract
text: pdf_no_text."; "Why it failed" shows the server's message (little or no extractable text,
scanned PDFs aren't supported). Screenshot: `job-failed-scanned-pdf-*.png`.

**13. Retry is disabled, with the server's reason.** At the top right, **Retry job** is grayed
out and the line under the buttons says "This job failed because of its input file, so retrying
would fail the same way. Upload a corrected file and create a new job." Only one attempt row:
permanent errors are not retried.

**14. A fault-injected job retries live.** New job, choose `report.pdf`, select "Fail the first
attempts (transient)" (Failing attempts: 1), Upload and start job. Watch: Summarize in attempt 1
turns red (`injected_transient`, "transient"); an amber line appears: "Retrying automatically in
1.x s after injected_transient, a transient error."; then "Attempt 2 / Automatic retry" with
**Reused** (dashed) in the first three columns (their checkpoints were verified, not re-run),
Summarize and Write artifacts run, and the job succeeds. The amber line now reads "Waited N s,
then retried automatically ...". The Attempts table shows "Failed, retryable" then "Succeeded".
Screenshot: `job-retried-fault-injection-*.png`.

## Cancel, kill, reload

**15. Cancel a running job.** New job, choose `large.pdf` (500 pages; it runs for minutes), Upload
and start job. When Summarize is running, click **Cancel job**. A dialog asks "Cancel this job?"
and explains the worker stops at the next stage boundary (`job-cancel-dialog-*.png`). Click
**Cancel job** in the dialog.

**16. It stops at a stage boundary.** A magenta line appears under the attempt: "Cancel requested
at ... The worker stops at the next stage boundary." The state shows "Cancelling" while Summarize
finishes (with `large.pdf` that takes a while), then "■ Cancelled before Write artifacts." and
Write artifacts shows "Not run". The Cancel button is now disabled with "The job has finished, so
there is nothing to cancel." Screenshot: `job-cancelled-*.png` (from a shorter job).

**17. Start a job to interrupt.** New job, choose `report.pdf`, Upload and start job. As soon as
Summarize is running, go straight to the next step.

**18. Kill the worker in the middle of the job.**
```powershell
dc kill worker; dc start worker
```
Expected: `Container tasklane-worker-1  Killed`, then `...  Started`.

**19. Watch recovery appear.** Back in the browser: the Summarize cell keeps its running look for
up to 20 s (the lease), and a "Worker not responding" notice may appear. Then attempt 1's
Summarize turns amber, "Worker lost"; the lines under it say "The worker stopped renewing its
lease (last heartbeat ...). Recovery took the job back at ..." and "Waited N s, then retried
automatically after lease_expired, a transient error."; "Attempt 2 / Recovery" reuses the three
finished stages and succeeds. Screenshot: `job-recovered-after-worker-kill-*.png`.

**20. Reload in the middle of a job.** Start another `report.pdf` job; while Summarize runs, press
**F5**. The page reloads the stored history, then reconnects from the last event it has. The
timeline continues and ends "Succeeded". Tick "Show progress events": the Seq column counts 1, 2,
3, ... to the end with no number missing or repeated.

## History, repository, themes

**21. Jobs.** Click **Jobs** in the top bar: every job, newest first, with state, file, workflow,
stage, progress, attempts, created time and duration (`jobs-*.png`). Click "PDF ingestion" and the
"Failed" and "Cancelled" chips: the list narrows and the address bar shows
`?workflow=pdf_ingest&status=failed&status=cancelled`. Press F5: the filters stay
(`jobs-filtered-*.png`). Now select only "Cancelling": "No jobs match these filters." with a
**Clear filters** button (`jobs-no-match-*.png`). Click it.

**22. A repository ZIP.** New job, select "Repository ZIP indexing", choose `repo.zip`, Upload and
start job. Six stage columns; it succeeds in about 10 s.

**23. The manifest viewer.** Scroll to Manifest: totals (24 files in the archive, 8 indexed, 16
skipped), Languages, Skip reasons (Excluded folder, Hidden file, Symbolic link, Nested archive,
Binary, Minified, ...), Indexed files and Skipped files. Type `node` in the Skipped files filter:
one row, `node_modules/left-pad/index.js`, "Excluded folder". Screenshot: `job-repo-manifest-*.png`.

**24. Untrusted names stay text.** In Skipped files, the very long `long/nnnn...` path wraps
inside its cell; nothing in the manifest is a link. (Hostile names with right-to-left overrides,
`javascript:` or HTML are covered by the unit tests in step 27.)

**25. Switch the theme.** In the top bar, change "System theme" to "Dark theme": every screen
switches; reload, and it stays dark. Choose "System theme" again to follow Windows.

**26. Live-update states you can't easily cause by hand** are captured in the screenshots:
`job-stream-reconnecting-*.png` ("Live updates paused, reconnecting…"),
`job-stream-limit-polling-*.png` ("The server's live-stream limit is reached. Checking for new
events every 2 s instead."), `overview-api-unreachable-*.png` (banner, "Not current: last
updated ...", values dimmed), `overview-loading-*.png` (skeletons) and
`new-job-replayed-*.png` (the server returned an existing job).

## Run the tests

**27. Dashboard unit and component tests** (Node 24 on Windows; `node_modules` is already
installed, otherwise run `npm --prefix "$repo\frontend" ci` first).
```powershell
npm --prefix "$repo\frontend" test
```
Expected: `Test Files  6 passed (6)`, `Tests  40 passed (40)`.

**28. End-to-end tests** against the running stack (they need the demo settings from step 2; the
first time also `npm --prefix "$repo\frontend" exec playwright install chromium`).
```powershell
npm --prefix "$repo\frontend" run e2e
```
Expected: `6 passed`. They build the dashboard, serve it on http://127.0.0.1:4173 and create jobs
in your development database.

## Reset, then the backend tests

**29. Remove the demo settings** and restart the containers with your `.env` values.
```powershell
"ENABLE_FAULT_INJECTION", "STUB_DELAY_SECONDS", "LEASE_SECONDS", "PROVIDER_TIMEOUT_SECONDS", "RECOVERY_INTERVAL_SECONDS", "RETRY_BASE_SECONDS" | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }; dc up -d
```
Expected: the api, dispatcher and worker containers are recreated. Check with
`dc exec -T worker printenv LEASE_SECONDS`: `60`. The dashboard's New job page no longer shows
the fault injection group.

**30. Backend tests** (all of them, about 8 minutes; they use their own test database). Run them
after the reset: Compose passes shell variables into the test container, and some tests expect
the default settings (for example, fault injection off).
```powershell
dc run --rm api pytest -q -p no:cacheprovider
```
Expected: last line `1326 passed, 1 warning in ...` (the warning is a Starlette deprecation
notice).
