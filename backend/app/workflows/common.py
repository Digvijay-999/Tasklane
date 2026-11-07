"""Helpers shared by the stages of every workflow (PDF and repository).

- `Throttle`: report progress without writing an event for every tiny step.
- `verified_input_path`: the uploaded file, after checking it still exists and is unchanged.
- `register_artifact`: describe a written artifact file and hand it to the worker.
- `SummaryCaller`: everything around one provider call in a summarize stage: fault injection, the
  per-chunk summary cache (so a retry doesn't pay for chunks it already summarized), the stage
  deadline, recording every call, capping the output length, and progress.
"""

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.domain.enums import ProviderCallPurpose
from app.domain.errors import JobError
from app.providers.base import Provider, ProviderCallRecord, ProviderError, SummarizeOptions
from app.providers.faults import with_fault_injection
from app.storage.workspace import JobWorkspace, sha256_file
from app.workflows.base import ArtifactOutput, StageContext

logger = logging.getLogger(__name__)

CHUNK_SUMMARY_DIR = "summaries"  # work/{job_id}/summaries/{chunk_id}.json: per-chunk cache


class Throttle:
    """Report progress at most every `seconds` or every `step` percent, whichever comes first."""

    def __init__(self, report: Callable[[int], None], *, seconds: float = 2.0, step: int = 5):
        self.report = report
        self.seconds = seconds
        self.step = step
        self.last_percent = -100
        self.last_time = 0.0

    def __call__(self, percent: int) -> None:
        now = time.monotonic()
        if percent - self.last_percent >= self.step or now - self.last_time >= self.seconds:
            self.report(percent)
            self.last_percent, self.last_time = percent, now


def verified_input_path(context: StageContext, *, check_hash: bool) -> Path:
    """The uploaded file. Fails with `input_unavailable` if it is gone or (with check_hash) its
    content no longer matches the hash recorded at upload time."""
    upload = context.job.upload
    path = context.workspace.absolute(upload.storage_path)
    if not path.is_file():
        raise JobError("input_unavailable")
    if check_hash and sha256_file(path) != upload.sha256:
        logger.error(
            "uploaded file no longer matches its hash", extra={"upload": str(upload.upload_id)}
        )
        raise JobError("input_unavailable")
    return path


def register_artifact(
    context: StageContext, artifact_type: str, path: str, filename: str, content_type: str
) -> ArtifactOutput:
    artifact = ArtifactOutput(
        artifact_type=str(artifact_type),
        relative_path=path,
        filename=filename,
        content_type=content_type,
        size_bytes=context.workspace.size(path),
        sha256=context.workspace.sha256(path),
    )
    context.save_artifact(artifact)
    return artifact


