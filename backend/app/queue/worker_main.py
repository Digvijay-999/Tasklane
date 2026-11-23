"""The Celery app used by the `celery worker` command:

    celery -A app.queue.worker_main worker --loglevel=INFO --hostname=worker@%h

Kept separate from celery_app.py so that importing the factory (e.g. in tests or the dispatcher)
never creates an app from environment settings as a side effect.
"""

import logging
from urllib.parse import urlsplit

from celery.signals import (
    setup_logging,
    worker_process_init,
    worker_process_shutdown,
    worker_ready,
)

from app.config import get_settings
from app.observability.logging import configure_logging
from app.observability.tracing import flush_telemetry
from app.queue.celery_app import create_celery_app
from app.queue.worker_process import (
    shutdown_worker_process,
    start_presence,
    start_telemetry,
    stop_presence,
)

logger = logging.getLogger(__name__)

settings = get_settings()
# A real provider without a key stops the worker at once, with this message (not on every job).
if (_key_problem := settings.provider_key_problem()) is not None:
    raise SystemExit(f"tasklane worker cannot start: {_key_problem}")
celery_app = create_celery_app(settings)


@setup_logging.connect
def _configure_logging(**_: object) -> None:
    # Connecting to this signal stops Celery from installing its own logging; we use JSON logs.
    # The provider key is masked as a literal wherever it might appear (it is also registered as
    # a sensitive-looking environment variable).
    provider_key = (
        settings.provider_api_key.get_secret_value() if settings.provider_api_key else None
    )
    configure_logging(
        settings.log_level,
        # Both forms: the provider sends the trimmed key.
        secrets=(
            settings.postgres_password.get_secret_value(),
            provider_key,
            provider_key.strip() if provider_key else None,
        ),
    )


@worker_process_init.connect
def _start_child(**_: object) -> None:
    # Runs in each forked child process (the ones that run jobs), before its first task:
    # telemetry (with its own span export thread) and the presence heartbeat.
    start_telemetry()
    start_presence()


@worker_ready.connect
def _warn_about_fault_injection(**_: object) -> None:
    if settings.provider != "stub":
        logger.warning(
            "document text is sent to a remote summarization provider",
            extra={
                "provider": settings.provider,
                "model": settings.provider_model,
                "provider_host": urlsplit(settings.provider_base_url or "").hostname,
            },
        )
    if settings.enable_fault_injection:
        logger.warning(
            "fault injection is ENABLED: jobs may request injected failures "
            "(development only; set ENABLE_FAULT_INJECTION=false to turn it off)"
        )


@worker_process_shutdown.connect
def _close_database_connections(**_: object) -> None:
    stop_presence()  # marks this process stopped, so the dashboard updates at once
    shutdown_worker_process()
    flush_telemetry(settings.otel_export_timeout_seconds)  # the last spans of this child
