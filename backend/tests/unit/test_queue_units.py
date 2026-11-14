"""Pure pieces of the queue: dispatch backoff, the dummy stage, Celery config, settings."""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.domain.dispatch import DISPATCH_BACKOFF_MAX_SECONDS, dispatch_backoff_seconds
from app.domain.enums import WorkflowType
from app.domain.workflows import WORKFLOW_STAGES
from app.queue.celery_app import CELERY_TASK_NAMES, RUN_JOB_TASK, create_celery_app
from app.workflows.registry import stages_for
from tests.dummy_stage import DummyStage

# --- Backoff ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failures", "seconds"),
    [(0, 0), (1, 1), (2, 2), (3, 4), (6, 32), (7, 60), (100, 60), (10_000, 60)],
)
def test_dispatch_backoff_doubles_and_caps(failures, seconds):
    assert dispatch_backoff_seconds(failures) == seconds


def test_dispatch_backoff_never_exceeds_the_cap():
    assert all(dispatch_backoff_seconds(n) <= DISPATCH_BACKOFF_MAX_SECONDS for n in range(500))


# --- Dummy stage ----------------------------------------------------------------------------------


class RecordingContext:
    def __init__(self):
        self.reports: list[int] = []

    def report_progress(self, percent: int) -> None:
        self.reports.append(percent)


def test_dummy_stage_reports_progress_in_steps_and_sleeps_for_its_duration():
    slept: list[float] = []
    stage = DummyStage(duration_seconds=2.5, steps=5, sleep=slept.append)
    context = RecordingContext()
    stage.run(context)
    assert context.reports == [20, 40, 60, 80, 100]
    assert sum(slept) == pytest.approx(2.5)


def test_workflow_stages(unit_settings):
    from app.queue.worker_process import workflow_config

    config = workflow_config(unit_settings)
    # GET /v1/meta publishes WORKFLOW_STAGES; it must match what the worker really runs.
    for workflow_type in WorkflowType:
        stages = stages_for(workflow_type, config)
        assert [(s.name, s.weight) for s in stages] == list(WORKFLOW_STAGES[workflow_type])
    assert [s.name for s in stages_for(WorkflowType.PDF_INGEST, config)] == [
        "validate_pdf",
        "extract_text",
        "chunk_document",
        "summarize",
        "write_artifacts",
    ]
    assert [s.name for s in stages_for(WorkflowType.REPO_INDEX, config)] == [
        "validate_archive",
        "extract_files",
        "filter_files",
        "chunk_sources",
        "summarize",
        "write_artifacts",
    ]


# --- Celery configuration -------------------------------------------------------------------------


@pytest.fixture
def celery_app(unit_settings):
    return create_celery_app(unit_settings)


def test_messages_are_json_only(celery_app):
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]


def test_no_result_backend_postgres_is_the_source_of_truth(celery_app):
    assert celery_app.conf.task_ignore_result is True
    assert not celery_app.conf.result_backend


def test_delivery_settings(celery_app):
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1


def test_never_eager(celery_app):
    """Eager mode runs tasks in-process and skips serialization and the broker."""
    assert celery_app.conf.task_always_eager is False


def test_visibility_timeout_and_limits_come_from_settings(celery_app, unit_settings):
    options = celery_app.conf.broker_transport_options
    assert options["visibility_timeout"] == unit_settings.celery_visibility_timeout_seconds
    assert options["visibility_timeout"] > unit_settings.lease_seconds
    assert celery_app.conf.task_time_limit == unit_settings.celery_task_time_limit_seconds
    assert celery_app.conf.task_default_queue == unit_settings.celery_queue


def test_run_job_task_is_registered(celery_app):
    assert RUN_JOB_TASK in celery_app.tasks
    assert CELERY_TASK_NAMES["run_job"] == RUN_JOB_TASK


def test_creating_the_app_does_not_connect_to_redis(unit_settings):
    # unit_settings points at an unreachable host; creating the app must not touch it.
    create_celery_app(unit_settings)