class SummaryCaller:
    """`caller(text, purpose, chunk_id, max_chars) -> summary`, the SummarizeCall used by the
    map-reduce summarizer. One instance per stage run."""

    def __init__(
        self,
        context: StageContext,
        provider: Provider,
        *,
        stage_version: int,
        chunk_sha256: dict[str, str],
        expected_calls: int,
        stage_timeout_seconds: float,
        provider_timeout_seconds: float,
    ) -> None:
        self.context = context
        self.workspace: JobWorkspace = context.workspace
        self.configured = provider  # what the cache compares (not the fault-injecting wrapper)
        # Wrapped only when the job asked for fault injection (development); see providers/faults.
        self.provider = with_fault_injection(provider, context.job.options, context.job.attempt_no)
        self.stage_version = stage_version
        self.chunk_sha256 = chunk_sha256
        self.expected = max(1, expected_calls)
        self.stage_timeout_seconds = stage_timeout_seconds
        self.provider_timeout_seconds = provider_timeout_seconds
        self.deadline = time.monotonic() + stage_timeout_seconds
        # "done" counts the summaries the result is built from (cached or not), so the stage's
        # output is the same whether or not a retry reused cached chunk summaries.
        self.done = 0
        self.reused = 0
        self._tokens = 0
        self._tokens_known = True

    @property
    def total_tokens(self) -> int | None:
        """Token total, only if the provider reported usage for every summary; never estimated."""
        return self._tokens if self._tokens_known else None

    def __call__(
        self, text: str, purpose: ProviderCallPurpose, chunk_id: str | None, max_chars: int
    ) -> str:
        cache_path = None
        if purpose == ProviderCallPurpose.CHUNK and chunk_id is not None:
            cache_path = self.workspace.work(f"{CHUNK_SUMMARY_DIR}/{chunk_id}.json")
            cached = self._cached_summary(cache_path, self.chunk_sha256[chunk_id], max_chars)
            if cached is not None:  # summarized by an earlier attempt: no provider call
                self.reused += 1
                self._account(cached.get("total_tokens"))
                return str(cached["summary"])
        if time.monotonic() > self.deadline:
            raise JobError("stage_timeout", limit=int(self.stage_timeout_seconds))
        provider = self.provider
        options = SummarizeOptions(
            purpose=purpose,
            max_output_chars=max_chars,
            timeout_seconds=self.provider_timeout_seconds,
        )
        started = time.perf_counter()
        try:
            # `text` is untrusted file content and is passed ONLY as the data argument; nothing
            # from it reaches `options` (see the provider contract in providers/base.py).
            result = provider.summarize(text, options)
        except Exception as exc:
            error_class = exc.error_class if isinstance(exc, ProviderError) else None
            self.context.record_provider_call(
                ProviderCallRecord(
                    purpose=purpose,
                    provider=provider.name,
                    model=provider.model,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    success=False,
                    input_chars=len(text),
                    chunk_id=chunk_id,
                    # Never None: the table requires an error class for failed calls.
                    error_class=error_class or "internal_error",
                )
            )
            if error_class is not None:
                # An expected provider failure: its class decides whether the job retries.
                raise JobError(error_class) from exc
            raise
        # A provider may ignore max_output_chars; enforce it here, so every combine round really
        # shrinks the text (otherwise map-reduce could loop until the stage timeout).
        text_out = result.text[:max_chars]
        # Recording the call also renews the lease, so each call's timeout (at most half the
        # lease) bounds the longest time between renewals.
        self.context.record_provider_call(
            ProviderCallRecord(
                purpose=purpose,
                provider=result.provider,
                model=result.model,
                latency_ms=result.latency_ms,
                success=True,
                input_chars=len(text),
                output_chars=len(text_out),
                chunk_id=chunk_id,
                usage=result.usage,
            )
        )
        total_tokens = result.usage.total_tokens if result.usage is not None else None
        if cache_path is not None and chunk_id is not None:
            self.workspace.write_json(
                cache_path,
                {
                    "chunk_id": chunk_id,
                    "chunk_sha256": self.chunk_sha256[chunk_id],
                    # The configured provider and model (what the check compares), not
                    # result.model: a real provider may answer with a resolved model id.
                    "provider": self.configured.name,
                    "model": self.configured.model,
                    "stage_version": self.stage_version,
                    "max_output_chars": max_chars,
                    "summary": text_out,
                    "total_tokens": total_tokens,
                },
            )
        self._account(total_tokens)
        return text_out

    def log_reuse(self) -> None:
        if self.reused:
            logger.info(
                "reused chunk summaries from an earlier attempt",
                extra={"job_id": str(self.context.job.job_id), "reused": self.reused},
            )

    def _account(self, total_tokens: int | None) -> None:
        self.done += 1
        if total_tokens is None:
            self._tokens_known = False
        else:
            self._tokens += total_tokens
        self.context.report_progress(min(99, round(self.done * 100 / self.expected)))

    def _cached_summary(
        self, path: str, chunk_sha256: str, max_chars: int
    ) -> dict[str, Any] | None:
        """A chunk summary from an earlier attempt, if it was made from exactly this chunk text
        by the same provider, model and stage version, with the same length limit."""
        if not self.workspace.exists(path):
            return None
        try:
            cached = self.workspace.read_json(path)
        except ValueError:  # damaged: summarize again
            return None
        matches = (
            isinstance(cached, dict)
            and cached.get("chunk_sha256") == chunk_sha256
            and cached.get("provider") == self.configured.name
            and cached.get("model") == self.configured.model
            and cached.get("max_output_chars") == max_chars
            # Bumping stage_version (the stage's logic changed) re-summarizes every chunk.
            and cached.get("stage_version") == self.stage_version
            and isinstance(cached.get("summary"), str)
        )
        return cached if matches else None
