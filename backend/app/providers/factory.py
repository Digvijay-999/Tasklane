"""Create the configured provider. Only the stub exists so far (Phase 11 adds an LLM)."""

from app.providers.base import Provider
from app.providers.stub import STUB_PROVIDER, StubProvider


def create_provider(name: str, *, stub_delay_seconds: float = 0) -> Provider:
    if name == STUB_PROVIDER:
        return StubProvider(delay_seconds=stub_delay_seconds)
    raise ValueError(f"unknown summarization provider: {name!r}")
