"""Celery task entry points. Thin, like API routes: parse the message, call a service."""

import logging
import uuid
from typing import Any

from celery import Celery, Task

from app.domain.dispatch import dispatch_backoff_seconds

logger = logging.getLogger(__name__)


def run_job(task: Task, job_id: Any) -> None:
    """Run one job. The message body is only {"job_id": "..."}; everything else comes from the
    database, so a duplicate or stale message can't carry outdated instructions.

    If the database is unavailable, the message must not be lost: with late acks, a task that
    raises is still acknowledged, so a failed claim would leave the job `queued` with no message.
    Instead, ask Celery to deliver it again after a backoff (forever, while the database is down).
    That is always safe: if the claim had already committed before the error, the retry is a
    duplicate and the claim drops it.
    """
    from app.queue.worker_process import get_execution_service
    from app.services.execution import database_unavailable

    try:
        parsed = uuid.UUID(str(job_id))
    except ValueError:
        logger.warning("message with an invalid job_id dropped", extra={"task_id": task.request.id})
        return
    try:
        # traceparent: the dispatcher's publish span (a custom message header), so the attempt
        # continues the job's trace. Missing for messages sent outside the dispatcher.
        traceparent = getattr(task.request, "traceparent", None)
        get_execution_service().run(
            parsed,
            task_id=task.request.id,
            traceparent=traceparent if isinstance(traceparent, str) else None,
        )
    except Exception as exc:
        if not database_unavailable(exc):  # incl. a session ended by the server (25P03)
            raise
        retries = task.request.retries or 0
        countdown = dispatch_backoff_seconds(retries + 1)
        logger.warning(
            "database unavailable; message will be redelivered",
            extra={"job_id": str(parsed), "retry_in_seconds": countdown, "retries": retries},
        )
        raise task.retry(exc=exc, countdown=countdown, max_retries=None) from None


def register_tasks(app: Celery) -> None:
    from app.queue.celery_app import RUN_JOB_TASK

    app.task(name=RUN_JOB_TASK, bind=True)(run_job)
