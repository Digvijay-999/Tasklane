"""Database-derived metrics for GET /metrics, computed from PostgreSQL at scrape time.

Why from the database (PHASE10_NOTES.md, "Which metrics come from where"): jobs, attempts, events,
provider calls, the outbox and the worker heartbeat table already record everything these
metrics count. Reading them is exact, survives restarts of any process, and covers every worker
process, including Celery children that were recycled or killed (their in-memory counters would
be gone, and the API can't read another process's memory anyway).

The cost is a few aggregate queries per scrape, bounded by a 5 s statement timeout. They scan the
attempts, events and provider-call tables, which grow until Phase 12 adds retention.

Counters here are totals over the rows that exist. Nothing deletes rows today; once retention
does (Phase 12), a total can go down, which Prometheus treats as a counter reset.
"""

import logging
from collections.abc import Iterator
from datetime import timedelta

from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)
from prometheus_client.metrics_core import Metric
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.db.repositories import metrics as rows
from app.db.repositories import overview as overview_rows
from app.db.repositories import workers as worker_rows
from app.domain.enums import EventType, JobStatus, WorkflowType
from app.domain.state_machine import TERMINAL_STATUSES

logger = logging.getLogger(__name__)

# Bucket upper bounds in seconds (Prometheus adds +Inf).
QUEUE_WAIT_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1800, 3600)
RUNTIME_BUCKETS = (0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600)
STAGE_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 900)
PROVIDER_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30)
STATEMENT_TIMEOUT = "5s"


