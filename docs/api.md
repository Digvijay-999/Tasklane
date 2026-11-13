# Tasklane HTTP API

> **Status (Phase 8):** the routes marked **implemented** below work. `pdf_ingest` and
> `repo_index` jobs really process their input (with the built-in **stub** summarizer, not an AI
> model) and produce downloadable artifacts; see
> [What a pdf_ingest job does](#what-a-pdf_ingest-job-does) and
> [What a repo_index job does](#what-a-repo_index-job-does). Transient failures are retried
> automatically with backoff, resuming after the stages that already finished; see
> [Failures and retries](#failures-and-retries). A job whose worker dies is recovered once its
> lease expires; jobs can be cancelled and failed jobs retried by hand
> ([cancel](#post-v1jobsjob_idcancel--implemented), [retry](#post-v1jobsjob_idretry--implemented)).
> A job's events can be followed live, with replay after a dropped connection
> ([Events](#events)).
> Routes marked **planned** don't exist yet.

| Route | Status |
|---|---|
| `GET /health/live`, `GET /health/ready` | implemented (Phase 1) |
| `POST /v1/uploads` | implemented |
| `POST /v1/jobs` | implemented |
| `GET /v1/jobs` | implemented |
| `GET /v1/jobs/{job_id}` | implemented |
| `POST /v1/jobs/{job_id}/cancel`, `POST /v1/jobs/{job_id}/retry` | implemented (Phase 7) |
| `GET /v1/jobs/{job_id}/events` (SSE), `GET /v1/jobs/{job_id}/events/history` | implemented (Phase 8) |
| `GET /v1/jobs/{job_id}/artifacts/{artifact_id}` | implemented (Phase 5) |
| `GET /metrics` | planned (Phase 10) |

Interactive docs generated from the code: http://127.0.0.1:8000/docs

## Conventions

- Base URL for local development: `http://127.0.0.1:8000`. Versioned routes live under `/v1`.
- Request and response bodies are JSON, except the upload body, which is the raw file.
- Timestamps are ISO 8601 in UTC (`2026-11-03T12:30:45.123456Z`). IDs are UUIDs.
- Every response carries an `X-Request-ID` header. Send your own (letters, digits, `.`, `_`,
  `-`, up to 64 characters) to correlate with your logs; otherwise the server generates one.
- JSON request bodies are limited to 64 KiB (`MAX_JSON_BODY_BYTES`); larger ones get
  `413 request_too_large`.

### PowerShell examples

The examples are for **Windows PowerShell** and call **`curl.exe`** explicitly (plain `curl` in
PowerShell 5.1 is an alias for `Invoke-WebRequest`, which takes different arguments). They assume:

```powershell
$api = "http://127.0.0.1:8000"
```

JSON bodies are written to a file first and sent with `--data-binary "@file.json"`: Windows
PowerShell 5.1 strips the double quotes out of JSON passed inline to native programs, so inline
`-d '{"a":1}'` arrives broken. Run the commands from the folder that holds the files.

## Errors

Every error, on every route, has this shape:

```json
{
  "error": {
    "code": "upload_too_large",
    "message": "The file exceeds the 50 MB limit for PDF uploads.",
    "details": {"limit_bytes": 52428800},
    "request_id": "3f2a9c0e5b7d4e1f8a6b2c3d4e5f6a7b"
  }
}
```

- `code` is stable and meant for programs; `message` is safe to show to people.
- `details` holds extra structured information (may be empty).
- `request_id` equals the `X-Request-ID` header; the server logs use the same id.
- Messages never include internal details (stack traces, SQL, file paths, submitted values).

| Status | `code` | When |
|---|---|---|
| 400 | `idempotency_key_missing` | `POST /v1/jobs` without an `Idempotency-Key` header |
| 400 | `idempotency_key_invalid` | The key doesn't match the required format |
| 400 | `invalid_cursor` | A malformed cursor, or one reused with different filters |
| 400 | `empty_file` | The upload body is empty |
| 400 | `upload_interrupted` | The client disconnected mid-upload |
| 400 | `malformed_body` | The request body couldn't be read or parsed at all |
| 404 | `job_not_found` | No job with that id (also for ids that aren't UUIDs) |
| 404 | `artifact_not_found` | No artifact with that id *for that job* (also for ids that aren't UUIDs) |
| 404 | `not_found` | No route matches the URL |
| 405 | `method_not_allowed` | Wrong HTTP method for the URL |
| 409 | `idempotency_key_reused` | The key was used before with a *different* request |
| 409 | `job_not_retryable` | `POST .../retry` on a job that can't be retried; `details.reason` is `not_failed`, `input_error` or `retry_limit_reached`, `details.status` the job's status |
| 410 | `artifact_gone` | The artifact is recorded, but its file is missing or no longer matches the recorded size |
| 413 | `upload_too_large` | The file exceeds the limit for its type |
| 413 | `request_too_large` | A JSON body exceeds 64 KiB |
| 415 | `unsupported_file_type` | The filename doesn't end in `.pdf` or `.zip` |
| 415 | `content_type_mismatch` | The `Content-Type` header doesn't fit the file type |
| 415 | `file_signature_mismatch` | The file's first bytes don't match its type |
| 422 | `invalid_request` | Request validation failed (including invalid JSON); `details.fields` lists each problem |
| 422 | `invalid_filename` | The filename is empty, too long, or has control characters, line breaks, or invisible text-direction characters |
| 422 | `upload_not_found` | `upload_id` doesn't exist |
| 422 | `upload_not_ready` | The upload isn't `ready` (`details.upload_status` says why) |
| 422 | `upload_expired` | The upload is past its `expires_at` |
| 422 | `upload_kind_mismatch` | e.g. a ZIP upload for `pdf_ingest` |
| 422 | `fault_injection_disabled` | The job asks for `fault_injection`, but the server has `ENABLE_FAULT_INJECTION` off |
| 500 | `internal_error` | An unexpected server error (details are only in the logs) |
| 503 | `service_unavailable` | The database can't be reached, or all its connections are busy |

`invalid_request` details look like:
`{"fields": [{"location": "query", "field": "limit", "issue": "Input should be less than or equal to 100"}]}`.
The submitted value isn't echoed back: messages from the validator that would quote part of the
input (for example for an invalid UUID) are replaced with fixed text.

## Uploads

### `POST /v1/uploads` — implemented

Uploads a PDF or ZIP. The request body is the **raw file**, not a multipart form, so the server
can check the size and file signature while the bytes arrive.

| Part | Value |
|---|---|
| Query `filename` | Required. The original filename, **for display only**: it is never used in a storage path. Must end in `.pdf` or `.zip` (any case), 1–255 characters, with no control characters, line breaks, or invisible text-direction characters (which could make `x\u202Egpj.pdf` display as `xfdp.jpg`). |
| Header `Content-Type` | Required. PDF: `application/pdf`. ZIP: `application/zip`, `application/x-zip-compressed`, or `application/octet-stream`. |
| Body | The file bytes. |

```powershell
curl.exe -s -X POST "$api/v1/uploads?filename=report.pdf" -H "Content-Type: application/pdf" --data-binary "@report.pdf"
curl.exe -s -X POST "$api/v1/uploads?filename=repo.zip" -H "Content-Type: application/zip" --data-binary "@repo.zip"
```

Use `--data-binary "@file"`. Plain `-d @file` strips newlines and corrupts binary files, and without
`-H "Content-Type: ..."` curl sends `application/x-www-form-urlencoded`, which is rejected with
`415 content_type_mismatch`.

**`201 Created`:**

```json
{
  "id": "9cb43d9b-5d6d-4963-b5f1-c1f7dc29646e",
  "original_filename": "report.pdf",
  "kind": "pdf",
  "content_type": "application/pdf",
  "size_bytes": 18234,
  "sha256": "14bcd090baf31edba64e9cbd8cdfc15f943344aa72cb3675ad8e91bfcbce03ad",
  "status": "ready",
  "created_at": "2026-11-03T12:00:00.000000Z",
  "expires_at": "2026-11-04T12:00:00.000000Z"
}
```

`content_type` is the type proven by the file signature, not the one you declared.

**What is checked now:**

1. The filename's extension and the declared `Content-Type` (before any body is read).
2. Size: PDF up to **50 MiB** (`MAX_PDF_UPLOAD_BYTES`), ZIP up to **100 MiB**
   (`MAX_ZIP_UPLOAD_BYTES`). The limit is enforced on the bytes actually received, so a missing or
   wrong `Content-Length` can't bypass it. A `Content-Length` that already exceeds the limit is
   rejected immediately, before anything is stored.
3. The file signature: a PDF must start with `%PDF-`; a ZIP must start with `PK\x03\x04` (empty
   or split archives are rejected). A mismatch is detected from the first bytes and the server
   stops reading.
4. The file isn't empty. Its SHA-256 is computed while streaming.

**Not checked yet:** ZIP contents (path traversal, symlinks, decompression bombs, file counts,
nested archives) are checked in Phase 6, when ZIPs are first opened. PDF structure, encryption,
and scanned PDFs are checked in Phase 5. Until then, an accepted ZIP is only known to *start* like
a ZIP.

**Lifecycle (`status`):**

| Status | Meaning |
|---|---|
| `receiving` | The row is created (and committed) before the body is read. The file streams into a temporary path. |
| `ready` | The whole file arrived, passed the checks, and was renamed atomically into its final path. Only `ready` uploads can be used for jobs. |
| `rejected` | A check failed or the upload was interrupted. Its files are deleted; `rejection_reason` records why (`too_large`, `signature_mismatch`, `empty_file`, `client_disconnected`, `internal_error`). |
| `deleted` | (Phase 12) The file was removed by retention cleanup; the row stays because jobs reference it. |

If the server **crashes mid-upload**, the row stays `receiving` and a partial file may remain. Such
an upload can never be used for a job. Cleanup of these leftovers comes with retention in
Phase 12.

Uploads expire after **24 hours** (`UPLOAD_TTL_HOURS`): after that they can't be used for *new*
jobs. Identical files uploaded twice are two separate uploads.

**Where files are stored:** in the `storage` Docker volume, at
`uploads/{upload_id}/source.pdf` (or `.zip`). The original filename is never part of a path.
To look at them:

```powershell
docker compose exec api find /data/storage -type f
docker compose cp "api:/data/storage/uploads/<upload_id>/source.pdf" .\copy.pdf
```

`docker compose down -v` deletes this volume, and with it every uploaded file (and the database).
Plain `docker compose down` keeps both.

## Jobs

### `POST /v1/jobs` — implemented

Creates a job for an upload. The job is written together with its first event (`job.created`) and
an outbox message in **one transaction**: either all three exist or none do. The job is returned
`queued`; within about a second the dispatcher publishes it and a worker picks it up (follow it
with `GET /v1/jobs/{job_id}`). If Redis is down, it stays `queued` until Redis is back.

**Headers:** `Content-Type: application/json` and **`Idempotency-Key`** (required).

**Body:**

| Field | Required | Notes |
|---|---|---|
| `workflow_type` | yes | `pdf_ingest` (needs a PDF upload) or `repo_index` (needs a ZIP upload) |
| `upload_id` | yes | A `ready`, unexpired upload of the matching kind |
| `options` | no | Workflow-specific options; unknown fields are rejected. Both workflows: only `fault_injection` (development only, see [Fault injection](#fault-injection-development-only)). Options that are off are not stored. |

```powershell
$upload = curl.exe -s -X POST "$api/v1/uploads?filename=report.pdf" -H "Content-Type: application/pdf" --data-binary "@report.pdf" | ConvertFrom-Json
@{ workflow_type = "pdf_ingest"; upload_id = $upload.id } | ConvertTo-Json -Compress | Set-Content -Encoding ascii job.json
$key = "demo-" + [guid]::NewGuid()
curl.exe -i -X POST "$api/v1/jobs" -H "Content-Type: application/json" -H "Idempotency-Key: $key" --data-binary "@job.json"
```

**Responses:**

| Status | Headers | Meaning |
|---|---|---|
| `201 Created` | `Location: /v1/jobs/{id}`, `Idempotent-Replayed: false` | A new job was created |
| `200 OK` | `Idempotent-Replayed: true` | Replay: this key was already used for the *same* request; the existing job is returned (in its current state) and nothing new is written |
| `409 Conflict` | | `idempotency_key_reused`: the key was used for a *different* request; `details.job_id` names the existing job |

Both `201` and `200` return the job (see [Job object](#job-object)).

#### Idempotency keys

Send a fresh `Idempotency-Key` for each new job, and **the same key when retrying** a request
whose response you didn't get (timeout, dropped connection). The retry returns the original job
instead of creating a second one.

- **Format:** 16–128 characters of letters, digits, and `.` `_` `:` `-`. A UUID works well.
  Missing → `400 idempotency_key_missing`; malformed → `400 idempotency_key_invalid`.
- **Scope:** one key space for the whole (single-user) service.
- **Retention:** a key lives **as long as its job row**. There is no separate expiry; when
  retention cleanup (Phase 12) deletes a job, its key becomes usable again.
- A request rejected by validation (e.g. `422 upload_not_found`) **doesn't use up** its key: fix
  the request and retry with the same key.
- A replay doesn't re-check the upload, so retrying returns the original job even if the upload
  has expired since.
- **Concurrent requests** with the same key are safe: the database's unique index on the key
  decides the winner (`INSERT ... ON CONFLICT DO NOTHING`), the others wait for it and then get
  the replay (or `409`). Exactly one job is created.

#### How "same request" is decided

The server fingerprints the request with SHA-256 over a canonical JSON form of **the fields you
actually sent**:

- Key order and whitespace don't matter; UUIDs are compared case-insensitively.
- Defaults are **not** filled in before hashing, so if a future version adds an option with a
  default, replays of older requests still match.
- Consequence: **sending a field explicitly with its default value and omitting it count as
  different requests.** For example, `{"workflow_type": "pdf_ingest", "upload_id": "..."}` and the
  same body plus `"options": {}` are different; reusing one's key for the other gets `409`.
- The job itself stores the *resolved* options, with defaults filled in.

#### Using one upload for several jobs

Allowed. Uploads are immutable inputs, so the same file can be processed again (for example
after a failure, or later with different options). Duplicate *submissions* are prevented by the
idempotency key, not by the upload.

### `GET /v1/jobs` — implemented

Lists jobs, **newest first**, with cursor pagination.

| Query | Notes |
|---|---|
| `status` | Optional, repeatable: `queued`, `running`, `retry_wait`, `cancel_requested`, `succeeded`, `failed`, `cancelled` |
| `workflow_type` | Optional, repeatable: `pdf_ingest`, `repo_index` |
| `limit` | 1–100, default 20 |
| `cursor` | The `next_cursor` from the previous page |

```powershell
curl.exe -s "$api/v1/jobs?limit=5"
curl.exe -s "$api/v1/jobs?status=queued&status=failed&workflow_type=pdf_ingest"
$page = curl.exe -s "$api/v1/jobs?limit=2" | ConvertFrom-Json
curl.exe -s "$api/v1/jobs?limit=2&cursor=$($page.next_cursor)"
```

**Response:** `{"items": [<job>, ...], "next_cursor": "..." | null}`. `next_cursor` is `null` on
the last page.

**Why a cursor instead of page numbers:** with `?page=2` (offset paging), every job created
between your requests pushes rows down a page, so you'd see some jobs twice and miss others, and
the database has to read and discard every skipped row. A cursor means "continue after this
job": it stays correct while new jobs arrive, and an index jumps straight to it. Jobs are ordered
by `created_at`, then `id` to break ties.

The cursor is opaque. It is tied to the filters it was created with: reusing it with different
`status`/`workflow_type` filters, or sending a damaged one, returns `400 invalid_cursor`. There is
no total count.

### `GET /v1/jobs/{job_id}` — implemented

```powershell
curl.exe -s "$api/v1/jobs/<job_id>"
```

Returns the [job object](#job-object) plus:

- `upload`: `id`, `original_filename`, `kind`, `size_bytes`, `sha256`, `status`.
- `attempts`: one entry per execution attempt (`attempt_no`, `trigger`, `worker_id`, `task_id`,
  `started_at`, `heartbeat_at`, `lease_expires_at`, `ended_at`, `outcome`, `error_class`,
  `error_summary`). Filled in once a worker claims the job.
- `artifacts`: the job's outputs (`id`, `artifact_type`, `attempt_no`, `filename`,
  `size_bytes`, `content_type`, `sha256`, `created_at`, `download_url`). A succeeded
  `pdf_ingest` job has two: `summary` (`summary.md`) and `manifest` (`manifest.json`). A
  succeeded `repo_index` job has three: `summary`, `chunks` (`chunks.jsonl`) and `manifest`.
  Artifacts are registered in the same database transaction that marks the job `succeeded`, so
  they are listed only for succeeded jobs: never while the job is running, and never for a
  failed one.

- `lease` (Phase 7): while a worker holds the job, `{attempt_no, worker_id, heartbeat_at,
  lease_expires_at, expired}`; otherwise `null`. The worker's heartbeat renews the lease every
  quarter lease (15 s by default). `expired: true` (judged by the database clock) means the
  worker stopped renewing it and recovery is about to retry or fail the job.
- `can_cancel` / `cancel_blocked_reason`: whether `POST .../cancel` would change anything; when
  not, `already_cancel_requested` or `already_finished`.
- `can_retry` / `retry_blocked_reason`: whether `POST .../retry` would be accepted; when not,
  `not_failed`, `input_error` or `retry_limit_reached`. Both pairs are decided by the server's
  state machine, so clients never have to guess from the status.

An unknown id, or one that isn't a UUID, returns `404 job_not_found`.

### `POST /v1/jobs/{job_id}/cancel` — implemented

```powershell
curl.exe -s -X POST "$api/v1/jobs/<job_id>/cancel"
```

No body. Always `200` with the job detail (as `GET /v1/jobs/{job_id}`) as it is after the request:

| Status before | Effect | `status` after |
|---|---|---|
| `queued`, `retry_wait` | Cancelled at once; the job's unsent queue message is deleted. A message already sent is ignored by the worker. | `cancelled` |
| `running` | The worker finishes the stage it is in, then stops before the next one (its attempt ends with outcome `cancelled`). | `cancel_requested`, later `cancelled` |
| `cancel_requested`, `succeeded`, `failed`, `cancelled` | Nothing changes (repeating a cancel is safe). | unchanged |

A job in `cancel_requested` can still end `succeeded` (its last stage finished before the worker
saw the request) or `failed` (a permanent error came first); a `job.cancel_superseded` event
then explains why. `404 job_not_found` for an unknown id.

### `POST /v1/jobs/{job_id}/retry` — implemented

```powershell
curl.exe -s -X POST "$api/v1/jobs/<job_id>/retry"
```

No body. Reopens a `failed` job: `200` with the job detail, now `queued` on the **same job id**,
with:

- a fresh budget of 3 automatic attempts (`max_attempts = attempt_count + 3`; at most 20 in
  total), `manual_retry_count + 1`;
- a clean run: `error_class`, `error_summary`, `finished_at`, `current_stage`, `retry_at` and
  `cancel_requested_at` cleared, `progress_percent` 0 (the old values stay in `attempts` and the
  events);
- a `job.manual_retry` event; the next attempt has trigger `manual_retry`;
- resume: stages whose checkpoints still verify are skipped (`stage.skipped`), as for an automatic
  retry. The backoff of later automatic retries starts over (first retry 2.5–5 s).

Refusals are `409 job_not_retryable` with `details.reason`:

| `reason` | When |
|---|---|
| `not_failed` | The job isn't `failed` (also the answer to a second retry request: the first already queued it) |
| `input_error` | It failed because of its input (e.g. `pdf_no_text`, `zip_unsafe_path`); upload a corrected file and create a new job |
| `retry_limit_reached` | Another 3 attempts would pass the limit of 20 attempts per job |

`404 job_not_found` for an unknown id.

### `GET /v1/jobs/{job_id}/artifacts/{artifact_id}` — implemented

Downloads one artifact file. Use the `download_url` from the job detail.

```powershell
$detail = curl.exe -s "$api/v1/jobs/<job_id>" | ConvertFrom-Json
$summary = $detail.artifacts | Where-Object artifact_type -eq "summary"
curl.exe -s -o summary.md "$api$($summary.download_url)"
```

- `200` with the file. Headers: `Content-Type` as recorded (`text/markdown; charset=utf-8`,
  `application/json`, or `application/x-ndjson` for `chunks.jsonl`), `Content-Disposition: attachment; filename="summary.md"`,
  `X-Content-Type-Options: nosniff` (browsers must not guess another type),
  `ETag: "<sha256>"`, `Cache-Control: private, no-cache`.
- `404 artifact_not_found`: unknown id, or an artifact that belongs to a *different* job.
- `410 artifact_gone`: the database row exists but the file is missing or its size changed
  (for example, it was deleted by hand inside the `storage` volume).

### What a pdf_ingest job does

The job runs five stages in order. `current_stage` and `progress_percent` show where it is;
each stage gets a fixed share of the progress bar.

| Stage | Share | What it does |
|---|---|---|
| `validate_pdf` | 5 % | Checks the upload file is still there and unchanged (SHA-256), then opens the PDF in a separate, limited process and counts its pages |
| `extract_text` | 30 % | Extracts the text of every page (in the same kind of limited process), normalizes it, and decides whether there is enough text |
| `chunk_document` | 5 % | Splits the text into overlapping chunks (target 4,000 characters, at most 6,000, 400 overlap) |
| `summarize` | 55 % | Summarizes each chunk, then combines the chunk summaries (map-reduce) |
| `write_artifacts` | 5 % | Writes `summary.md` and `manifest.json` and records them as artifacts |

**The summarizer is a stub.** With `PROVIDER=stub` (the only option until Phase 11) "summaries"
are the first sentences of each chunk plus frequent key terms. It needs no API key and gives the
same output for the same input every time. `summary.md` says so at the top. Every summarizer
call is recorded (provider, model, duration, success, sizes) in the `provider_calls` table.

**Limits** (settings in `.env`):

| Setting | Default | Over the limit |
|---|---|---|
| `PDF_MAX_PAGES` | 500 | `pdf_too_many_pages` |
| `PDF_MAX_TEXT_CHARS` | 2,000,000 | `pdf_text_too_large` (never silently truncated) |
| `PDF_VALIDATE_TIMEOUT_SECONDS` / `PDF_EXTRACT_TIMEOUT_SECONDS` | 60 / 300 | `pdf_too_complex` |
| `PDF_PARSER_MEMORY_MB` | 1024 | `pdf_too_complex` |
| `SUMMARIZE_TIMEOUT_SECONDS` | 900 | `stage_timeout` |
| `PROVIDER_TIMEOUT_SECONDS` | 20 (must be at most half of `LEASE_SECONDS`) | — |

Inconsistent values (for example a text limit too large for `PDF_PARSER_OUTPUT_MAX_MB`, or chunk
overlap not smaller than the chunk target) stop the services at startup with a message naming
the setting, instead of failing jobs later.

### What a repo_index job does

The upload must be a ZIP of a source repository. The job runs six stages; nothing from the
archive is ever executed, imported, or built. Threat model and defenses:
[security.md](security.md).

| Stage | Share | What it does |
|---|---|---|
| `validate_archive` | 5 % | Checks the upload is unchanged (SHA-256), then reads the ZIP's table of contents in a separate, limited process: rejects encrypted or damaged archives, too many entries, and any unsafe or conflicting name. Nothing is decompressed yet |
| `extract_files` | 15 % | In the same kind of limited process, decompresses the files that could be indexed into one generated file, counting the real bytes (never trusting the sizes written in the ZIP) and stopping at the limits |
| `filter_files` | 5 % | Decides, for every file, whether it is indexed or skipped and why |
| `chunk_sources` | 5 % | Splits each file into chunks of whole lines (same sizes as PDFs: target 4,000 characters, at most 6,000, 400 overlap) |
| `summarize` | 65 % | Summarizes every chunk, combines each file's chunk summaries into a file summary, then combines the file summaries into a repository overview (map-reduce) |
| `write_artifacts` | 5 % | Writes `summary.md`, `chunks.jsonl` and `manifest.json` and records them as artifacts |

**Which files are indexed.** Only an allowlist: known text and code extensions (`.py`, `.js`,
`.ts`, `.java`, `.go`, `.rs`, `.c`, `.md`, `.json`, `.yaml`, `.toml`, `.sql`, `.html`, `.css`, and
about 60 more, see `backend/app/workflows/repo/filters.py`) plus the names `README`, `LICENSE`,
`Makefile` and `Dockerfile`. Every other file is skipped with one reason:

| Reason | Meaning |
|---|---|
| `excluded_dir` | Inside a folder from `REPO_EXCLUDED_DIRS` (default: `.git`, `node_modules`, `dist`, `build`, `target`, `__pycache__`, `.venv`, `venv`, `.idea`, `.vscode`, `coverage`, `vendor`, and similar), at any depth. These are never decompressed |
| `hidden` | A file or folder name starting with `.` (for example `.env`, which may hold secrets) |
| `system_file` | `__MACOSX/`, `.DS_Store`, `Thumbs.db`, `desktop.ini` |
| `nested_archive` | `.zip`, `.tar`, `.gz`, `.7z`, `.rar`, `.jar`, `.whl`, ...: never extracted |
| `lockfile` | `package-lock.json`, `yarn.lock`, `poetry.lock`, `uv.lock`, `Cargo.lock`, `go.sum`, ... |
| `minified` | `*.min.js`-style names, or JS/TS/CSS/JSON/HTML whose lines average over 250 characters |
| `unsupported_type` | Not on the allowlist (images, executables, unknown extensions) |
| `too_large` | Over `ZIP_MAX_FILE_BYTES` (5 MiB), by its declared size (then never decompressed) or while decompressing |
| `binary` | A NUL byte in the first 8 KiB, or more than 10 % invalid UTF-8 |
| `empty` | No visible characters |
| `symlink` | A symbolic link entry: never created, never followed, its target never read |
| `special_file` | A device, pipe or socket entry |
| `path_too_long` / `path_too_deep` | More than 240 characters / more than 20 path components |

**Artifacts.**

- `summary.md`: the overview, then one section per indexed file (language, lines, chunks, and
  its summary), then a table of skip reasons. File names are escaped (Markdown punctuation, and
  invisible characters such as right-to-left overrides shown as their code, e.g. `\u202e`);
  summaries are in fenced blocks they can't break out of, so they render as plain text. The
  stub-provider notice is at the top.
- `chunks.jsonl`: one JSON object per line: `chunk_id` (deterministic, same scheme as PDFs),
  `path`, `line_start`, `line_end`, `sha256` (of `text`), `text`. ASCII-only JSON.
- `manifest.json` (schema `tasklane.repo_manifest/1`): `input` (upload, archive entries,
  folders), `totals` (files, included, skipped, bytes, lines), `skip_reasons` (count per
  reason), `languages` (files, lines, bytes per language), `included` (path, language, bytes,
  lines, sha256 per file), `skipped` (path and reason per file), `chunking`, `summary`
  (provider and model), `usage` (provider calls, tokens if reported), `limits` (every limit
  that applied), and `artifacts`. No timestamps: the same input gives the same bytes.
  ASCII-only JSON.

**Limits** (settings in `.env`; sizes are real decompressed bytes):

| Setting | Default | Over the limit |
|---|---|---|
| `ZIP_MAX_ENTRIES` | 5,000 (at most 100,000) | `zip_too_many_files` |
| `ZIP_MAX_TOTAL_BYTES` | 500 MiB (of the files that are extracted) | `zip_too_large` |
| `ZIP_MAX_FILE_BYTES` | 5 MiB | file skipped as `too_large` |
| `ZIP_MAX_COMPRESSION_RATIO` | 100 (per entry and overall, once past `ZIP_RATIO_CHECK_MIN_BYTES`, 1 MiB) | `zip_bomb` |
| `ZIP_MAX_PATH_LENGTH` / `ZIP_MAX_PATH_DEPTH` | 240 / 20 | file skipped |
| `ZIP_VALIDATE_TIMEOUT_SECONDS` / `ZIP_EXTRACT_TIMEOUT_SECONDS` | 60 / 300 | `zip_too_complex` |
| `ZIP_SANDBOX_MEMORY_MB` | 512 | `zip_too_complex` |
| `REPO_MAX_PROVIDER_CALLS` | 1,000 | `repo_too_large` (checked before the first call) |

#### Failures and retries

When an attempt fails, `error_class` says why and `error_summary` holds a fixed, safe message
(never a stack trace, a file path, text from the PDF, or a name from inside a ZIP). Each class is either:

- **transient**, meaning it might work if tried again. While attempts remain (3 per job), the job
  goes to `retry_wait` with `retry_at` set, and is retried automatically after a backoff. The
  backoff uses equal jitter over `min(RETRY_BASE_SECONDS × 2^(n−1), RETRY_MAX_SECONDS)`. With
  the defaults (5 s and 300 s), the **first retry is 2.5–5 s out, the second 5–10 s**. A retry
  resumes after the stages that already finished (`stage.skipped` events), and chunks that were
  already summarized aren't sent to the provider again. If the last attempt fails too, the job
  ends `failed` and the summary gains "Gave up after N attempts.";
- **permanent**, meaning it would fail the same way again. The job goes straight to `failed`
  after one attempt.

The job's `attempts` list shows each try's `outcome` (`failed_transient`, `failed_permanent`,
`succeeded`, `cancelled`, `lease_expired`) and `trigger` (`initial`, `auto_retry`, `recovery`,
`manual_retry`). Its events include `stage.failed` (with `retryable`) and a `job.status_changed` to
`retry_wait`, whose payload has `retry_at` and `retry_in_seconds`.

Permanent:

| `error_class` | Stage | `error_summary` |
|---|---|---|
| `pdf_encrypted` | `validate_pdf` | The PDF is password-protected. Upload an unprotected copy. |
| `pdf_corrupt` | `validate_pdf` / `extract_text` | The PDF is damaged or is not a valid PDF, so it could not be read. |
| `pdf_too_many_pages` | `validate_pdf` | The PDF has N pages; the limit is L. |
| `pdf_no_text` | `extract_text` / `chunk_document` | The PDF contains little or no extractable text (it may be scanned images). Scanned PDFs aren't supported yet. |
| `pdf_text_too_large` | `extract_text` | The PDF contains more than L characters of text, which is over the limit. |
| `pdf_too_complex` | `validate_pdf` / `extract_text` | The PDF took too long or needed too much memory to process. |
| `zip_unsafe_path` | `validate_archive` | The archive contains an entry with an unsafe or conflicting name (for example an absolute path, a '..' segment, a drive letter, a backslash, or two names that differ only in letter case), so the whole archive was rejected. |
| `zip_encrypted` | `validate_archive` | The archive contains password-protected entries. Upload an archive without encryption. |
| `zip_corrupt` | `validate_archive` / `extract_files` | The archive is damaged or is not a valid ZIP file, so it could not be read. |
| `zip_too_many_files` | `validate_archive` | The archive has N entries; the limit is L. |
| `zip_too_large` | `extract_files` | The archive's files add up to more than the limit of L bytes when extracted. |
| `zip_bomb` | `extract_files` | The archive is compressed more than R:1, which is how zip bombs work, so it was not extracted. |
| `zip_too_complex` | `validate_archive` / `extract_files` | The archive took too long or needed too much memory to process. |
| `repo_no_sources` | `filter_files` | None of the N files in the archive can be indexed (unsupported file types, excluded folders, binary, empty, or too large files). |
| `repo_too_large` | `filter_files` / `summarize` | The repository has too much source code to summarize within the limit of L summarization calls. |
| `input_unavailable` | the first two stages | The uploaded file is no longer available. Upload it again and create a new job. |
| `injected_permanent` | `summarize` | Injected permanent failure (fault injection test). |
| `fault_injection_disabled` | (before any stage) | The job asks for fault injection, but fault injection is disabled on the worker. |
| `internal_error` | any | An unexpected error occurred while running the job. (Details are only in the worker logs.) Any error that isn't classified ends up here: retrying an unknown bug would only repeat it. |

Transient (retried):

| `error_class` | When | `error_summary` |
|---|---|---|
| `stage_timeout` | `summarize` ran past `SUMMARIZE_TIMEOUT_SECONDS` | Processing took longer than the N-second limit for this step. |
| `provider_timeout`, `provider_unavailable`, `provider_rate_limited` | The summarizer timed out, was down, or refused for rate limiting (real providers arrive in Phase 11) | The summarization provider did not respond in time. / ... is temporarily unavailable. / ... is limiting requests right now. |
| `database_unavailable` | The database connection failed during a stage | The database was temporarily unavailable. |
| `database_conflict` | A deadlock or serialization failure (Postgres `40P01`, `40001`) | A temporary database conflict interrupted the job. |
| `storage_unavailable` | The disk was full, read-only, or failed (`ENOSPC`, `EIO`, ...), also while reading a ZIP | File storage was temporarily unavailable. |
| `injected_transient` | Fault injection | Injected transient failure (fault injection test). |
| `lease_expired` | The worker stopped renewing its lease (killed, crashed, cut off) and recovery took the job back | The worker running the job stopped responding before it finished (its lease expired). |

"Little or no text" means fewer than 100 visible characters in total, or fewer than 20 per page
on average. PDFs protected only by an *owner* password (printing/copying restrictions, no
password needed to open) are processed normally; `manifest.json` records
`"owner_password_only": true`.

#### Fault injection (development only)

To watch retries happen, a `pdf_ingest` or `repo_index` job can ask its summarizer to fail on
purpose:

| `options.fault_injection` | Effect |
|---|---|
| `{"mode": "transient", "failures": N}` (N = 1–20) | The first summarizer call of attempts 1 to N fails with `injected_transient`, so those attempts are retried; attempt N+1 runs normally. With N ≥ 3 and the default budget, the job gives up after 3 attempts. |
| `{"mode": "permanent"}` | The first summarizer call fails with `injected_permanent`; never retried. |

It needs the server setting **`ENABLE_FAULT_INJECTION=true`** (default `false`). While the
setting is off, the API answers `422 fault_injection_disabled` and creates nothing. The worker
checks its own setting again before running the job, and fails it with `fault_injection_disabled`
if they disagree. When the setting is on, the API and worker log a warning at startup.

### Job object

| Field | Notes |
|---|---|
| `id`, `workflow_type`, `status`, `upload_id`, `input_sha256` | `input_sha256` is the upload's hash at submission |
| `current_stage`, `progress_percent` | Set by the worker as stages run (see the stages of [`pdf_ingest`](#what-a-pdf_ingest-job-does) and [`repo_index`](#what-a-repo_index-job-does)) |
| `options` | Resolved options, defaults included |
| `attempt_count`, `max_attempts`, `manual_retry_count` | The retry budget is 3 automatic attempts |
| `retry_at` | When a `retry_wait` job runs again |
| `cancel_requested_at` | When cancellation was requested (cleared by a manual retry) |
| `error_class`, `error_summary` | Safe description of the latest failure |
| `last_event_seq` | Number of the job's latest event (see [Events](#events)) |
| `created_at`, `updated_at`, `started_at`, `finished_at` | |

## Events

Everything that happens to a job is recorded as an **event** with a per-job number, `seq`
(1, 2, 3, ... with no gaps). Events are written in the same database transaction as the change
they describe, so an event you receive always describes something that was really committed.

| Event | When | Payload (ids, counts, stage names, timestamps, error classes) |
|---|---|---|
| `job.created` | Job accepted | `workflow_type`, `upload_id` |
| `job.status_changed` | Any status change | `from`, `to`; depending on the change also `error_class`, `retry_in_seconds`, `gave_up_after_attempts`, `reason`, `before_stage`, `outbox_removed`, `requested_by`, `attempt_no`, `worker_id`, `manual_retry_count` |
| `job.cancel_superseded` | The job finished before a pending cancel took effect | `outcome`, `reason` |
| `job.requeued` | Recovery re-sent a lost message | `reason`, `status`, `last_published_at` |
| `job.manual_retry` | `POST .../retry` reopened the job | `manual_retry_count`, `previous_error_class`, `attempt_count`, `max_attempts` |
| `attempt.started` | A worker claimed the job | `trigger` (`initial`, `auto_retry`, `recovery`, `manual_retry`), `worker_id`, `attempt_no` |
| `attempt.lease_expired` | Recovery closed an attempt whose worker stopped renewing its lease | `attempt_no`, `worker_id`, `last_heartbeat_at`, `lease_expired_at` |
| `stage.started` / `stage.completed` | A stage begins / ends | `stage`; completed adds `duration_ms` |
| `stage.progress` | Progress inside a stage | `stage`, `percent` |
| `stage.failed` | A stage raised | `stage`, `error_class`, `retryable` |
| `stage.skipped` | A retry reused the stage's verified checkpoint | `stage`, `reason` |

Every event has the same shape in both routes below:

```json
{"seq": 4, "event_type": "stage.started", "attempt_no": 1,
 "created_at": "2026-11-10T12:00:01.722110Z", "payload": {"stage": "validate_pdf"}}
```

`created_at` is the start of the transaction that wrote the event, so a later event can carry an
earlier timestamp. **Order by `seq`, never by `created_at`.** Payloads never contain tracebacks,
secrets, file contents or server paths. Should a future event carry a value taken from an upload
(a file name, say), it is only ever a JSON string value, never part of the SSE framing.

### `GET /v1/jobs/{job_id}/events` — implemented (Server-Sent Events)

Follow a job live. The response stays open and the server writes one block of text per event
([Server-Sent Events](https://html.spec.whatwg.org/multipage/server-sent-events.html)).

```powershell
curl.exe -N "$api/v1/jobs/<job_id>/events"            # -N: print as it arrives, don't buffer
curl.exe -N "$api/v1/jobs/<job_id>/events?after=12"   # resume after event 12
curl.exe -N -H "Last-Event-ID: 12" "$api/v1/jobs/<job_id>/events"
```

Response headers: `Content-Type: text/event-stream; charset=utf-8`, `Cache-Control: no-cache`,
`X-Accel-Buffering: no` (tells proxies such as nginx not to buffer), `Connection: keep-alive`.
The body, literally (a finished job read from the beginning, shortened):

```text
retry: 3000

id: 1
event: job.created
data: {"seq":1,"event_type":"job.created","attempt_no":null,"created_at":"2026-11-10T12:00:01.202364Z","payload":{"upload_id":"fd1a8ab5-1bb3-445e-b714-c1ba39320605","workflow_type":"pdf_ingest"}}

id: 2
event: job.status_changed
data: {"seq":2,"event_type":"job.status_changed","attempt_no":1,"created_at":"2026-11-10T12:00:01.705243Z","payload":{"to":"running","from":"queued","worker_id":"worker@6d635f77cb6a:63","attempt_no":1}}

: keepalive

id: 21
event: job.status_changed
data: {"seq":21,"event_type":"job.status_changed","attempt_no":1,"created_at":"2026-11-10T12:00:09.110021Z","payload":{"to":"succeeded","from":"running","attempt_no":1}}

event: stream.end
data: {"reason":"job_finished","status":"succeeded","last_seq":21}

```

- `retry: 3000` (first block) asks browsers to wait 3 s before reconnecting
  (`SSE_RETRY_MILLISECONDS`).
- Each event block: `id:` = the event's `seq`, `event:` = its type, `data:` = the event object
  above as **one line of JSON**. Every non-ASCII and control character in it is escaped (`\n`,
  `\r`, ` `, ...), so no value can end the line or add a field or an event.
- `: keepalive` is a comment, sent after `SSE_KEEPALIVE_SECONDS` (15) without other output, so
  proxies and clients don't treat a quiet stream as dead. Clients ignore comments.
- `stream.end` is not a stored event and has **no `id:`**: see below.

**Resume position.** The stream sends every event whose `seq` is greater than the resume
position, in order, then waits for new ones:

| Request | Resume position |
|---|---|
| `Last-Event-ID: N` header | `N` (wins if `?after=` is also given) |
| `?after=N` | `N` (EventSource can't set headers, and forgets the last id when the page reloads) |
| neither, an empty value, or `0` | `0`: everything from the first event |
| `N` greater than the job's last `seq` | No replay, no error: only events written from now on |
| anything but a non-negative integer (`-1`, `abc`, `1.5`) | `400 invalid_cursor` |

These are checked before streaming starts, as is the job itself: an unknown job is
`404 job_not_found` with the usual JSON error body.

**End of stream.** Once the job is finished (`succeeded`, `failed`, `cancelled`) and every event
up to its last one has been sent, the server sends `stream.end` and closes the connection. Its
`data` holds `reason` (`job_finished`; `job_not_found` if the job was deleted meanwhile),
`status` and `last_seq`. A client should stop on `stream.end` instead of reconnecting. A
**manual retry** reopens a failed job (it is `queued` again): to follow the new run, reconnect
with `Last-Event-ID` (or `?after=`) set to `last_seq` and you receive `job.status_changed`
(`failed -> queued`), `job.manual_retry`, and everything after.

**Limits** (per API process; settings in `.env`):

| Setting | Default | |
|---|---|---|
| `SSE_MAX_STREAMS` | 50 | Open streams at once. One more gets `503 stream_limit_reached` (`details.limit`): retry shortly, or use the history route |
| `SSE_MAX_STREAM_SECONDS` | 3600 | Then the server ends the stream with a comment and a bare `id: <last seq>` line (no `stream.end`), so EventSource reconnects and resumes where it was |
| `SSE_POLL_SECONDS` | 0.5 | How often an open stream checks for new events: new events arrive within about this long |
| `SSE_KEEPALIVE_SECONDS` | 15 | Idle time before a `: keepalive` comment (at least `SSE_POLL_SECONDS`) |
| `SSE_RETRY_MILLISECONDS` | 3000 | The `retry:` value |
| `SSE_WRITE_TIMEOUT_SECONDS` | 30 | A client that can't accept data for this long (it stopped reading) is disconnected |

A dropped connection (client, proxy or network) never affects the job: the stream only reads.
When the API stops or reloads, it closes open streams after 5 s; clients reconnect.

**Error codes:** `400 invalid_cursor`, `404 job_not_found`, `503 stream_limit_reached`, and
`503 service_unavailable` if the database is down when connecting. If the database becomes
unavailable *during* a stream, the stream stays open, keepalives continue, and events resume when
it is back.

#### Using it from a browser

```js
let lastSeq = Number(sessionStorage.getItem(`seq:${jobId}`) || 0);
const source = new EventSource(`/v1/jobs/${jobId}/events?after=${lastSeq}`);
const onEvent = (e) => {
  lastSeq = Number(e.lastEventId);
  sessionStorage.setItem(`seq:${jobId}`, String(lastSeq));
  render(JSON.parse(e.data));
};
for (const type of ["job.status_changed", "stage.started", "stage.progress" /* ... */]) {
  source.addEventListener(type, onEvent);
}
source.addEventListener("stream.end", () => source.close()); // finished: don't reconnect
source.onerror = () => {
  // readyState CONNECTING: the browser reconnects by itself, sending Last-Event-ID.
  // readyState CLOSED: it gave up (the server answered with an error, e.g. 503).
  //   Reconnect yourself later with ?after=<lastSeq>, waiting longer each time.
};
```

- Events have names, so listen with `addEventListener(type, ...)`; `onmessage` only receives
  unnamed events and never fires here.
- After a dropped connection the browser reconnects by itself and sends `Last-Event-ID`. When
  both are present, the header wins, so the `?after=` in the URL doesn't cause repeats.
- After a **page refresh** that memory is gone and EventSource can't set headers: keep the last
  `seq` (page state or `sessionStorage`) and reconnect with `?after=<seq>`.
- Close the EventSource on `stream.end`. Otherwise it reconnects after the `retry:` delay, gets
  `stream.end` again at once, and repeats every few seconds.
- A browser gives up for good when a (re)connection is answered with an error status (`503`,
  `404`); only a dropped connection is retried automatically.

### `GET /v1/jobs/{job_id}/events/history` — implemented

Plain JSON pages of events, for clients that fell behind or can't use SSE.

```powershell
curl.exe -s "$api/v1/jobs/<job_id>/events/history?limit=5"
curl.exe -s "$api/v1/jobs/<job_id>/events/history?after=5&limit=5"
```

```json
{"items": [{"seq": 6, "event_type": "stage.completed", "attempt_no": 1, "created_at": "...", "payload": {"stage": "validate_pdf", "duration_ms": 676}}],
 "next_after": 10, "last_seq": 21}
```

| Query | |
|---|---|
| `after` | Events with `seq` greater than this (default 0). Malformed: `400 invalid_cursor` |
| `limit` | 1-500, default 100. Outside that range: `422 invalid_request` |

`items` are in `seq` order. `next_after` is the `after` for the next page, or `null` when you
have everything up to `last_seq` (the job's latest event when the page was read). Unknown job:
`404 job_not_found`.

## Health

```powershell
curl.exe -s "$api/health/live"
curl.exe -s "$api/health/ready"
```

See [architecture.md](architecture.md#health-semantics-implemented-in-phase-1).
