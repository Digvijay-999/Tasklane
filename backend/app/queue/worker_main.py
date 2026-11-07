"""The Celery app used by the `celery worker` command:

    celery -A app.queue.worker_main worker --loglevel=INFO --hostname=worker@%h

Kept separate from celery_app.py so that importing the factory (e.g. in tests or the dispatcher)
never creates an app from environment settings as a side effect.
"""

import logging

from celery.signals import setup_logging, worker_process_shutdown, worker_ready

from app.config import get_settings
from app.observability.logging import configure_logging
from app.queue.celery_app import create_celery_app
from app.queue.worker_process import shutdown_worker_process

logger = logging.getLogger(__name__)

settings = get_settings()
celery_app = create_celery_app(settings)


@setup_logging.connect
def _configure_logging(**_: object) -> None:
    # Connecting to this signal stops Celery from installing its own logging; we use JSON logs.
    configure_logging(settings.log_level)


@worker_ready.connect
def _warn_about_fault_injection(**_: object) -> None:
    if settings.enable_fault_injection:
        logger.warning(
            "fault injection is ENABLED: jobs may request injected failures "
            "(development only; set ENABLE_FAULT_INJECTION=false to turn it off)"
        )


@worker_process_shutdown.connect
def _close_database_connections(**_: object) -> None:
    shutdown_worker_process()
