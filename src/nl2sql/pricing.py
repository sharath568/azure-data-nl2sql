"""Token prices for cost estimates. Reference constants, not a quote.

USD per 1M tokens (input, output). These are the published OpenAI list prices
for these models as of 2025; Azure OpenAI Global Standard deployments are in
the same range, regional (Standard) and Data Zone deployments can be a little
higher. Check https://azure.microsoft.com/pricing/details/cognitive-services/openai-service/
before you rely on them, and update the date below when you do.
"""

from __future__ import annotations

PRICES_CHECKED = "2025-10"

PRICES_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}

DEFAULT_MODEL = "gpt-4.1-mini"


def price_for(model: str | None) -> tuple[float, float] | None:
    if not model:
        return None
    m = model.lower()
    # Deployment names often carry suffixes ("gpt-4.1-mini-2025-04-14"); match the longest prefix.
    for name in sorted(PRICES_PER_1M, key=len, reverse=True):
        if m.startswith(name):
            return PRICES_PER_1M[name]
    return None


def cost_usd(
    prompt_tokens: int, completion_tokens: int, model: str | None = DEFAULT_MODEL
) -> float | None:
    price = price_for(model)
    if price is None:
        return None
    return (prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000
