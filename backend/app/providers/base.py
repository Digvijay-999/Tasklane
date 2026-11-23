"""The summarization provider interface.

    provider.summarize(text, options) -> SummaryResult

Content is DATA, never instructions. `text` comes from an untrusted file. A provider must treat it
purely as material to summarize:
- The stub doesn't interpret text at all; it extracts sentences.
- An LLM provider (Phase 11) must put the content in a clearly delimited data block, tell the
  model in its system instruction to ignore any instructions inside that block, offer the model
  no tools, and never let output trigger actions. The result is only ever stored as text.

A provider must enforce its own timeout (`options.timeout_seconds`), so a slow call can't hold a
worker longer than its lease allows. Network calls can be cancelled by a timeout; that's why
providers, unlike the PDF parser, don't need a separate process.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app.domain.enums import ProviderCallPurpose


@dataclass(frozen=True)
class SummarizeOptions:
    purpose: ProviderCallPurpose
    max_output_chars: int
    timeout_seconds: float
    # How a provider waits before a call (pacing). The summarize stage passes a sleep that keeps
    # checking for fencing and the stage deadline, so a long wait never outlives the attempt.
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class Usage:
    """Token counts as reported by the provider. Never estimated."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class SummaryResult:
    text: str
    provider: str
    model: str
    latency_ms: int
    usage: Usage | None = None  # None: the provider doesn't report usage (e.g. the stub)


class Provider(Protocol):
    name: str
    model: str
    # True when text leaves the machine (a real provider): the summarize stage then replaces
    # secrets in it first (observability/redaction.scrub).
    remote: bool

    def summarize(self, text: str, options: SummarizeOptions) -> SummaryResult:
        """Summarize `text`. Raise a ProviderError subclass for an expected failure; anything
        else is treated as a bug (permanent `internal_error`)."""
        ...


class ProviderError(Exception):
    """An expected provider failure. `error_class` is a key of app.domain.errors.ERRORS, which
    decides whether the job is retried. The exception text is logged, never shown to users."""

    error_class = "provider_unavailable"


class ProviderTimeout(ProviderError):
    error_class = "provider_timeout"


class ProviderUnavailable(ProviderError):
    error_class = "provider_unavailable"


class ProviderRateLimited(ProviderError):
    error_class = "provider_rate_limited"

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds  # the provider's Retry-After, if any


class ProviderAuthError(ProviderError):
    error_class = "provider_auth"


class ProviderRejected(ProviderError):
    error_class = "provider_rejected"


class ProviderBadResponse(ProviderError):
    error_class = "provider_bad_response"


@dataclass(frozen=True)
class ProviderCallRecord:
    """One provider call, as stored in the provider_calls table (success or failure)."""

    purpose: ProviderCallPurpose
    provider: str
    model: str
    latency_ms: int
    success: bool
    input_chars: int
    output_chars: int | None = None
    chunk_id: str | None = None
    error_class: str | None = None
    usage: Usage | None = None
