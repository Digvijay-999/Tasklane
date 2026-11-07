"""Fault injection: make a provider fail on purpose, to watch retries happen (development only).

A job opts in with the option
    {"fault_injection": {"mode": "transient", "failures": 2}}   or   {"mode": "permanent"}
and it only works when the server setting ENABLE_FAULT_INJECTION is true (the API refuses the
option otherwise, and the worker checks again before running the job).

- transient, failures N: the FIRST provider call of attempts 1..N raises `injected_transient`,
  so attempts 1..N fail and are retried; attempt N+1 runs normally. It depends only on the attempt
  number, so it needs no extra state and behaves the same every time. (failures 3 with the
  default budget of 3 attempts: every attempt fails and the job gives up.)
- permanent: the first provider call raises `injected_permanent`, which is never retried.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.providers.base import Provider, ProviderError, SummarizeOptions, SummaryResult

OPTION_NAME = "fault_injection"


class InjectedTransientFault(ProviderError):
    error_class = "injected_transient"


class InjectedPermanentFault(ProviderError):
    error_class = "injected_permanent"


@dataclass(frozen=True)
class FaultPlan:
    mode: str  # "transient" or "permanent"
    failures: int = 1  # transient only: how many attempts fail


def fault_plan(options: Mapping[str, Any]) -> FaultPlan | None:
    """The job's fault-injection request, or None. (The API has already validated it.)"""
    requested = options.get(OPTION_NAME)
    if not requested:
        return None
    return FaultPlan(mode=str(requested["mode"]), failures=int(requested.get("failures", 1)))


class FaultInjectingProvider:
    """Wraps a real provider; raises once per attempt as the plan says, otherwise delegates."""

    def __init__(self, inner: Provider, plan: FaultPlan, attempt_no: int) -> None:
        self.inner = inner
        self.plan = plan
        self.attempt_no = attempt_no
        self.name = inner.name
        self.model = inner.model
        self._fired = False

    def summarize(self, text: str, options: SummarizeOptions) -> SummaryResult:
        if not self._fired:
            self._fired = True
            if self.plan.mode == "permanent":
                raise InjectedPermanentFault("fault injection: permanent failure")
            if self.attempt_no <= self.plan.failures:
                raise InjectedTransientFault(
                    f"fault injection: attempt {self.attempt_no} of {self.plan.failures} failing"
                )
        return self.inner.summarize(text, options)


def with_fault_injection(
    provider: Provider, options: Mapping[str, Any], attempt_no: int
) -> Provider:
    """The provider to use for this attempt: wrapped if the job asked for fault injection."""
    plan = fault_plan(options)
    return provider if plan is None else FaultInjectingProvider(provider, plan, attempt_no)
