from __future__ import annotations

import pytest

from foldos.control.models import ScopeSelector
from foldos.control.scopes import AmbiguousScopeError, ScopeResolver
from foldos.core.store import MemoryStore
from foldos.core.types import Event, Scope


@pytest.mark.asyncio
async def test_resolver_returns_default_scope_before_it_exists() -> None:
    store = MemoryStore()

    scope = await ScopeResolver(store).resolve(ScopeSelector(session="run-1"))

    assert scope == Scope("acme", "analyst", "run-1", "main")


@pytest.mark.asyncio
async def test_resolver_returns_existing_default_scope() -> None:
    store = MemoryStore()
    default = Scope("acme", "analyst", "run-1", "main")
    await store.append(default, [Event(kind="state_delta", payload={"set": {"x": 1}})], 0)

    assert await ScopeResolver(store).resolve(ScopeSelector(session="run-1")) == default


@pytest.mark.asyncio
async def test_resolver_uses_single_matching_scope_when_default_is_absent() -> None:
    store = MemoryStore()
    only = Scope("other", "agent", "run-1", "trial")
    await store.append(only, [Event(kind="state_delta", payload={"set": {"x": 1}})], 0)

    assert await ScopeResolver(store).resolve(ScopeSelector(session="run-1")) == only


@pytest.mark.asyncio
async def test_resolver_raises_typed_ambiguity_with_available_keys() -> None:
    store = MemoryStore()
    first = Scope("tenant-a", "agent", "run-1", "one")
    second = Scope("tenant-b", "agent", "run-1", "two")
    await store.append(first, [Event(kind="state_delta", payload={"set": {"x": 1}})], 0)
    await store.append(second, [Event(kind="state_delta", payload={"set": {"x": 2}})], 0)

    with pytest.raises(AmbiguousScopeError) as raised:
        await ScopeResolver(store).resolve(ScopeSelector(session="run-1"))

    assert raised.value.available == (first.key(), second.key())


@pytest.mark.asyncio
async def test_explicit_selector_resolves_its_exact_scope() -> None:
    store = MemoryStore()
    scope = Scope("tenant-a", "agent", "run-1", "trial")
    await store.append(scope, [Event(kind="state_delta", payload={"set": {"x": 1}})], 0)

    resolved = await ScopeResolver(store).resolve(
        ScopeSelector(tenant="tenant-a", agent="agent", session="run-1", thread="trial")
    )

    assert resolved == scope
