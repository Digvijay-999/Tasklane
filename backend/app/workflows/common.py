"""Helpers shared by the stages of every workflow (PDF and repository).

- `Throttle`: report progress without writing an event for every tiny step.
- `verified_input_path`: the uploaded file, after checking it still exists and is unchanged.
- `register_artifact`: describe a written artifact file and hand it to the worker.
- `SummaryCaller`: everything around one provider call in a summarize stage: fault injection,
  secret scrubbing for a real (remote) provider, the summary cache (so a retry doesn't pay for
  chunks or combine rounds it already summarized), the stage deadline, pacing waits that keep
  checking for fencing, recording every call, capping the output length, and progress.
"""

import hashlib
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opentelemetry.trace import SpanKind

from app.domain.enums import ProviderCallPurpose
from app.domain.errors import JobError
from app.observability.redaction import scrub
from app.observability.tracing import set_error, span
from app.providers.base import (
    Provider,
    ProviderCallRecord,
    ProviderError,
    SummarizeOptions,
    SummaryResult,
)
from app.providers.faults import with_fault_injection
from app.storage.workspace import JobWorkspace, sha256_file
from app.workflows.base import ArtifactOutput, StageContext

logger = logging.getLogger(__name__)

# work/{job_id}/summaries/{chunk_id}.json (a chunk) and combine-<sha256>.json (a combine call):
# summaries of exactly this input text, reused by later attempts.
CHUNK_SUMMARY_DIR = "summaries"
_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
_PACING_SLICE_SECONDS = 1.0


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
        # A real provider gets the text with secrets replaced; the stub keeps Phase 10 behaviour.
        self.scrubs = bool(getattr(provider, "remote", False))
        self.secrets_found: dict[str, int] = {}  # per kind, in the document's chunks only
        self.expected = max(1, expected_calls)
        self.stage_timeout_seconds = stage_timeout_seconds
        self.provider_timeout_seconds = provider_timeout_seconds
        self.deadline = time.monotonic() + stage_timeout_seconds
        # "done" counts the summaries the result is built from (cached or not), so the stage's
        # output is the same whether or not a retry reused cached chunk summaries.
        self.done = 0
        self.reused = 0
        self._tokens = dict.fromkeys(_TOKEN_FIELDS, 0)
        self._tokens_known = dict.fromkeys(_TOKEN_FIELDS, True)

    def tokens(self, field: str) -> int | None:
        """A token total over every summary the result is built from (cached or not), only if
        the provider reported it for each of them; never estimated."""
        return self._tokens[field] if self._tokens_known[field] else None

    @property
    def total_tokens(self) -> int | None:
        return self.tokens("total_tokens")

    def usage_record(self) -> dict[str, Any]:
        """Token totals and secret-scrubbing counts, for summary.json (never any values)."""
        return {
            **{field: self.tokens(field) for field in _TOKEN_FIELDS},
            "secret_scrubbing": self.secret_scrubbing(),
        }

    def secret_scrubbing(self) -> dict[str, Any] | None:
        """Counts only, never values. None for the stub (nothing leaves the machine)."""
        if not self.scrubs:
            return None
        return {
            "replaced": sum(self.secrets_found.values()),
            "by_kind": dict(sorted(self.secrets_found.items())),
        }

    def __call__(
        self, text: str, purpose: ProviderCallPurpose, chunk_id: str | None, max_chars: int
    ) -> str:
        if self.scrubs:
            # Before the cache check, so the counts are the same whether or not a retry reused
            # a cached summary. Counted for the document's own text (chunks), not for combine
            # inputs (earlier summaries).
            text, found = scrub(text)
            if purpose == ProviderCallPurpose.CHUNK:
                for kind, n in found.items():
                    self.secrets_found[kind] = self.secrets_found.get(kind, 0) + n
        # The cache is keyed by the exact text sent, so a changed chunk or scrubbing rule never
        # reuses a summary of other text.
        input_sha256 = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
        name = chunk_id if purpose == ProviderCallPurpose.CHUNK and chunk_id else None
        cache_path = self.workspace.work(
            f"{CHUNK_SUMMARY_DIR}/{name or 'combine-' + input_sha256}.json"
        )
        cached = self._cached_summary(cache_path, input_sha256, max_chars)
        if cached is not None:  # summarized by an earlier attempt: no provider call
            self.reused += 1
            self._account(cached)
            return str(cached["summary"])
        self._check_deadline()
        provider = self.provider
        options = SummarizeOptions(
            purpose=purpose,
            max_output_chars=max_chars,
            timeout_seconds=self.provider_timeout_seconds,
            sleep=self._sleep,
        )
        # One CLIENT span per real provider call (cached chunk summaries make no call and get no
        # span). Attributes: ids, provider, model, purpose, outcome; never the text.
        with span(
            "provider.summarize",
            kind=SpanKind.CLIENT,
            attributes={
                "job_id": str(self.context.job.job_id),
                "attempt_no": self.context.job.attempt_no,
                "workflow_type": self.context.job.workflow_type,
                "stage": getattr(self.context, "stage_name", None),
                "provider": provider.name,
                "model": provider.model,
                "purpose": purpose.value,
            },
        ) as call_span:
            try:
                result = self._call(provider, text, purpose, chunk_id, options)
            except Exception as exc:
                error_class = exc.error_class if isinstance(exc, JobError) else "internal_error"
                call_span.set_attribute("outcome", "failure")
                set_error(call_span, error_class)
                raise
            call_span.set_attribute("outcome", "success")
            if result.usage is not None:
                for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    value = getattr(result.usage, name)
                    if value is not None:
                        call_span.set_attribute(f"usage.{name}", value)
        return self._finish(result, text, purpose, chunk_id, max_chars, cache_path, input_sha256)

    def _check_deadline(self) -> None:
        if time.monotonic() > self.deadline:
            raise JobError("stage_timeout", limit=int(self.stage_timeout_seconds))

    def _sleep(self, seconds: float) -> None:
        """A provider's pacing wait, in short slices: a fenced worker stops (keep_alive raises)
        and the stage deadline still applies."""
        end = time.monotonic() + seconds
        while (remaining := end - time.monotonic()) > 0:
            self.context.keep_alive()
            self._check_deadline()
            time.sleep(min(remaining, _PACING_SLICE_SECONDS))

    def _call(
        self,
        provider: Provider,
        text: str,
        purpose: ProviderCallPurpose,
        chunk_id: str | None,
        options: SummarizeOptions,
    ) -> SummaryResult:
        started = time.perf_counter()
        try:
            # `text` is untrusted file content and is passed ONLY as the data argument; nothing
            # from it reaches `options` (see the provider contract in providers/base.py).
            return provider.summarize(text, options)
        except JobError:
            raise  # e.g. the stage deadline hit during a pacing wait: no request was made
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
                raise JobError(
                    error_class,
                    retry_after_seconds=getattr(exc, "retry_after_seconds", None),
                ) from exc
            raise

    def _finish(
        self,
        result: SummaryResult,
        text: str,
        purpose: ProviderCallPurpose,
        chunk_id: str | None,
        max_chars: int,
        cache_path: str,
        input_sha256: str,
    ) -> str:
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
        usage = result.usage
        record = {
            "chunk_id": chunk_id,
            "input_sha256": input_sha256,
            # The configured provider and model (what the check compares), not anything the
            # provider reported.
            "provider": self.configured.name,
            "model": self.configured.model,
            "cache_scope": getattr(self.configured, "cache_scope", None),
            "stage_version": self.stage_version,
            "max_output_chars": max_chars,
            "summary": text_out,
            **{field: getattr(usage, field) if usage else None for field in _TOKEN_FIELDS},
        }
        self.workspace.write_json(cache_path, record)
        self._account(record)
        return text_out

    def log_reuse(self) -> None:
        if self.reused:
            logger.info(
                "reused chunk summaries from an earlier attempt",
                extra={"job_id": str(self.context.job.job_id), "reused": self.reused},
            )

    def _account(self, record: dict[str, Any]) -> None:
        self.done += 1
        for field in _TOKEN_FIELDS:
            value = record.get(field)
            if isinstance(value, int):
                self._tokens[field] += value
            else:
                self._tokens_known[field] = False
        self.context.report_progress(min(99, round(self.done * 100 / self.expected)))

    def _cached_summary(
        self, path: str, input_sha256: str, max_chars: int
    ) -> dict[str, Any] | None:
        """A summary from an earlier attempt, if it was made from exactly this text by the same
        provider, model and stage version, with the same length limit."""
        if not self.workspace.exists(path):
            return None
        try:
            cached = self.workspace.read_json(path)
        except ValueError:  # damaged: summarize again
            return None
        matches = (
            isinstance(cached, dict)
            and cached.get("input_sha256") == input_sha256
            and cached.get("provider") == self.configured.name
            and cached.get("model") == self.configured.model
            and cached.get("cache_scope") == getattr(self.configured, "cache_scope", None)
            and cached.get("max_output_chars") == max_chars
            # Bumping stage_version (the stage's logic changed) re-summarizes every chunk.
            and cached.get("stage_version") == self.stage_version
            and isinstance(cached.get("summary"), str)
        )
        return cached if matches else None
