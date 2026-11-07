"""Per-process state for Celery worker processes (database engine and execution service).

Celery's default worker starts a parent process and *forks* child processes that run the tasks.
A forked child is a copy of its parent, including open sockets. If the parent had opened database
connections, every child would share the same sockets and their queries would get mixed up (like
Node `cluster` workers sharing one `pg` connection).

So: nothing here is created at import time or in the parent. The first task a process runs
creates that process's own engine. The process id is recorded, and if this module ever finds
itself in a different process than the one that created the state (a fork happened after
creation), it discards the inherited state without closing the parent's connections and builds
its own.
"""

import logging
import os
import socket
import threading
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Engine

from app.config import Settings, get_settings
from app.db.session import create_db_engine, create_session_factory
from app.domain.retry import RetryPolicy
from app.providers.factory import create_provider
from app.services.execution import JobExecutionService
from app.storage.local import LocalStorage
from app.workflows.pdf.chunking import ChunkingSettings
from app.workflows.pdf.sandbox import ParserLimits
from app.workflows.pdf.stages import PdfConfig
from app.workflows.pdf.summarize import SummarySettings
from app.workflows.registry import WorkflowConfig, stages_for
from app.workflows.repo.archive import ZipLimits
from app.workflows.repo.stages import RepoConfig

logger = logging.getLogger(__name__)

# Each worker process runs one task at a time (prefetch 1), so it needs very few connections.
WORKER_POOL_SIZE = 2


@dataclass
class _ProcessState:
    pid: int
    engine: Engine
    service: JobExecutionService


_state: _ProcessState | None = None
_lock = threading.Lock()


def worker_id() -> str:
    """worker@<container hostname>:<pid>. The pid matters: a container runs several processes."""
    return f"worker@{socket.gethostname()}:{os.getpid()}"


def get_execution_service() -> JobExecutionService:
    global _state
    with _lock:
        if _state is not None and _state.pid != os.getpid():
            # Inherited from a parent process: never use (or close) the parent's connections.
            _state.engine.dispose(close=False)
            _state = None
        if _state is None:
            _state = _create_state()
        return _state.service


def shutdown_worker_process() -> None:
    global _state
    with _lock:
        if _state is not None and _state.pid == os.getpid():
            _state.engine.dispose()
        _state = None


def _create_state() -> _ProcessState:
    settings = get_settings()
    engine = create_db_engine(
        settings, pool_size=WORKER_POOL_SIZE, max_overflow=0, application_name="tasklane-worker"
    )
    config = workflow_config(settings)
    service = JobExecutionService(
        create_session_factory(engine),
        worker_id=worker_id(),
        lease=timedelta(seconds=settings.lease_seconds),
        stages_for=lambda workflow_type: stages_for(workflow_type, config),
        storage=LocalStorage(settings.storage_root),
        retry_policy=RetryPolicy(
            base_seconds=settings.retry_base_seconds, max_seconds=settings.retry_max_seconds
        ),
        fault_injection_enabled=settings.enable_fault_injection,
    )
    logger.info(
        "worker process ready",
        extra={"worker_id": service.worker_id, "provider": config.provider.name},
    )
    return _ProcessState(pid=os.getpid(), engine=engine, service=service)


def workflow_config(settings: Settings) -> WorkflowConfig:
    """Translate settings into the plain configuration objects that stage code receives."""
    mib = 1024 * 1024
    chunking = ChunkingSettings(
        target_chars=settings.chunk_target_chars,
        max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars,
    )
    summary = SummarySettings(
        chunk_max_chars=settings.summary_chunk_max_chars,
        final_max_chars=settings.summary_final_max_chars,
        combine_max_input_chars=settings.summary_combine_max_input_chars,
    )
    return WorkflowConfig(
        pdf=PdfConfig(
            limits=ParserLimits(
                max_pages=settings.pdf_max_pages,
                max_chars=settings.pdf_max_text_chars,
                memory_bytes=settings.pdf_parser_memory_mb * mib,
                output_max_bytes=settings.pdf_parser_output_max_mb * mib,
            ),
            validate_timeout_seconds=settings.pdf_validate_timeout_seconds,
            extract_timeout_seconds=settings.pdf_extract_timeout_seconds,
            summarize_timeout_seconds=settings.summarize_timeout_seconds,
            provider_timeout_seconds=settings.provider_timeout_seconds,
            keep_alive_interval_seconds=settings.keep_alive_interval_seconds,
            chunking=chunking,
            summary=summary,
        ),
        repo=RepoConfig(
            zip=ZipLimits(
                max_entries=settings.zip_max_entries,
                max_total_bytes=settings.zip_max_total_bytes,
                max_file_bytes=settings.zip_max_file_bytes,
                max_compression_ratio=settings.zip_max_compression_ratio,
                ratio_check_min_bytes=settings.zip_ratio_check_min_bytes,
                max_path_length=settings.zip_max_path_length,
                max_path_depth=settings.zip_max_path_depth,
                memory_bytes=settings.zip_sandbox_memory_mb * mib,
            ),
            excluded_dirs=settings.repo_excluded_dir_names,
            validate_timeout_seconds=settings.zip_validate_timeout_seconds,
            extract_timeout_seconds=settings.zip_extract_timeout_seconds,
            summarize_timeout_seconds=settings.summarize_timeout_seconds,
            provider_timeout_seconds=settings.provider_timeout_seconds,
            keep_alive_interval_seconds=settings.keep_alive_interval_seconds,
            chunking=chunking,
            summary=summary,
            max_provider_calls=settings.repo_max_provider_calls,
        ),
        provider=create_provider(settings.provider, stub_delay_seconds=settings.stub_delay_seconds),
    )
