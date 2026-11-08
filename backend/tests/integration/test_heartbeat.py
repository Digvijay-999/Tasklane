"""The heartbeat thread (app/services/heartbeat.py) with a real database and short leases (about a
second), so the tests stay fast while covering several lease periods."""

import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import JobAttempt
from app.db.repositories import attempts as attempt_rows
from app.domain.enums import AttemptOutcome, EventType, JobStatus
from app.domain.retry import RetryPolicy
from app.queue.recovery import Recovery, run_forever
from app.services.execution import JobExecutionService, RunOutcome
from app.services.heartbeat import Heartbeat
from app.workflows.sandbox import SandboxLimits, run_sandboxed
from tests.integration.test_execution import (
    StageThatCalls,
    load,
    new_job,
    session_factory,  # noqa: F401  (fixture)
)

LEASE = timedelta(seconds=1)


def service(session_factory, *stages, heartbeat_interval=0.2) -> JobExecutionService:  # noqa: F811
    return JobExecutionService(
        session_factory,
        worker_id="worker@heartbeat:1",
        lease=LEASE,
        stages_for=lambda _workflow: list(stages),
        heartbeat_interval=heartbeat_interval,
    )


def heartbeat_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("heartbeat-")]


@pytest.fixture
def recovery_loop(session_factory):  # noqa: F811
    """A real recovery loop running every 0.1 s in the background for the whole test."""
    recovery = Recovery(
        session_factory,
        retry_policy=RetryPolicy(base_seconds=60, max_seconds=60),
        lost_message_grace=timedelta(minutes=10),
    )
    passes = []
    original = recovery.expire_leases

    def counting():
        passes.append(original())
        return passes[-1]

    recovery.expire_leases = counting  # type: ignore[method-assign]
    stop = threading.Event()
    thread = threading.Thread(
        target=run_forever, args=(recovery,), kwargs={"interval_seconds": 0.1, "stop": stop}
    )
    thread.start()
    yield passes
    stop.set()
    thread.join(10)


def test_a_long_silent_stage_stays_alive_past_several_lease_periods(
    engine,
    session_factory,  # noqa: F811
    recovery_loop,
):
    job_id = new_job(engine)
    silent = StageThatCalls(lambda context: time.sleep(3.5), name="silent")  # 3.5 leases

    result = service(session_factory, silent).run(job_id)

    assert result.outcome == RunOutcome.SUCCEEDED
    job, [attempt], _ = load(engine, job_id)
    assert job.status == JobStatus.SUCCEEDED and attempt.outcome == AttemptOutcome.SUCCEEDED
    assert attempt.heartbeat_at - attempt.started_at > timedelta(seconds=3)  # kept beating
    assert len(recovery_loop) > 10 and sum(recovery_loop) == 0  # recovery looked, found nothing


def test_without_heartbeats_the_same_stage_is_recovered_and_the_worker_fenced(
    engine,
    session_factory,  # noqa: F811
    recovery_loop,
):
    """The control experiment: the heartbeat is what saved the stage above."""
    job_id = new_job(engine)
    silent = StageThatCalls(lambda context: time.sleep(3.5), name="silent")

    result = service(session_factory, silent, heartbeat_interval=100).run(job_id)

    assert result.outcome == RunOutcome.FENCED
    job, [attempt], events = load(engine, job_id)
    assert attempt.outcome == AttemptOutcome.LEASE_EXPIRED
    assert job.status == JobStatus.RETRY_WAIT  # recovery's decision; the worker wrote nothing
    assert EventType.STAGE_COMPLETED not in [e.event_type for e in events]


def close_attempt_of(engine, job_id) -> None:
    """What recovery does to a presumed-dead worker."""
    with Session(engine) as session, session.begin():
        attempt_id = session.scalar(
            select(JobAttempt.id).where(JobAttempt.job_id == job_id, JobAttempt.ended_at.is_(None))
        )
        attempt_rows.close_attempt(session, attempt_id, AttemptOutcome.LEASE_EXPIRED)


def test_a_fenced_heartbeat_stops_the_stage_and_kills_its_parser(
    engine,
    session_factory,  # noqa: F811
    tmp_path,
):
    job_id = new_job(engine)
    pid_file = tmp_path / "child.pid"
    child = tmp_path / "child.py"
    child.write_text(
        "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
    )
    seen: dict[str, object] = {}

    def parse_for_a_long_time(context):
        def close_soon():
            while not pid_file.exists():
                time.sleep(0.05)
            seen["events_before"] = len(load(engine, job_id)[2])
            close_attempt_of(engine, job_id)

        threading.Thread(target=close_soon, daemon=True).start()
        started = time.monotonic()
        try:
            run_sandboxed(
                child,
                [str(pid_file)],
                limits=SandboxLimits(memory_bytes=512 * 2**20, output_max_bytes=2**20),
                timeout_seconds=60,
                keep_alive=context.keep_alive,  # the worker's real keep_alive
                keep_alive_interval=0.1,
                too_complex_error="pdf_too_complex",
                allowed_errors=(),
            )
        finally:
            seen["seconds"] = time.monotonic() - started

    result = service(session_factory, StageThatCalls(parse_for_a_long_time)).run(job_id)

    assert result.outcome == RunOutcome.FENCED
    assert seen["seconds"] < 5  # the heartbeat noticed within one interval, not at the deadline
    assert not _alive(int(pid_file.read_text()))  # the parser was killed
    _, _, events = load(engine, job_id)
    assert len(events) == seen["events_before"]  # nothing written after the fencing


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def test_writes_after_fencing_are_refused_before_touching_the_database(
    engine,
    session_factory,  # noqa: F811
):
    job_id = new_job(engine)

    def lose_the_job_then_report(context):
        close_attempt_of(engine, job_id)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # wait for the heartbeat to notice
            time.sleep(0.05)
            context.keep_alive()  # raises once fenced
        raise AssertionError("never fenced")

    result = service(session_factory, StageThatCalls(lose_the_job_then_report)).run(job_id)

    assert result.outcome == RunOutcome.FENCED
    _, _, events = load(engine, job_id)
    assert EventType.STAGE_COMPLETED not in [e.event_type for e in events]


