"""Measure queue wait and workflow duration on this machine (docs/performance.md).

    docker compose exec -T api python -m devtools.benchmark --jobs 20

Submits N PDF jobs and N repository jobs through the real API (uploads, then job creation with
idempotency keys, as fast as it can), waits until every job has finished, and reports from the
database, per workflow:

- queue wait: first attempt's started_at - queued_at (the time a job waited for a free worker;
  the same definition as the tasklane_queue_wait_seconds metric);
- workflow duration: finished_at - first attempt's started_at (the worker's processing time,
  retries included);
- end to end: finished_at - created_at;

as median, p95 (nearest rank) and max, plus the machine (CPU, memory and kernel as the containers
see them) and the settings that matter. It also times GET /metrics with all those jobs in the
database. Numbers are measured, never estimated; with the stub provider they show the
orchestration's own overhead, not an LLM's.

The jobs are created in the database the api container uses (your development database). They
are ordinary jobs: the cleanup command removes them once they are past the retention period.
"""

import argparse
import json
import math
import os
import platform
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Job, JobAttempt, JobEvent
from app.db.session import create_db_engine
from devtools import pdf_samples, zip_samples

INPUTS = {
    "pdf_ingest": ("report.pdf", "application/pdf", pdf_samples.SAMPLES["report.pdf"]),
    "repo_index": ("repo.zip", "application/zip", zip_samples.SAMPLES["repo.zip"]),
}
FINISHED = {"succeeded", "failed", "cancelled"}
SETTINGS_SHOWN = (
    "PROVIDER",
    "STUB_DELAY_SECONDS",
    "WORKER_CONCURRENCY",
    "DISPATCH_POLL_SECONDS",
    "LEASE_SECONDS",
    "RECOVERY_INTERVAL_SECONDS",
    "ENABLE_FAULT_INJECTION",
)


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile: the smallest value with at least `fraction` of values <= it."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "median": statistics.median(values),
        "p95": percentile(values, 0.95),
        "max": max(values),
    }


def machine() -> dict[str, Any]:
    """What the containers can see: Docker Desktop's VM on Windows/macOS, the host on Linux."""
    info: dict[str, Any] = {
        "kernel": platform.uname().release,
        "python": platform.python_version(),
        "cpus_visible": os.cpu_count(),
    }
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("model name"):
                info["cpu_model"] = line.split(":", 1)[1].strip()
                break
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                info["memory_gib"] = round(int(line.split()[1]) / 1024**2, 1)
    cpu_max = Path("/sys/fs/cgroup/cpu.max")
    if cpu_max.exists():
        info["container_cpu_limit"] = cpu_max.read_text().strip()  # "max 100000" = no limit
    return info


def submit(client: httpx.Client, count: int) -> dict[str, list[uuid.UUID]]:
    """Upload each input once, then create `count` jobs per workflow, alternating."""
    uploads = {}
    for workflow, (filename, content_type, build) in INPUTS.items():
        response = client.post(
            "/v1/uploads",
            params={"filename": filename},
            content=build(),
            headers={"Content-Type": content_type},
        )
        response.raise_for_status()
        uploads[workflow] = response.json()["id"]
    created: dict[str, list[uuid.UUID]] = {workflow: [] for workflow in INPUTS}
    for _ in range(count):
        for workflow, upload_id in uploads.items():
            response = client.post(
                "/v1/jobs",
                json={"workflow_type": workflow, "upload_id": upload_id},
                headers={"Idempotency-Key": f"benchmark-{uuid.uuid4()}"},
            )
            response.raise_for_status()
            created[workflow].append(uuid.UUID(response.json()["id"]))
    return created


