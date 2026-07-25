from __future__ import annotations

_PRICING: dict[str, tuple[float, float]] = {}


def register_pricing(model: str, input_per_mtok: float, output_per_mtok: float) -> None:
    _PRICING[model] = (input_per_mtok, output_per_mtok)


def cost(model: str, input_tokens: int, output_tokens: int, explicit_cost_usd: float | None = None) -> float | None:
    if explicit_cost_usd is not None:
        return explicit_cost_usd
    rates = _PRICING.get(model)
    if rates is None:
        return None
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000.0


def reset_pricing() -> None:
    _PRICING.clear()
