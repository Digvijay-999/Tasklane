"""Create the configured provider: the offline stub (default) or an OpenAI-compatible API."""

from app.config import Settings
from app.providers.base import Provider
from app.providers.openai_compatible import OPENAI_COMPATIBLE_PROVIDER, OpenAICompatibleProvider
from app.providers.stub import STUB_PROVIDER, StubProvider


def create_provider(settings: Settings) -> Provider:
    if settings.provider == STUB_PROVIDER:
        return StubProvider(delay_seconds=settings.stub_delay_seconds)
    if settings.provider == OPENAI_COMPATIBLE_PROVIDER:
        problem = settings.provider_key_problem()
        if problem:
            raise ValueError(problem)
        assert settings.provider_api_key and settings.provider_base_url and settings.provider_model
        return OpenAICompatibleProvider(
            base_url=settings.provider_base_url,
            api_key=settings.provider_api_key.get_secret_value().strip(),
            model=settings.provider_model,
            max_output_tokens=settings.provider_max_output_tokens,
            calls_per_minute=settings.provider_max_calls_per_minute,
        )
    raise ValueError(f"unknown summarization provider: {settings.provider!r}")
