"""devtools/job_events.py: the readable event history used in the manual walkthrough."""

from types import SimpleNamespace

from devtools.job_events import describe, format_rows


def _event(seq, event_type, payload=None, attempt_no=1):
    return SimpleNamespace(
        seq=seq, event_type=event_type, payload=payload or {}, attempt_no=attempt_no
    )


def test_retry_details_are_readable():
    assert (
        describe(
            "job.status_changed",
            {"from": "running", "to": "retry_wait", "retry_in_seconds": 3.7123, "error_class": "x"},
        )
        == "running -> retry_wait  (retry in 3.71 s)  [x]"
    )
    assert (
        describe(
            "job.status_changed",
            {"from": "running", "to": "failed", "gave_up_after_attempts": 3, "error_class": "x"},
        )
        == "running -> failed  (gave up after 3 attempts)  [x]"
    )
    assert describe(
        "stage.failed", {"stage": "summarize", "error_class": "x", "retryable": True}
    ) == ("summarize: x (retryable)")
    assert describe("stage.skipped", {"stage": "validate_pdf", "reason": "checkpoint_reused"}) == (
        "validate_pdf (checkpoint_reused)"
    )


def test_progress_is_hidden_unless_asked_for():
    events = [
        _event(1, "job.created", attempt_no=None),
        _event(2, "stage.progress", {"stage": "s", "percent": 50}),
        _event(3, "stage.started", {"stage": "s"}),
    ]
    assert [line.split()[2] for line in format_rows(events)[1:]] == ["job.created", "stage.started"]
    assert len(format_rows(events, show_all=True)) == 4
    assert format_rows(events)[1].split()[:2] == ["1", "-"]  # no attempt number
