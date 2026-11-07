"""Create the configured provider. Phase 5: only the stub exists."""

from app.providers.base import Provider
from app.providers.stub import STUB_PROVIDER, StubProvider


def create_provider(name: str) -> Provider:
    if name == STUB_PROVIDER:
        return StubProvider()
    raise ValueError(f"unknown summarization provider: {name!r}")
