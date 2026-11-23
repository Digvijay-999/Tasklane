"""Estimated provider cost from token counts and configured prices (pure).

There are no built-in prices: they change, differ per account and model, and a wrong number that
looks official is worse than none. An estimate exists only when both prices are configured
(PROVIDER_INPUT_PRICE_PER_MILLION_TOKENS, PROVIDER_OUTPUT_PRICE_PER_MILLION_TOKENS) and the
provider reported both token counts. Decimal, not float, so 0.1 + 0.2 stays 0.3.
"""

from dataclasses import dataclass
from decimal import Decimal

CURRENCY = "USD"
_MILLION = Decimal(1_000_000)
_PLACES = Decimal("0.000001")  # a millionth of a dollar is plenty for an estimate


@dataclass(frozen=True)
class Pricing:
    input_per_million: Decimal
    output_per_million: Decimal

    def to_record(self) -> dict[str, str]:
        return {
            "currency": CURRENCY,
            "input_price_per_million_tokens": str(self.input_per_million),
            "output_price_per_million_tokens": str(self.output_per_million),
        }


def estimate_cost(
    pricing: Pricing | None, prompt_tokens: int | None, completion_tokens: int | None
) -> Decimal | None:
    """The estimated cost in CURRENCY, rounded to six places, or None if it can't be known."""
    if pricing is None or prompt_tokens is None or completion_tokens is None:
        return None
    cost = (
        Decimal(prompt_tokens) * pricing.input_per_million
        + Decimal(completion_tokens) * pricing.output_per_million
    ) / _MILLION
    return cost.quantize(_PLACES)


def cost_record(
    pricing: Pricing | None, prompt_tokens: int | None, completion_tokens: int | None
) -> dict[str, str] | None:
    """`{"amount": "0.000123", "currency": "USD", <prices>}` for manifests, or None."""
    cost = estimate_cost(pricing, prompt_tokens, completion_tokens)
    if cost is None or pricing is None:
        return None
    return {"amount": str(cost), **pricing.to_record()}
