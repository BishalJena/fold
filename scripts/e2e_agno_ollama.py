"""Real-service compatibility gate: Agno agent backed by Ollama llama3.2.

This script creates an Agno Agent using an OpenAI-compatible Ollama client,
runs a single turn, instruments the underlying OpenAI client so calls are
recorded in the FoldOS ledger, and verifies that telemetry can be exported.

Run from the repo root with the venv active:

    python scripts/e2e_agno_ollama.py
"""

from __future__ import annotations

import os
from pathlib import Path

from agno.agent import Agent
from agno.models.openai import OpenAILike

from foldos.core.session import Session
from foldos.core.store import MemoryStore
from foldos.core.types import Event, Scope
from foldos.db import FoldosDb
from foldos.otel.config import build_providers
from foldos.otel.emitter import Emitter
from foldos.otel.instrument_openai import instrument_openai


def _load_dotenv() -> None:
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def main() -> int:
    _load_dotenv()

    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    print(f"OLLAMA_BASE_URL={ollama_url}")

    scope = Scope("acme", "analyst", "agno-compat", "main")
    providers = build_providers()
    emitter = Emitter(scope, providers)

    def emit_event(_scope: Scope, event: Event) -> None:
        emitter.emit_event(event)

    store = MemoryStore(event_sink=emit_event)
    db = FoldosDb(store, tenant="acme")
    session = Session(store, scope)

    model = OpenAILike(id="llama3.2", api_key="ollama", base_url=ollama_url)
    client = model.get_client()
    instrument_openai(client, session=session)

    agent = Agent(model=model, db=db, session_id=scope.session, user_id="compat-test")

    print("running agent ...")
    response = agent.run("Say exactly 'hello from llama3.2' and nothing else.", stream=False)
    print(f"agent response: {response.content!r}")

    # Force flush telemetry to SigNoz.
    providers.force_flush()

    events = db.bridge.run(store.read_events(scope))
    print(f"ledger contains {len(events)} event(s)")
    kinds = [e.kind for e in events]
    print(f"event kinds: {kinds}")

    has_llm_call = any(e.kind == "llm_call" for e in events)

    db.close()
    providers.shutdown()

    if not has_llm_call:
        print("FAIL: no llm_call event recorded")
        return 1

    print("PASS: Agno + Ollama compatibility gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
