"""Metrics: process-local OpenTelemetry instruments, and the GET /metrics text.

Two kinds of metrics end up on GET /metrics (full list: docs/observability.md):

1. Database-derived (app/services/metrics.py): computed from PostgreSQL at scrape time. Jobs,
   attempts, events, provider calls, the outbox and the worker heartbeat table are the source
   of truth, so these numbers are exact, survive restarts, and include work done by every
   worker process (Celery's forked children are recycled and killed; anything they counted in
   memory would vanish, and their memory isn't reachable from the API anyway).
2. Process-local (here): things that happen only inside the API process and leave no row in the
   database, namely event streams. OpenTelemetry instruments, read through the Prometheus
   exporter. They reset when the API restarts (Prometheus' rate() handles counter resets).

Labels are bounded enums only (workflow_type, status, stage, error_class, provider, model,
outcome). Never job ids, upload ids, attempt ids, worker ids, pids, host names or paths: each
distinct label value creates a new time series forever. A test enforces the allowlist.
"""

import threading
from collections.abc import Iterable, Iterator

from opentelemetry import metrics
from prometheus_client import REGISTRY, CollectorRegistry, generate_latest
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector

PREFIX = "tasklane_"
ALLOWED_LABELS = frozenset(
    {"workflow_type", "status", "stage", "error_class", "provider", "model", "outcome"}
)

_configured = False
_lock = threading.Lock()


def configure_metrics() -> bool:
    """Install the meter provider with the Prometheus reader (API process only; once)."""
    global _configured
    with _lock:
        if _configured:
            return False
        _configured = True
        # Imported here: only the API process exposes OpenTelemetry instruments.
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from opentelemetry.sdk.metrics import MeterProvider

        # target_info would add resource attributes (service name, SDK version) as labels.
        reader = PrometheusMetricReader(disable_target_info=True)
        metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
        return True


# Instruments are created from the global (proxy) meter, so they can be defined at import and
# start recording once configure_metrics() runs; in processes that never configure metrics they
# are no-ops.
_meter = metrics.get_meter("tasklane")
SSE_STREAMS_OPEN = _meter.create_up_down_counter(
    "tasklane.sse.streams_open", description="Event streams open right now in this API process."
)
SSE_STREAMS_REFUSED = _meter.create_counter(
    "tasklane.sse.streams_refused",
    description="Event streams refused with 503 stream_limit_reached.",
)
SSE_STREAMS_CLOSED = _meter.create_counter(
    "tasklane.sse.streams_closed",
    description="Event streams closed, by outcome (disconnected, job_finished, max_lifetime, "
    "job_not_found).",
)
SSE_EVENTS_SENT = _meter.create_counter(
    "tasklane.sse.events_sent", description="Events written to event streams."
)
SSE_SEQ_GAPS = _meter.create_counter(
    "tasklane.sse.seq_gaps", description="Missing event numbers noticed while streaming."
)


class _OpenTelemetryFamilies(Collector):
    """The OpenTelemetry instruments from the global Prometheus registry: only ours (`tasklane_`,
    not the client library's process/GC metrics), without the exporter's constant otel_scope_*
    labels."""

    def collect(self) -> Iterator[Metric]:
        for family in REGISTRY.collect():
            if not family.name.startswith(PREFIX):
                continue
            family.samples = [
                s._replace(
                    labels={k: v for k, v in s.labels.items() if not k.startswith("otel_scope_")}
                )
                for s in family.samples
            ]
            yield family


def render(collectors: Iterable[Collector]) -> bytes:
    """The Prometheus text exposition of the given collectors plus the OpenTelemetry ones."""
    registry = CollectorRegistry(auto_describe=False)
    registry.register(_OpenTelemetryFamilies())
    for collector in collectors:
        registry.register(collector)
    return generate_latest(registry)
