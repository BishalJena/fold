from __future__ import annotations

from copy import deepcopy
from typing import Any, Never

from agno.db.base import SessionType
from agno.db.in_memory import InMemoryDb
from agno.session import AgentSession, Session, TeamSession, WorkflowSession

from foldos.bridge import SyncBridge
from foldos.core.session import Store
from foldos.core.types import Event, Scope


class FoldosDb(InMemoryDb):
    def __init__(self, store: Store, tenant: str = "acme") -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.store = store
        self.tenant = tenant
        self.bridge = SyncBridge()
        self._suppress_events = False
        if not self._sessions:
            self._rehydrate()

    def close(self) -> None:
        self.bridge.close()

    def _rehydrate(self) -> None:
        restored: list[dict[str, Any]] = []
        for scope in self.bridge.run(self.store.scopes()):
            for event in self.bridge.run(self.store.read_events(scope)):
                if event.kind != "agno_session":
                    continue
                session = deepcopy(event.payload.get("session"))
                if not isinstance(session, dict):
                    continue
                if event.payload.get("deleted"):
                    restored = [
                        existing
                        for existing in restored
                        if not (
                            existing.get("session_id") == session.get("session_id")
                            and FoldosDb._component_id(existing) == FoldosDb._component_id(session)
                        )
                    ]
                else:
                    self._replace_cached(restored, session)
        self._sessions = restored

    @staticmethod
    def _replace_cached(cache: list[dict[str, Any]], session: dict[str, Any]) -> None:
        session_id = session.get("session_id")
        user_id = session.get("user_id")
        session_type = session.get("session_type")
        component_id = FoldosDb._component_id(session)
        cache[:] = [
            existing
            for existing in cache
            if (
                existing.get("session_id"),
                existing.get("user_id"),
                existing.get("session_type"),
                FoldosDb._component_id(existing),
            )
            != (session_id, user_id, session_type, component_id)
        ]
        cache.append(session)

    @staticmethod
    def _component_id(session: dict[str, Any]) -> str:
        session_type = str(session.get("session_type", "agent")).lower()
        field = {"agent": "agent_id", "team": "team_id", "workflow": "workflow_id"}.get(session_type)
        if field is not None and session.get(field) is not None:
            return str(session[field])
        for candidate in ("agent_id", "team_id", "workflow_id"):
            if session.get(candidate) is not None:
                return str(session[candidate])
        raise ValueError("session must include an agent_id, team_id, or workflow_id")

    def _scope(self, session: dict[str, Any]) -> Scope:
        session_id = session.get("session_id")
        if session_id is None:
            raise ValueError("session must include a session_id")
        return Scope(self.tenant, self._component_id(session), str(session_id), "main")

    @staticmethod
    def _raw_session(session: Session) -> dict[str, Any]:
        raw = session.to_dict()
        if not isinstance(raw, dict):
            raise TypeError("session.to_dict() must return a dictionary")
        if isinstance(session, AgentSession):
            raw["session_type"] = SessionType.AGENT.value
        elif isinstance(session, TeamSession):
            raw["session_type"] = SessionType.TEAM.value
        elif isinstance(session, WorkflowSession):
            raw["session_type"] = SessionType.WORKFLOW.value
        return deepcopy(raw)

    @staticmethod
    def _event_payload(raw: dict[str, Any], deleted: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"session": deepcopy(raw), "type": str(raw["session_type"])}
        if deleted:
            payload["deleted"] = True
        return payload

    def _append(self, scope: Scope, events: list[Event]) -> None:
        expected = self.bridge.run(self.store.head(scope))
        self.bridge.run(self.store.append(scope, events, expected))

    def upsert_session(
        self, session: Session, deserialize: bool | None = True
    ) -> Session | dict[str, Any] | None:
        snapshot = deepcopy(self._sessions)
        try:
            result = super().upsert_session(session, deserialize)
            if self._suppress_events:
                return result
            raw = self._raw_session(session)
            self._append(self._scope(raw), [Event("agno_session", self._event_payload(raw), actor="agno")])
            return result
        except Exception:
            self._sessions = snapshot
            raise

    def delete_session(self, session_id: str, user_id: str | None = None) -> bool:
        snapshot = deepcopy(self._sessions)
        matches = [
            deepcopy(session)
            for session in snapshot
            if session.get("session_id") == session_id and (user_id is None or session.get("user_id") == user_id)
        ]
        try:
            deleted = super().delete_session(session_id, user_id)
            if self._suppress_events:
                return deleted
            if deleted:
                for session in matches:
                    self._append(
                        self._scope(session),
                        [Event("agno_session", self._event_payload(session, deleted=True), actor="agno")],
                    )
            return deleted
        except Exception:
            self._sessions = snapshot
            raise

    def upsert_sessions(
        self, sessions: list[Session], deserialize: bool | None = True, preserve_updated_at: bool = False
    ) -> list[Session | dict[str, Any]]:
        snapshot = deepcopy(self._sessions)
        try:
            self._suppress_events = True
            try:
                results = super().upsert_sessions(sessions, deserialize, preserve_updated_at)
            finally:
                self._suppress_events = False
            events_by_scope: dict[Scope, list[Event]] = {}
            for session in sessions:
                raw = self._raw_session(session)
                events_by_scope.setdefault(self._scope(raw), []).append(
                    Event("agno_session", self._event_payload(raw), actor="agno")
                )
            for scope, events in events_by_scope.items():
                self._append(scope, events)
            return results
        except Exception:
            self._sessions = snapshot
            raise

    def delete_sessions(self, session_ids: list[str], user_id: str | None = None) -> None:
        snapshot = deepcopy(self._sessions)
        identifiers = set(session_ids)
        matches = [
            deepcopy(session)
            for session in snapshot
            if session.get("session_id") in identifiers and (user_id is None or session.get("user_id") == user_id)
        ]
        try:
            self._suppress_events = True
            try:
                super().delete_sessions(session_ids, user_id)
            finally:
                self._suppress_events = False
            events_by_scope: dict[Scope, list[Event]] = {}
            for session in matches:
                scope = self._scope(session)
                events_by_scope.setdefault(scope, []).append(
                    Event("agno_session", self._event_payload(session, deleted=True), actor="agno")
                )
            for scope, events in events_by_scope.items():
                self._append(scope, events)
        except Exception:
            self._sessions = snapshot
            raise

    def add_user_memory(self, *args: Any, **kwargs: Any) -> Never:
        raise NotImplementedError("FoldosDb does not support user memory mutations")

    def upsert_user_memory(self, *args: Any, **kwargs: Any) -> Never:
        raise NotImplementedError("FoldosDb does not support user memory mutations")

    def replace_user_memory(self, *args: Any, **kwargs: Any) -> Never:
        raise NotImplementedError("FoldosDb does not support user memory mutations")

    def delete_user_memory(self, *args: Any, **kwargs: Any) -> Never:
        raise NotImplementedError("FoldosDb does not support user memory mutations")

    def clear_user_memories(self, *args: Any, **kwargs: Any) -> Never:
        raise NotImplementedError("FoldosDb does not support user memory mutations")

    def clear(self, *args: Any, **kwargs: Any) -> Never:
        raise NotImplementedError("FoldosDb does not support user memory mutations")
