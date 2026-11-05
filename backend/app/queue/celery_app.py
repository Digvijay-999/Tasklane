"""Celery application factory and configuration.

Celery is the task queue (like BullMQ in Node): the dispatcher publishes messages to Redis, and
worker processes consume them. Redis and Celery are only the *transport*: PostgreSQL stays the
source of truth, so no result backend is configured and Celery's own task state is never used.

Settings, in plain language:
- JSON only: messages are plain JSON. Pickle is refused, because unpickling a message can run
  arbitrary code. A message carries only {"job_id": ...}.
- task_acks_late: the worker confirms ("acks") a message *after* the task finishes, not when it
  arrives. If a worker dies between receiving a message and claiming the job, Redis keeps the
  message and delivers it again instead of losing it. Safe only because the claim is
  idempotent: a redelivered message for an already-claimed job is dropped.
- task_reject_on_worker_lost: if the process running a task is killed (out of memory, kill -9),
  the message is put back in the queue. Without it, Celery would acknowledge it, and a message
  whose job was never claimed would be lost.
- worker_prefetch_multiplier=1: each worker process reserves one message at a time. Otherwise a
  busy process would hoard messages (stuck unacknowledged if it died).
- visibility_timeout (Redis): a delivered message not acknowledged within this time is handed to
  another worker. Long jobs therefore see duplicate deliveries, which the claim drops. It is
  also the longest a message can stay stuck if its worker died before claiming. It must exceed
  the lease (checked in Settings).
- task_time_limit: last-resort kill for a runaway task. The job is then left `running` until its
  lease expires (Phase 7 recovery).
"""

from celery import Celery

from app.config import Settings

RUN_JOB_TASK = "tasklane.run_job"

# Outbox task names (stored in the database) -> Celery task names (on the wire).
CELERY_TASK_NAMES = {"run_job": RUN_JOB_TASK}


def create_celery_app(settings: Settings, *, for_publishing: bool = False) -> Celery:
    """Create the Celery app.

    for_publishing=True (the dispatcher): kombu doesn't retry the connection itself, because the
    dispatcher has its own backoff and holds outbox row locks while it publishes. (A failed publish
    can still take a few seconds: the connect timeout doesn't cover DNS, which is slow for a
    stopped container.) Workers reconnect indefinitely, so they ride out a Redis outage and resume
    consuming when it returns.
    """
    app = Celery("tasklane", broker=settings.redis_url)
    transport_options: dict[str, object] = {
        "visibility_timeout": settings.celery_visibility_timeout_seconds,
        "socket_connect_timeout": settings.publish_timeout_seconds,
        "socket_timeout": settings.publish_timeout_seconds,
    }
    if for_publishing:
        transport_options.update({"max_retries": 0, "interval_start": 0})
    app.conf.update(
        # Serialization: JSON only.
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        # No result backend: PostgreSQL is the source of truth.
        result_backend=None,
        task_ignore_result=True,
        # Delivery guarantees (see the module docstring).
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        broker_transport_options=transport_options,
        broker_connection_timeout=settings.publish_timeout_seconds,
        broker_connection_retry_on_startup=True,
        # Workers: keep reconnecting to Redis for as long as it's down (Celery's default gives up
        # after 100 retries, roughly 45 minutes). The dispatcher never uses this path.
        broker_connection_max_retries=None,
        # A Redis disconnect must not cancel tasks that are already running; they keep writing to
        # PostgreSQL, which is the source of truth. (Celery 6 plans to change this default.)
        worker_cancel_long_running_tasks_on_connection_loss=False,
        # Routing: one queue; tests use their own name and Redis database.
        task_default_queue=settings.celery_queue,
        # Limits and concurrency.
        task_time_limit=settings.celery_task_time_limit_seconds,
        worker_concurrency=settings.worker_concurrency,
        # Keep our JSON logging instead of Celery's.
        worker_hijack_root_logger=False,
        # Never run tasks in-process: tests must go through serialization and the broker.
        task_always_eager=False,
        # Quieter: we don't use Celery's monitoring events.
        worker_send_task_events=False,
        task_send_sent_event=False,
        enable_utc=True,
        timezone="UTC",
    )
    # Register the task(s) on this app instance.
    from app.queue.tasks import register_tasks

    register_tasks(app)
    return app