# --- Settings ------------------------------------------------------------------------------------


def test_visibility_timeout_must_exceed_the_lease(unit_settings):
    values = {**unit_settings.model_dump(), "lease_seconds": 600}
    values["celery_visibility_timeout_seconds"] = 600
    with pytest.raises(ValidationError, match="longer than LEASE_SECONDS"):
        Settings(**values)


def test_provider_timeout_must_fit_in_half_a_lease(unit_settings):
    values = {**unit_settings.model_dump(), "lease_seconds": 30, "provider_timeout_seconds": 16}
    with pytest.raises(ValidationError, match="at most half of LEASE_SECONDS"):
        Settings(**values)
    values["provider_timeout_seconds"] = 15
    assert Settings(**values).heartbeat_seconds == 7.5  # lease / 4


def test_task_time_limit_must_exceed_the_stage_limits(unit_settings):
    values = {**unit_settings.model_dump(), "celery_task_time_limit_seconds": 1260}
    with pytest.raises(ValidationError, match="longer than the stage time limits"):
        Settings(**values)  # 60 + 300 + 900 = 1260: not strictly longer
    values["celery_task_time_limit_seconds"] = 1261
    Settings(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"chunk_overlap_chars": 4000}, "overlap < target"),
        ({"chunk_target_chars": 7000}, "target <= max"),
        ({"summary_final_max_chars": 6000}, "more than twice summary_final_max_chars"),
        ({"summary_chunk_max_chars": 6000}, "more than twice summary_chunk_max_chars"),
        ({"pdf_max_text_chars": 10_000_000}, "PDF_PARSER_OUTPUT_MAX_MB is too small"),
    ],
)
def test_inconsistent_pdf_settings_are_refused_at_startup(unit_settings, overrides, message):
    """Not on the first job: a bad value would otherwise leave every job stuck."""
    with pytest.raises(ValidationError, match=message):
        Settings(**{**unit_settings.model_dump(), **overrides})


def test_only_the_dispatchers_app_fails_fast_on_connection_errors(unit_settings):
    """The dispatcher has its own backoff (and holds row locks while publishing); workers keep
    Celery's reconnect behaviour so they resume consuming after a Redis outage."""
    publishing = create_celery_app(unit_settings, for_publishing=True).conf
    consuming = create_celery_app(unit_settings).conf
    assert publishing.broker_transport_options["max_retries"] == 0
    assert "max_retries" not in consuming.broker_transport_options


def test_consuming_app_reconnects_forever_and_keeps_running_tasks(unit_settings):
    conf = create_celery_app(unit_settings).conf
    assert conf.broker_connection_max_retries is None
    assert conf.worker_cancel_long_running_tasks_on_connection_loss is False


# --- The Celery task: database outages must not lose the message --------------------------------


class _Retry(Exception):
    pass


class FakeTask:
    """Stands in for a bound Celery task: records retry() calls."""

    def __init__(self, retries=0):
        self.request = type("Request", (), {"id": "task-1", "retries": retries})()
        self.retry_calls = []

    def retry(self, **kwargs):
        self.retry_calls.append(kwargs)
        return _Retry()  # real Celery raises a Retry exception; `raise task.retry()` re-raises


def _service_raising(monkeypatch, exc):
    class Service:
        def run(self, job_id, task_id=None):
            raise exc

    import app.queue.worker_process as worker_process

    monkeypatch.setattr(worker_process, "get_execution_service", lambda: Service())


@pytest.mark.parametrize("retries", [0, 3, 50])
def test_database_outage_makes_the_message_come_back_later(monkeypatch, retries):
    from sqlalchemy.exc import OperationalError

    from app.queue.tasks import run_job

    _service_raising(monkeypatch, OperationalError("SELECT 1", {}, Exception("down")))
    task = FakeTask(retries=retries)

    with pytest.raises(_Retry):
        run_job(task, "00000000-0000-0000-0000-000000000001")

    [call] = task.retry_calls
    assert call["max_retries"] is None  # keep trying while the database is down
    assert call["countdown"] == dispatch_backoff_seconds(retries + 1)