def wait_until_finished(engine: Any, ids: list[uuid.UUID], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        with Session(engine) as session:
            open_jobs = session.scalar(
                select(func.count()).where(Job.id.in_(ids), Job.status.not_in(FINISHED))
            )
        if open_jobs == 0:
            return
        if time.monotonic() > deadline:
            raise SystemExit(f"timed out: {open_jobs} jobs still unfinished")
        print(f"  waiting: {open_jobs} of {len(ids)} jobs unfinished", file=sys.stderr)
        time.sleep(2)


def measure(engine: Any, ids: list[uuid.UUID]) -> dict[str, Any]:
    first = (
        select(JobAttempt.job_id, func.min(JobAttempt.attempt_no).label("attempt_no"))
        .where(JobAttempt.job_id.in_(ids))
        .group_by(JobAttempt.job_id)
        .subquery()
    )
    with Session(engine) as session:
        rows = session.execute(
            select(
                Job.status,
                Job.attempt_count,
                Job.created_at,
                Job.finished_at,
                JobAttempt.queued_at,
                JobAttempt.started_at,
            )
            .join(first, first.c.job_id == Job.id)
            .join(
                JobAttempt,
                (JobAttempt.job_id == Job.id) & (JobAttempt.attempt_no == first.c.attempt_no),
            )
        ).all()
    statuses: dict[str, int] = {}
    for row in rows:
        statuses[row.status] = statuses.get(row.status, 0) + 1
    return {
        "jobs": len(rows),
        "statuses": statuses,
        "attempts": sum(row.attempt_count for row in rows),
        "queue_wait_seconds": summarize(
            [(row.started_at - row.queued_at).total_seconds() for row in rows]
        ),
        "workflow_seconds": summarize(
            [(row.finished_at - row.started_at).total_seconds() for row in rows]
        ),
        "end_to_end_seconds": summarize(
            [(row.finished_at - row.created_at).total_seconds() for row in rows]
        ),
    }


def time_metrics_scrape(client: httpx.Client, engine: Any, tries: int = 5) -> dict[str, Any]:
    timings = []
    for _ in range(tries):
        start = time.perf_counter()
        client.get("/metrics").raise_for_status()
        timings.append((time.perf_counter() - start) * 1000)
    with Session(engine) as session:
        jobs = session.scalar(select(func.count()).select_from(Job))
        events = session.scalar(select(func.count()).select_from(JobEvent))
    return {
        "median_ms": round(statistics.median(timings), 1),
        "max_ms": round(max(timings), 1),
        "jobs_in_database": jobs,
        "events_in_database": events,
    }


def report(result: dict[str, Any]) -> str:
    lines = [
        f"Tasklane benchmark, {result['started_at']} (UTC), {result['jobs_per_workflow']} jobs "
        "per workflow",
        "",
        "Machine (as seen from the containers): "
        + ", ".join(f"{key}={value}" for key, value in result["machine"].items()),
        "Settings: " + ", ".join(f"{key}={value}" for key, value in result["settings"].items()),
        f"Wall time from first upload to last job finished: {result['wall_seconds']:.1f} s",
        "",
        "| Workflow | Jobs | Statuses | Measure | Median (s) | p95 (s) | Max (s) |",
        "|---|---|---|---|---|---|---|",
    ]
    for workflow, data in result["workflows"].items():
        statuses = ", ".join(f"{count} {name}" for name, count in sorted(data["statuses"].items()))
        for label, key in (
            ("queue wait", "queue_wait_seconds"),
            ("workflow duration", "workflow_seconds"),
            ("end to end", "end_to_end_seconds"),
        ):
            values = data[key]
            lines.append(
                f"| {workflow} | {data['jobs']} | {statuses} | {label} | "
                f"{values['median']:.2f} | {values['p95']:.2f} | {values['max']:.2f} |"
            )
    scrape = result["metrics_scrape"]
    lines += [
        "",
        f"GET /metrics: median {scrape['median_ms']} ms, max {scrape['max_ms']} ms over 5 scrapes, "
        f"with {scrape['jobs_in_database']} jobs and {scrape['events_in_database']} events in "
        "the database.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m devtools.benchmark", description=__doc__)
    parser.add_argument("--jobs", type=int, default=20, help="jobs per workflow (default 20)")
    parser.add_argument("--api", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--timeout", type=float, default=1800, help="seconds to wait for jobs")
    parser.add_argument("--json", type=Path, help="also write the raw results to this file")
    args = parser.parse_args(argv)

    engine = create_db_engine(get_settings(), pool_size=1, max_overflow=0)
    started = datetime.now(UTC)
    with httpx.Client(base_url=args.api, timeout=60) as client:
        provider = client.get("/v1/meta").json().get("provider", {}).get("name")
        if provider != "stub":
            raise SystemExit(f"the benchmark runs with the stub provider only (found {provider})")
        clock = time.monotonic()
        created = submit(client, args.jobs)
        all_ids = [job_id for ids in created.values() for job_id in ids]
        wait_until_finished(engine, all_ids, args.timeout)
        wall = time.monotonic() - clock
        result = {
            "started_at": started.strftime("%Y-%m-%d %H:%M"),
            "jobs_per_workflow": args.jobs,
            "machine": machine(),
            "settings": {name: os.environ.get(name, "(default)") for name in SETTINGS_SHOWN},
            "wall_seconds": wall,
            "workflows": {workflow: measure(engine, ids) for workflow, ids in created.items()},
            "metrics_scrape": time_metrics_scrape(client, engine),
        }
    engine.dispose()
    print(report(result))
    if args.json:
        args.json.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
