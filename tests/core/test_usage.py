from __future__ import annotations

from collections.abc import Iterator

import pytest

from foldos.core import usage


@pytest.fixture(autouse=True)
def _reset_pricing() -> Iterator[None]:
    usage.reset_pricing()
    yield


def test_cost_is_none_without_registry_or_explicit() -> None:
    assert usage.cost("gpt-4", 1000, 500) is None


def test_register_pricing_computes_cost() -> None:
    usage.register_pricing("llama3.2", 3.0, 15.0)
    assert usage.cost("llama3.2", 1_000_000, 1_000_000) == 18.0


def test_explicit_cost_overrides_registry() -> None:
    usage.register_pricing("llama3.2", 3.0, 15.0)
    assert usage.cost("llama3.2", 1_000_000, 1_000_000, explicit_cost_usd=5.0) == 5.0


def test_reset_pricing_isolates_state() -> None:
    usage.register_pricing("llama3.2", 3.0, 15.0)
    usage.reset_pricing()
    assert usage.cost("llama3.2", 1_000_000, 1_000_000) is None
