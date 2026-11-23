# Performance: measured on one development machine

These are **local measurements on a laptop with the stub summarizer**, not a load test and not
production scale. They show how long Tasklane's own orchestration takes (queueing, dispatch,
claim, stages, database writes, artifacts) when the summarizer is instant. With a real LLM
provider, each summarization call adds the provider's latency (typically seconds), which would
dominate the workflow duration. Every job ran to completion at least once (at-least-once
delivery); nothing here implies exactly-once execution.

## How it was measured

```powershell
docker compose up -d                                              # default settings (.env.example values)
docker compose exec -T api python -m devtools.benchmark --jobs 20 # 20 PDF + 20 repository jobs
docker compose exec -T api python -m devtools.benchmark --jobs 1  # one of each, on an idle system
```

The benchmark ([backend/devtools/benchmark.py](../backend/devtools/benchmark.py)) uploads each
input once, creates the jobs through the real API (alternating PDF and repository, as fast as
it can, each with its own idempotency key), waits until all have finished, and reads the
timestamps from PostgreSQL (written by the database clock in the transactions that changed each
job):

- **queue wait**: first attempt's `started_at` − `queued_at` (waiting for a free worker; the
  same definition as the `tasklane_queue_wait_seconds` metric);
- **workflow duration**: `finished_at` − first attempt's `started_at` (the worker's processing);
- **end to end**: `finished_at` − `created_at`;
- p95 is the nearest-rank 95th percentile; with 20 jobs it is the 19th-smallest value.

**Inputs** (generated in code, the same files the tests use):

| Workflow | Input | Work per job |
|---|---|---|
| `pdf_ingest` | `report.pdf` from `devtools.pdf_samples`: 4 pages of text, 6,049 bytes | 5 stages, 3 summarization calls |
| `repo_index` | `repo.zip` from `devtools.zip_samples`: 25 entries, 20,019 bytes (6.3 MB uncompressed, almost all of it one 6 MB file that is skipped as `too_large`; the rest is small source files and files skipped for other reasons) | 6 stages, 9 summarization calls |

**Settings:** the defaults: `PROVIDER=stub`, `STUB_DELAY_SECONDS=0`, `WORKER_CONCURRENCY=2` (one
worker container with two worker processes), `DISPATCH_POLL_SECONDS=1`, `LEASE_SECONDS=60`,
`RECOVERY_INTERVAL_SECONDS=5`, fault injection off, telemetry on with no exporter.

**Machine:**

| | |
|---|---|
| Host | Windows 11 Pro 10.0.26300, 12th Gen Intel Core i5-12450H (8 cores, 12 threads), 15.7 GiB RAM, NVMe SSD (SK hynix PC801) |
| Docker | Docker Desktop, Engine 29.3.1, WSL 2 backend (kernel 6.6.87.2-microsoft-standard-WSL2) |
| Resources Docker had | 12 CPUs, 7.6 GiB memory, no CPU limit on the containers |
| Other load | The rest of the Compose stack (API, dispatcher, PostgreSQL, Redis, Vite dev server) on the same machine; no other benchmark running |

## Results (2026-11-23, 14:30 UTC)

**20 PDF and 20 repository jobs submitted at once** (wall time from the first upload to the last
job finished: 26.8 s; all 40 succeeded on their first attempt):

| Workflow | Measure | Median (s) | p95 (s) | Max (s) |
|---|---|---|---|---|
| `pdf_ingest` | queue wait | 11.70 | 21.80 | 23.08 |
| `pdf_ingest` | workflow duration | 1.33 | 1.42 | 1.47 |
| `pdf_ingest` | end to end | 12.97 | 23.09 | 24.41 |
| `repo_index` | queue wait | 11.80 | 22.02 | 23.13 |
| `repo_index` | workflow duration | 1.08 | 1.19 | 1.21 |
| `repo_index` | end to end | 12.85 | 23.14 | 24.28 |

**One job of each on an idle system:**

| Workflow | Queue wait (s) | Workflow duration (s) | End to end (s) |
|---|---|---|---|
| `pdf_ingest` | 0.90 | 1.40 | 2.30 |
| `repo_index` | 0.88 | 1.17 | 2.06 |

**Reading them.** A job takes a little over a second of worker time either way. With 40 jobs
and 2 worker processes, jobs simply wait their turn, so queue wait grows linearly with the
position in the queue (the median job waited for about half the others) and the whole batch
took 26.8 s, about 1.5 jobs per second. On an idle system the queue wait is the dispatcher's
poll interval (`DISPATCH_POLL_SECONDS=1`) plus the hop through Redis. The workflow duration is
mostly the sandboxed parser processes (a fresh Python child per parse, with resource limits) and
the database writes for every stage and progress event. More worker processes
(`WORKER_CONCURRENCY`) would shorten the queue on this 12-thread machine; that was not measured.

Two earlier runs with the same command during the Phase 12 walkthrough gave the same picture
(PDF workflow duration median 1.39 s and 1.41 s, queue wait median 12.1 s and 12.9 s).

## The /metrics scrape

`GET /metrics` computes its job, attempt, event and provider-call metrics from the database on
every scrape (Phase 10). Measured by the same benchmark run, five scrapes in a row:

| Rows in the database | Median | Max |
|---|---|---|
| 411 jobs, 16,797 events | 48.7 ms | 109.4 ms |

**Decision: no extra index.** The Phase 10 notes asked to measure this and add a partial index
on `job_events(event_type)` if the scrape got slow. At this size it is far below Prometheus's
15 s scrape interval and the 5 s statement timeout, and the cleanup (30-day job retention)
keeps the tables bounded. The scrape's cost grows with the number of rows; a database with many
thousands of jobs per day should measure it again before adding the index.

## Not measured

- A real LLM provider (needs a paid key and sends data to a third party; see the README's
  Known limitations).
- Large inputs (a 500-page PDF, a 100 MiB repository), more worker processes, or several worker
  containers.
- Behavior under failures (recovery after a killed worker takes the 60 s lease plus a backoff of
  2.5–5 s; the demo script shows about 100 s from the kill to the finished job for a 40-page PDF
  with a 1 s simulated delay per call).
