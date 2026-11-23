"""devtools/benchmark.py: the statistics it reports (the run itself needs the whole stack)."""

from devtools.benchmark import percentile, report, summarize


def test_nearest_rank_percentile():
    values = [float(n) for n in range(1, 21)]  # 1..20
    assert percentile(values, 0.95) == 19.0  # ceil(0.95 * 20) = 19th value
    assert percentile(values, 0.5) == 10.0
    assert percentile([3.0], 0.95) == 3.0
    assert percentile([5.0, 1.0, 3.0], 1.0) == 5.0  # order of the input doesn't matter


def test_summary_and_report_shape():
    stats = summarize([1.0, 2.0, 3.0, 4.0])
    assert stats == {"median": 2.5, "p95": 4.0, "max": 4.0}
    text = report(
        {
            "started_at": "2026-11-23 12:00",
            "jobs_per_workflow": 4,
            "machine": {"cpus_visible": 8},
            "settings": {"PROVIDER": "stub"},
            "wall_seconds": 12.3,
            "workflows": {
                "pdf_ingest": {
                    "jobs": 4,
                    "statuses": {"succeeded": 4},
                    "attempts": 4,
                    "queue_wait_seconds": stats,
                    "workflow_seconds": stats,
                    "end_to_end_seconds": stats,
                }
            },
            "metrics_scrape": {
                "median_ms": 10.0,
                "max_ms": 12.0,
                "jobs_in_database": 4,
                "events_in_database": 40,
            },
        }
    )
    assert "| pdf_ingest | 4 | 4 succeeded | queue wait | 2.50 | 4.00 | 4.00 |" in text
    assert "GET /metrics: median 10.0 ms" in text