class DatabaseMetrics:
    """A prometheus_client collector: each scrape reads one consistent snapshot (one read-only
    transaction) and yields metric families."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        worker_stale_after: timedelta,
    ) -> None:
        self.session_factory = session_factory
        self.worker_stale_after = worker_stale_after

    def describe(self) -> list[Metric]:
        return []  # no up-front description: families depend on the data

    def collect(self) -> Iterator[Metric]:
        try:
            families = self._read()
        except Exception as exc:
            # The database is down (or too slow): say so, and still serve the API-local metrics.
            logger.warning(
                "metrics: database read failed", extra={"error_type": type(exc).__name__}
            )
            families = []
        up = GaugeMetricFamily(
            "tasklane_metrics_database_up",
            "1 if this scrape could read the database-derived metrics, else 0.",
        )
        up.add_metric([], 1 if families else 0)
        yield up
        yield from families

    def _read(self) -> list[Metric]:
        with self.session_factory.begin() as session:
            session.execute(text("SET TRANSACTION READ ONLY, ISOLATION LEVEL REPEATABLE READ"))
            session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
            return [
                *self._job_metrics(session),
                *self._histograms(session),
                *self._provider_metrics(session),
                *self._queue_metrics(session),
                *self._worker_metrics(session),
            ]

    def _job_metrics(self, session: Session) -> list[Metric]:
        created = CounterMetricFamily(
            "tasklane_jobs_created", "Jobs created, by workflow.", labels=["workflow_type"]
        )
        found = rows.jobs_created(session)
        for workflow in WorkflowType:
            created.add_metric([workflow.value], found.get(workflow.value, 0))

        by_status = GaugeMetricFamily(
            "tasklane_jobs", "Jobs in each status right now.", labels=["status"]
        )
        counts = rows.jobs_by_status(session)
        for status in JobStatus:
            by_status.add_metric([status.value], counts.get(status.value, 0))

        finished = CounterMetricFamily(
            "tasklane_jobs_finished",
            "Times a job reached succeeded, failed or cancelled (a manually retried job can "
            "finish more than once), by workflow and status.",
            labels=["workflow_type", "status"],
        )
        retries = CounterMetricFamily(
            "tasklane_job_retries",
            "Automatic retries scheduled (job moved to retry_wait), by workflow and error class.",
            labels=["workflow_type", "error_class"],
        )
        manual = CounterMetricFamily(
            "tasklane_manual_retries", "Manual retries of failed jobs.", labels=["workflow_type"]
        )
        recoveries = CounterMetricFamily(
            "tasklane_recoveries",
            "Recovery actions: expired leases closed, lost messages requeued.",
            labels=["workflow_type", "outcome"],
        )
        totals: dict[str, dict[tuple[str, ...], int]] = {
            "finished": {
                (w.value, s.value): 0 for w in WorkflowType for s in sorted(TERMINAL_STATUSES)
            },
            "retries": {},
            "manual": {(w.value,): 0 for w in WorkflowType},
            "recoveries": {
                (w.value, o): 0 for w in WorkflowType for o in ("lease_expired", "lost_message")
            },
        }
        for c in rows.event_counts(session):
            if c.event_type == EventType.JOB_STATUS_CHANGED:
                if c.to_status in TERMINAL_STATUSES:
                    key = (c.workflow_type, c.to_status)
                    totals["finished"][key] = totals["finished"].get(key, 0) + c.count
                elif c.to_status == JobStatus.RETRY_WAIT:
                    key = (c.workflow_type, c.error_class or "unknown")
                    totals["retries"][key] = totals["retries"].get(key, 0) + c.count
            elif c.event_type == EventType.JOB_MANUAL_RETRY:
                totals["manual"][(c.workflow_type,)] += c.count
            elif c.event_type == EventType.ATTEMPT_LEASE_EXPIRED:
                totals["recoveries"][(c.workflow_type, "lease_expired")] += c.count
            elif c.event_type == EventType.JOB_REQUEUED:
                totals["recoveries"][(c.workflow_type, "lost_message")] += c.count
        for family, name in (
            (finished, "finished"),
            (retries, "retries"),
            (manual, "manual"),
            (recoveries, "recoveries"),
        ):
            for labels, value in sorted(totals[name].items()):
                family.add_metric(list(labels), value)
        return [created, by_status, finished, retries, manual, recoveries]

    def _histograms(self, session: Session) -> list[Metric]:
        specs = [
            (
                "tasklane_queue_wait_seconds",
                "Time from a job becoming claimable (queued, or its retry due) to a worker "
                "claiming it, per attempt.",
                ["workflow_type"],
                QUEUE_WAIT_BUCKETS,
                rows.queue_wait,
            ),
            (
                "tasklane_workflow_runtime_seconds",
                "Run time of finished attempts (claim to end), by how they ended.",
                ["workflow_type", "outcome"],
                RUNTIME_BUCKETS,
                rows.workflow_runtime,
            ),
            (
                "tasklane_stage_duration_seconds",
                "Duration of completed stage runs (reused checkpoints not included).",
                ["workflow_type", "stage"],
                STAGE_BUCKETS,
                rows.stage_duration,
            ),
            (
                "tasklane_provider_call_duration_seconds",
                "Summarization provider call latency, by outcome (success or failure).",
                ["provider", "model", "outcome"],
                PROVIDER_BUCKETS,
                rows.provider_latency,
            ),
        ]
        families: list[Metric] = []
        for name, documentation, labels, buckets, read in specs:
            family = HistogramMetricFamily(name, documentation, labels=labels)
            for row in read(session, buckets):
                family.add_metric(
                    list(row.labels),
                    [(str(float(b)), c) for b, c in zip(buckets, row.cumulative, strict=True)]
                    + [("+Inf", row.count)],
                    sum_value=row.total,
                )
            families.append(family)
        return families

    def _provider_metrics(self, session: Session) -> list[Metric]:
        calls = CounterMetricFamily(
            "tasklane_provider_calls",
            "Summarization provider calls, by outcome (success or failure).",
            labels=["provider", "model", "outcome"],
        )
        failures = CounterMetricFamily(
            "tasklane_provider_call_failures",
            "Failed provider calls, by error class.",
            labels=["provider", "model", "error_class"],
        )
        prompt = CounterMetricFamily(
            "tasklane_provider_prompt_tokens",
            "Prompt tokens reported by providers (calls without usage count 0; never estimated).",
            labels=["provider", "model"],
        )
        completion = CounterMetricFamily(
            "tasklane_provider_completion_tokens",
            "Completion tokens reported by providers (never estimated).",
            labels=["provider", "model"],
        )
        call_totals: dict[tuple[str, str, str], int] = {}
        token_totals: dict[tuple[str, str], list[int]] = {}
        for c in rows.provider_call_counts(session):
            outcome = "success" if c.error_class is None else "failure"
            key = (c.provider, c.model, outcome)
            call_totals[key] = call_totals.get(key, 0) + c.calls
            if c.error_class is not None:
                failures.add_metric([c.provider, c.model, c.error_class], c.calls)
            tokens = token_totals.setdefault((c.provider, c.model), [0, 0])
            tokens[0] += c.prompt_tokens
            tokens[1] += c.completion_tokens
        for labels, value in sorted(call_totals.items()):
            calls.add_metric(list(labels), value)
        for labels, (p, c) in sorted(token_totals.items()):
            prompt.add_metric(list(labels), p)
            completion.add_metric(list(labels), c)
        return [calls, failures, prompt, completion]

    def _queue_metrics(self, session: Session) -> list[Metric]:
        oldest = overview_rows.oldest_queued(session)
        age = GaugeMetricFamily(
            "tasklane_oldest_queued_job_age_seconds",
            "How long the oldest queued job has waited (0 when none is queued).",
        )
        age.add_metric([], oldest.queued_seconds if oldest else 0.0)
        counts = rows.outbox_counts(session)
        pending = GaugeMetricFamily(
            "tasklane_outbox_pending_messages",
            "Unpublished outbox messages that are due, or whose publishing already failed.",
        )
        pending.add_metric([], counts.pending)
        scheduled = GaugeMetricFamily(
            "tasklane_outbox_scheduled_messages",
            "Unpublished outbox messages waiting for a later retry time.",
        )
        scheduled.add_metric([], counts.scheduled)
        failures = CounterMetricFamily(
            "tasklane_dispatch_failures",
            "Failed attempts to publish outbox messages to Redis, counted once a message is "
            "published (while Redis is down, the rest of each batch counts as failed too).",
        )
        failures.add_metric([], counts.dispatch_failures)
        return [age, pending, scheduled, failures]

    def _worker_metrics(self, session: Session) -> list[Metric]:
        summary = worker_rows.worker_summary(session, self.worker_stale_after)
        alive = GaugeMetricFamily(
            "tasklane_workers_alive",
            "Worker processes that reported within the last three presence intervals.",
        )
        alive.add_metric([], summary.alive)
        families: list[Metric] = [alive]
        for name, documentation, value in (
            (
                "tasklane_worker_fenced_attempts",
                "Attempts a worker stopped because recovery had closed them (fenced out).",
                summary.fenced_attempts,
            ),
            (
                "tasklane_worker_heartbeat_failures",
                "Lease renewals by the heartbeat thread that failed (and were retried).",
                summary.heartbeat_failures,
            ),
            (
                "tasklane_worker_heartbeat_thread_deaths",
                "Heartbeat threads that died from an unexpected error.",
                summary.heartbeat_thread_deaths,
            ),
        ):
            family = CounterMetricFamily(name, documentation)
            family.add_metric([], value)
            families.append(family)
        return families
