from __future__ import annotations

from typing import Protocol

from foldos.control.models import ScopeSelector
from foldos.core.types import Scope


class ScopeStore(Protocol):
    async def scopes(self, session: str | None = None) -> list[Scope]: ...


class AmbiguousScopeError(Exception):
    def __init__(self, available: tuple[str, ...]) -> None:
        self.available = available
        super().__init__(f"ambiguous scope: {', '.join(available)}")


class ScopeResolver:
    def __init__(self, store: ScopeStore) -> None:
        self._store = store

    async def resolve(self, selector: ScopeSelector) -> Scope:
        requested = Scope(selector.tenant, selector.agent, selector.session, selector.thread)
        if {"tenant", "agent", "thread"} & selector.model_fields_set:
            return requested
        available = await self._store.scopes(selector.session)
        if requested in available or not available:
            return requested
        if len(available) == 1:
            return available[0]
        raise AmbiguousScopeError(tuple(scope.key() for scope in available))