@pytest.mark.parametrize("ending", ["success", "failure", "fenced", "error_escapes"])
def test_the_heartbeat_thread_is_cleaned_up_however_the_attempt_ends(
    engine,
    session_factory,  # noqa: F811
    monkeypatch,
    ending,
):
    job_id = new_job(engine)
    alive_during: list[int] = []

    def stage(context):
        alive_during.append(len(heartbeat_threads()))
        if ending == "failure":
            raise RuntimeError("boom")
        if ending == "fenced":
            close_attempt_of(engine, job_id)
            context.report_progress(10)
        if ending == "error_escapes":
            # Recording the failure itself fails (database down): the error leaves run().
            from sqlalchemy.exc import OperationalError

            def down(*args, **kwargs):
                raise OperationalError("UPDATE", {}, Exception("connection lost"))

            monkeypatch.setattr(attempt_rows, "close_attempt", down)
            raise RuntimeError("boom")

    run = service(session_factory, StageThatCalls(stage)).run
    if ending == "error_escapes":
        with pytest.raises(Exception, match="connection lost"):
            run(job_id)
    else:
        run(job_id)

    assert alive_during == [1]  # it was running during the stage...
    assert heartbeat_threads() == []  # ...and is gone afterwards


def test_a_failed_beat_is_retried_at_the_next_interval(
    engine,
    session_factory,  # noqa: F811
    monkeypatch,
):
    job_id = new_job(engine)
    claim = service(session_factory).claim(job_id, None)
    real_renew = attempt_rows.renew_lease
    failures = []

    def flaky_renew(session, attempt_id, lease):
        if not failures:
            failures.append(1)
            raise ConnectionError("database briefly unreachable")
        return real_renew(session, attempt_id, lease)

    monkeypatch.setattr(attempt_rows, "renew_lease", flaky_renew)
    heartbeat = Heartbeat(session_factory, claim.attempt_id, lease=LEASE, interval=0.05)
    with heartbeat:
        deadline = time.monotonic() + 5
        while heartbeat.beats < 2 and time.monotonic() < deadline:
            time.sleep(0.02)

    assert failures and heartbeat.beats >= 2
    assert not heartbeat.fenced.is_set() and not heartbeat.died and not heartbeat.alive


def test_if_the_heartbeat_thread_dies_the_job_still_finishes(
    engine,
    session_factory,  # noqa: F811
    monkeypatch,
):
    """A dead heartbeat only loses the background renewals; the stage's own writes still renew
    (and fence-check) the lease, so a chatty stage is unaffected."""
    job_id = new_job(engine)
    died = []

    def crash(self):
        died.append(self)
        raise SystemExit("heartbeat bug")  # BaseException: escapes the per-beat handler

    monkeypatch.setattr(Heartbeat, "_beat", crash)

    def chatty(context):
        for percent in range(0, 101, 20):
            time.sleep(0.15)
            context.report_progress(percent)

    result = service(session_factory, StageThatCalls(chatty), heartbeat_interval=0.05).run(job_id)

    assert result.outcome == RunOutcome.SUCCEEDED
    assert died and died[0].died and not died[0].alive


def test_heartbeat_renews_with_the_database_clock(engine, session_factory):  # noqa: F811
    job_id = new_job(engine)
    claim = service(session_factory).claim(job_id, None)
    with engine.begin() as connection:  # pretend the lease is nearly over
        connection.execute(
            text(
                "UPDATE job_attempts SET lease_expires_at = started_at + interval '1 ms' "
                "WHERE id = :id"
            ),
            {"id": claim.attempt_id},
        )
    heartbeat = Heartbeat(
        session_factory, claim.attempt_id, lease=timedelta(minutes=5), interval=0.05
    )
    with heartbeat:
        while heartbeat.beats < 1:
            time.sleep(0.02)
    with engine.connect() as connection:
        remaining = connection.execute(
            text("SELECT lease_expires_at - now() FROM job_attempts WHERE id = :id"),
            {"id": claim.attempt_id},
        ).scalar_one()
    assert timedelta(minutes=4) < remaining <= timedelta(minutes=5)