def test_other_errors_are_not_retried(monkeypatch):
    from app.queue.tasks import run_job

    _service_raising(monkeypatch, ValueError("a bug"))
    task = FakeTask()
    with pytest.raises(ValueError):
        run_job(task, "00000000-0000-0000-0000-000000000001")
    assert task.retry_calls == []


def test_message_with_a_malformed_job_id_is_dropped(monkeypatch):
    from app.queue.tasks import run_job

    _service_raising(monkeypatch, AssertionError("must not be called"))
    run_job(FakeTask(), "not-a-uuid")  # no exception: acknowledged and dropped


# --- The dispatcher loop --------------------------------------------------------------------------


class FakeStop:
    """A stop event that records how long the loop waited, and stops after `waits` waits."""

    def __init__(self, waits: int):
        self.remaining = waits
        self.waited: list[float] = []

    def is_set(self) -> bool:
        return self.remaining <= 0

    def wait(self, seconds: float) -> bool:
        self.waited.append(seconds)
        self.remaining -= 1
        return self.is_set()


def test_dispatch_loop_backs_off_on_errors_and_polls_when_idle():
    from app.queue.dispatcher import PassResult, run_forever

    outcomes = iter(
        [
            RuntimeError("database down"),  # backoff(1)
            PassResult(0, 3, "ConnectionError"),  # backoff(2)
            PassResult(5, 0),  # healthy, not a full batch -> poll interval
            PassResult(0, 0),  # idle -> poll interval
        ]
    )

    class FakeDispatcher:
        batch_size = 50

        def run_once(self):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    stop = FakeStop(waits=4)
    run_forever(FakeDispatcher(), poll_seconds=0.5, stop=stop)
    assert stop.waited == [1, 2, 0.5, 0.5]


def test_dispatch_loop_goes_again_immediately_after_a_full_batch():
    from app.queue.dispatcher import PassResult, run_forever

    passes = []

    class FullThenEmpty:
        batch_size = 2

        def run_once(self):
            passes.append(1)
            return PassResult(2, 0) if len(passes) == 1 else PassResult(0, 0)

    stop = FakeStop(waits=1)
    run_forever(FullThenEmpty(), poll_seconds=0.5, stop=stop)
    assert len(passes) == 2 and stop.waited == [0.5]  # no wait between the two passes


def test_heartbeat_must_be_well_under_the_lease(unit_settings):
    values = {**unit_settings.model_dump(), "lease_seconds": 30, "provider_timeout_seconds": 10}
    assert Settings(**values).heartbeat_seconds == 7.5
    assert Settings(**{**values, "heartbeat_interval_seconds": 10}).heartbeat_seconds == 10
    with pytest.raises(ValidationError, match="at most a third of LEASE_SECONDS"):
        Settings(**{**values, "heartbeat_interval_seconds": 10.5})


def test_recovery_interval_must_not_exceed_the_lease(unit_settings):
    values = {**unit_settings.model_dump(), "lease_seconds": 20, "provider_timeout_seconds": 10}
    Settings(**{**values, "recovery_interval_seconds": 20})
    with pytest.raises(ValidationError, match="RECOVERY_INTERVAL_SECONDS must not exceed"):
        Settings(**{**values, "recovery_interval_seconds": 21})


def test_lost_message_grace_must_cover_a_few_recovery_passes(unit_settings):
    values = {**unit_settings.model_dump(), "recovery_interval_seconds": 5}
    Settings(**{**values, "lost_message_grace_seconds": 10})
    with pytest.raises(ValidationError, match="LOST_MESSAGE_GRACE_SECONDS must be at least twice"):
        Settings(**{**values, "lost_message_grace_seconds": 9})
