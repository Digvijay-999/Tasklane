# Phase 11 walkthrough: the optional real provider

Windows PowerShell 5.1, one window, every path is a variable, no `cd`. All settings are **shell
variables only** (`.env` is never edited); step 13 removes them. Ids, token counts, costs and
summaries will differ from the examples. Background: [docs/providers.md](docs/providers.md).

Steps marked **(you run this)** need your Groq key (or call Groq's servers) and were not run by
Claude; every other step was run twice exactly as written. You set the key yourself with
`$env:PROVIDER_API_KEY` in your own window; it reaches only the worker container.

## Setup

**1. Paths, a short name for `docker compose`, and helpers** (create a job, wait for it, read its
manifest, wait for the API after a restart).
```powershell
$repo = "D:\Projects\Tasklane"; $demo = "$env:USERPROFILE\tasklane-demo"; $api = "http://127.0.0.1:8000"; New-Item -ItemType Directory -Force $demo | Out-Null
function dc { docker compose -f "$repo\compose.yaml" @args }
function New-DemoJob($File, $Workflow = "pdf_ingest") {
  $type = if ($Workflow -eq "repo_index") { "application/zip" } else { "application/pdf" }
  $upload = Invoke-RestMethod -Method Post -Uri "$api/v1/uploads?filename=$(Split-Path $File -Leaf)" -InFile $File -ContentType $type
  $body = @{ workflow_type = $Workflow; upload_id = $upload.id } | ConvertTo-Json
  Invoke-RestMethod -Method Post -Uri "$api/v1/jobs" -ContentType "application/json" -Body $body -Headers @{ "Idempotency-Key" = [guid]::NewGuid().ToString() }
}
function Wait-DemoJob($Id, $Seconds = 180) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  do { $job = Invoke-RestMethod "$api/v1/jobs/$Id"; if ($job.status -in "succeeded", "failed", "cancelled") { return $job }; Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $deadline)
  throw "job $Id is still $($job.status)"
}
function Get-DemoManifest($Job) { Invoke-RestMethod "$api$(($Job.artifacts | Where-Object artifact_type -eq 'manifest').download_url)" }
function Wait-DemoApi { $deadline = (Get-Date).AddSeconds(60); do { Start-Sleep -Seconds 2; try { if ((Invoke-WebRequest "$api/health/ready" -UseBasicParsing).StatusCode -eq 200) { return "API ready" } } catch {} } while ((Get-Date) -lt $deadline); throw "API not ready" }
```
Expected: no output.

**2. Sample files**: a text PDF, and a small repository with a planted **fake** secret in
`config.py` (files at the top level of the ZIP: PowerShell 5.1's `Compress-Archive` would write
sub-folders with backslashes, which Tasklane rejects as `zip_unsafe_path`).
```powershell
dc run --rm -v "${demo}:/out" api python -m devtools.pdf_samples /out | Select-Object -First 1; $src = "$demo\secret-repo"; New-Item -ItemType Directory -Force $src | Out-Null; Set-Content "$src\README.md" "# Shop`nA tiny shop service that totals orders." -Encoding ascii; Set-Content "$src\orders.py" "def total(items):`n    return sum(item.price for item in items)" -Encoding ascii; Set-Content "$src\config.py" "DATABASE_URL = `"postgresql://shop:fake-db-password-12345@db.internal/shop`"`nAPI_TOKEN = `"fake-token-value-abcdef123456`"`nTIMEOUT_SECONDS = 30" -Encoding ascii; Compress-Archive -Path "$src\*" -DestinationPath "$demo\secret-repo.zip" -Force; (Get-Item "$demo\secret-repo.zip").Length -gt 0
```
Expected: `wrote /out/report.pdf`, then `True`.

## (a) The stub still works with no key

**3. Start the stack with the defaults** and ask the API which summarizer it uses.
```powershell
dc up -d 2>$null; Wait-DemoApi; (Invoke-RestMethod "$api/v1/meta").provider
```
Expected:
```
API ready

name           : stub
model          : stub-extractive-v1
remote         : False
host           :
estimated_cost : False
```
On http://127.0.0.1:5173/jobs/new the page says *Summaries are made on this machine by the
built-in stub ... No document text leaves it.*

**4. A PDF and the planted-secret repository with the stub.**
```powershell
$pdf = Wait-DemoJob (New-DemoJob "$demo\report.pdf").id; $zip = Wait-DemoJob (New-DemoJob "$demo\secret-repo.zip" "repo_index").id; "$($pdf.status) / $($zip.status)"; $pdf.usage | Format-Table provider, model, calls, prompt_tokens, estimated_cost; "secret_scrubbing: $((Get-DemoManifest $zip).secret_scrubbing)"
```
Expected: `succeeded / succeeded`, a usage row `stub  stub-extractive-v1  3` with empty tokens
and cost (the stub reports none), and `secret_scrubbing: ` (null: nothing left the machine, so
nothing was scrubbed).

## The startup check (no key)

**5. Configure Groq without a key.** The API and dispatcher start (they never need the key) and
`GET /v1/meta` reports the remote provider; the worker refuses to start. The prices are example
values: copy yours from Groq's pricing page.
```powershell
$env:PROVIDER = "openai_compatible"; $env:PROVIDER_BASE_URL = "https://api.groq.com/openai/v1"; $env:PROVIDER_MODEL = "llama-3.1-8b-instant"; $env:PROVIDER_MAX_CALLS_PER_MINUTE = "20"; $env:PROVIDER_INPUT_PRICE_PER_MILLION_TOKENS = "0.05"; $env:PROVIDER_OUTPUT_PRICE_PER_MILLION_TOKENS = "0.08"; dc up -d 2>$null; Wait-DemoApi; (Invoke-RestMethod "$api/v1/meta").provider; Start-Sleep -Seconds 10; (dc logs --since 1m worker 2>$null | Select-String "cannot start" | Select-Object -Last 1).Line
```
Expected:
```
API ready

name           : openai_compatible
model          : llama-3.1-8b-instant
remote         : True
host           : api.groq.com
estimated_cost : True

worker-1  | tasklane worker cannot start: PROVIDER=openai_compatible needs PROVIDER_API_KEY in the worker's environment (set it in your shell, e.g. $env:PROVIDER_API_KEY, never in a committed file)
```

**6. The New job page** (http://127.0.0.1:5173/jobs/new, reload it) now shows, before you choose
a file: **Document text is sent to an external AI provider.** ... sent to `api.groq.com` ...
using the model `llama-3.1-8b-instant`, what is replaced first, and *Don't upload files that must
not leave this machine.*

## (b) Groq with your key

**7. (you run this) Give the worker your key** (this window only) and check it started.
```powershell
$env:PROVIDER_API_KEY = "<paste your Groq key>"; dc up -d worker 2>$null; Start-Sleep -Seconds 15; dc logs --since 1m worker 2>$null | Select-String "remote summarization provider|ready\." | Select-Object -Last 2
```
Expected: a JSON line `"message": "document text is sent to a remote summarization provider",
"provider": "openai_compatible", "model": "llama-3.1-8b-instant", "provider_host": "api.groq.com"`
and `worker@... ready.` The key appears in neither.

**8. (you run this) Summarize report.pdf with Groq.**
```powershell
$groq = Wait-DemoJob (New-DemoJob "$demo\report.pdf").id; $groq.status; $groq.usage | Format-Table provider, model, calls, failed_calls, prompt_tokens, completion_tokens, total_tokens, estimated_cost, estimated_cost_currency; (Invoke-WebRequest "$api$(($groq.artifacts | Where-Object artifact_type -eq 'summary').download_url)" -UseBasicParsing).Content
```
Expected: `succeeded`; one usage row `openai_compatible  llama-3.1-8b-instant  3  0` with real
token counts and an estimated cost like `0.000123  USD`; then `summary.md`, with an AI-written
summary under the notice *This summary was generated by a language model ...* (If it failed with
`provider_rate_limited`, the job retries by itself after the `Retry-After` wait; run
`Wait-DemoJob $groq.id` again.)

## (c) Tokens, model and estimated cost in the dashboard

**9. (you run this) Open the job page.**
```powershell
Start-Process "http://127.0.0.1:5173/jobs/$($groq.id)"
```
Expected: the **Summarization** section shows `openai_compatible / llama-3.1-8b-instant`, calls,
prompt / completion / total tokens and **Estimated cost** `0.000123 USD`; the summary preview
shows the AI summary as plain text. The same numbers: `(Get-DemoManifest $groq).usage`.

## (d) The planted secret is scrubbed

**10. (you run this) Index the repository with Groq.**
```powershell
$scrubbed = Wait-DemoJob (New-DemoJob "$demo\secret-repo.zip" "repo_index").id; $scrubbed.status; (Get-DemoManifest $scrubbed).secret_scrubbing | ConvertTo-Json -Compress; $summary = (Invoke-WebRequest "$api$(($scrubbed.artifacts | Where-Object artifact_type -eq 'summary').download_url)" -UseBasicParsing).Content; "password in summary: $($summary.Contains('fake-db-password-12345')); token in summary: $($summary.Contains('fake-token-value-abcdef123456'))"
```
Expected: `succeeded`, `{"replaced":2,"by_kind":{"assignment":1,"url_credentials":1}}`, and
`password in summary: False; token in summary: False`: the provider only ever saw `[REDACTED]`.
On the job page the manifest shows **Secrets replaced before sending: 2**. (The `chunks.jsonl`
artifact still holds the original file text: it never left your machine.)

## (e) A wrong key: provider_auth, no retry

**11. (you run this) Use an invalid key** (Groq answers 401).
```powershell
$env:PROVIDER_API_KEY = "gsk_this_is_not_a_valid_key_0000000000000000"; dc up -d worker 2>$null; Start-Sleep -Seconds 15; $bad = Wait-DemoJob (New-DemoJob "$demo\report.pdf").id; "$($bad.status) $($bad.error_class) attempts=$($bad.attempts.Count) can_retry=$($bad.can_retry)"; $bad.error_summary
```
Expected:
```
failed provider_auth attempts=1 can_retry=True
The summarization provider rejected the API key (not valid, or not allowed to use this model). Check PROVIDER_API_KEY and PROVIDER_MODEL.
```
One attempt, no automatic retry (a wrong key won't fix itself). After setting the right key
(step 7), **Retry** on the job page resumes it. The worker log has `"status_code": 401,
"provider_error_code": "invalid_api_key"`, never the key or Groq's message:
`dc logs --since 2m worker | Select-String "provider call failed"`.

**12. (you run this, optional) The live smoke test** with your real key: one short, harmless
paragraph, nothing from your files.
```powershell
$env:PROVIDER_API_KEY = "<paste your Groq key>"; dc run --rm -e PROVIDER_API_KEY api pytest -m live -q
```
Expected: `1 passed`. (Without `PROVIDER_API_KEY` it reports `1 skipped`.)

## Reset

**13. Remove every provider variable** (including your key) and go back to the stub. Do this
before running any backend tests: Compose passes shell variables into the test container, and
the tests expect the default (stub) settings.
```powershell
"PROVIDER", "PROVIDER_BASE_URL", "PROVIDER_MODEL", "PROVIDER_API_KEY", "PROVIDER_MAX_CALLS_PER_MINUTE", "PROVIDER_INPUT_PRICE_PER_MILLION_TOKENS", "PROVIDER_OUTPUT_PRICE_PER_MILLION_TOKENS" | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }; dc up -d 2>$null; Wait-DemoApi; (Invoke-RestMethod "$api/v1/meta").provider.name; "key characters in the worker: $(([string](dc exec -T worker printenv PROVIDER_API_KEY)).Length)"
```
Expected: `API ready`, `stub`, then `key characters in the worker: 0`.

## Without a key, as a reviewer

**14. What the tests prove without any network** (a fake OpenAI-compatible server): every error
class, Retry-After, the hard timeout, malformed replies, the prompt-injection structure, secret
scrubbing, the cache keyed by model, resume after a failure, and the key found nowhere.
```powershell
dc run --rm api pytest -q tests/unit/test_openai_provider.py tests/unit/test_real_provider_stages.py tests/unit/test_provider_settings.py tests/integration/test_real_provider.py tests/integration/test_real_provider_e2e.py tests/live 2>$null | Select-Object -Last 1
```
Expected: `70 passed, 1 skipped, 1 warning in ...s` (the skipped one is the live test; the
warning is a Starlette deprecation notice).

**15. All tests** (the backend suite takes 10 minutes or more).
```powershell
dc run --rm api pytest -q 2>$null | Select-Object -Last 1; npm --prefix "$repo\frontend" test 2>$null | Select-String "Tests " -CaseSensitive
```
Expected: `1500 passed, 1 skipped, 1 warning in ...s` and `Tests  57 passed (57)`. The Playwright
tests need the Phase 9 demo settings: see `WALKTHROUGH_PHASE9.md`, "Run the tests".
