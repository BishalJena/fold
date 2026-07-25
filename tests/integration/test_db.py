from __future__ import annotations

import inspect

import pytest
from agno.db.base import SessionType
from agno.session import AgentSession, TeamSession

from foldos.core.store import MemoryStore
from foldos.core.types import Event, Scope
from foldos.db import FoldosDb


def test_session_mutation_signatures_match_agno() -> None:
    assert list(inspect.signature(FoldosDb.upsert_session).parameters) == ["self", "session", "deserialize"]
    assert list(inspect.signature(FoldosDb.delete_session).parameters) == ["self", "session_id", "user_id"]
    assert list(inspect.signature(FoldosDb.upsert_sessions).parameters) == [
        "self",
        "sessions",
        "deserialize",
        "preserve_updated_at",
    ]
    assert list(inspect.signature(FoldosDb.delete_sessions).parameters) == ["self", "session_ids", "user_id"]


def test_session_crud_is_recorded_in_component_scoped_ledger() -> None:
    store = MemoryStore()
    db = FoldosDb(store)
    session = AgentSession(session_id="s1", agent_id="agent-a", user_id="user-a")
    db.upsert_session(session)
    scope = Scope("acme", "agent-a", "s1", "main")
    events = db.bridge.run(store.read_events(scope))
    assert len(events) == 1
    assert events[0].kind == "agno_session"
    assert events[0].actor == "agno"
    expected_session = {**session.to_dict(), "session_type": SessionType.AGENT.value}
    assert events[0].payload == {"session": expected_session, "type": "agent"}
    assert db.delete_session("s1", "user-a")
    events = db.bridge.run(store.read_events(scope))
    assert events[-1].payload == {"session": expected_session, "type": "agent", "deleted": True}
    db.close()


def test_composite_component_keys_do_not_collide() -> None:
    store = MemoryStore()
    db = FoldosDb(store, tenant="tenant-a")
    agent = AgentSession(session_id="shared", agent_id="agent-a")
    team = TeamSession(session_id="shared", team_id="team-a")
    db.upsert_session(agent)
    db.upsert_session(team)
    assert db.bridge.run(store.head(Scope("tenant-a", "agent-a", "shared"))) == 1
    assert db.bridge.run(store.head(Scope("tenant-a", "team-a", "shared"))) == 1
    db.close()


def test_bulk_operations_append_one_batch_without_duplicate_events() -> None:
    store = MemoryStore()
    db = FoldosDb(store)
    sessions = [AgentSession(session_id="s1", agent_id="agent-a"), AgentSession(session_id="s2", agent_id="agent-a")]
    db.upsert_sessions(sessions)
    scope = Scope("acme", "agent-a", "s1")
    assert db.bridge.run(store.head(scope)) == 1
    assert db.bridge.run(store.head(Scope("acme", "agent-a", "s2"))) == 1
    db.delete_sessions(["s1", "s2"])
    assert db.bridge.run(store.head(scope)) == 2
    assert db.bridge.run(store.head(Scope("acme", "agent-a", "s2"))) == 2
    db.close()


def test_append_failure_rolls_back_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore()
    db = FoldosDb(store)

    def fail(coro: object, *args: object, **kwargs: object) -> int:
        if inspect.iscoroutine(coro):
            coro.close()
        raise RuntimeError("append failed")

    monkeypatch.setattr(db.bridge, "run", fail)
    with pytest.raises(RuntimeError, match="append failed"):
        db.upsert_session(AgentSession(session_id="s1", agent_id="agent-a"))
    assert db._sessions == []
    db.close()


def test_rehydrates_empty_cache_from_raw_agno_session_events() -> None:
    store = MemoryStore()
    session = AgentSession(session_id="s1", agent_id="agent-a", user_id="user-a")
    scope = Scope("acme", "agent-a", "s1")
    bridge_db = FoldosDb(store)
    raw = {**session.to_dict(), "session_type": SessionType.AGENT.value}
    bridge_db.bridge.run(
        store.append(scope, [Event("agno_session", {"session": raw, "type": "agent"}, actor="agno")], 0)
    )
    bridge_db.close()
    restored = FoldosDb(store)
    assert restored.get_session("s1", SessionType.AGENT, "user-a") is not None
    restored.close()


@pytest.mark.parametrize(
    "method", ["add_user_memory", "replace_user_memory", "delete_user_memory", "clear_user_memories"]
)
def test_user_memory_mutations_are_unsupported_before_cache_changes(method: str) -> None:
    db = FoldosDb(MemoryStore())
    before = list(db._memories)
    with pytest.raises(NotImplementedError):
        getattr(db, method)(None, None)
    assert db._memories == before
    db.close()
