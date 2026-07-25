from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from foldos.app import create_app


@pytest.mark.asyncio
async def test_agentos_run_emits_llm_call_and_verifies() -> None:
    """A real Agno agent run writes an llm_call ledger event and verifies."""
    app = create_app()
    transport = ASGITransport(app=app)
    session_id = f"agentos-test-{uuid.uuid4().hex[:8]}"

    async with AsyncClient(transport=transport, base_url="http://test", timeout=60.0) as client:
        agents_resp = await client.get("/agents")
        assert agents_resp.status_code == 200
        agents = agents_resp.json()
        agent_ids = {a.get("id") for a in agents}
        assert "analyst" in agent_ids

        run_resp = await client.post(
            "/agents/analyst/runs",
            data={
                "message": "Say hello in exactly one word.",
                "session_id": session_id,
                "stream": "false",
            },
        )
        assert run_resp.status_code == 200, run_resp.text

        events_resp = await client.get(f"/foldos/events/{session_id}")
        assert events_resp.status_code == 200
        events_body = events_resp.json()
        events = events_body.get("events", [])
        llm_calls = [e for e in events if e.get("kind") == "llm_call"]
        assert llm_calls, f"expected at least one llm_call event, got {events}"

        verify_resp = await client.get(f"/foldos/verify/{session_id}")
        assert verify_resp.status_code == 200
        assert verify_resp.json().get("ok") is True
