# FoldOS

**An agent platform whose state is an append-only ledger, making every agent run rewindable, every policy backtestable, and every history tamper-evident -- with end-to-end SigNoz observability.**

Built for the [Agents of SigNoz](https://wemakedevs.org) hackathon (WeMakeDevs x SigNoz).

---

## What makes FoldOS different

| Capability | How |
|---|---|
| **Event-sourced ledger** | Agent state is never overwritten. Every action -- LLM call, tool call, message, policy change -- is an immutable, sequenced event. `fold(events[:n])` reconstructs any past state. |
| **Policy backtesting** | Apply a new budget or policy retroactively against the real event history. See exactly which past actions would have been blocked -- without modifying any data. |
| **Tamper-evident history** | Each event is chained via SHA-256 hashes. A single-bit mutation anywhere breaks the chain, and `/foldos/verify/{session}` catches it. |
| **Counterfactual branching** | Fork the ledger at any event, swap one payload, and replay the rest to see "what if" outcomes side by side. |
| **SigNoz observability** | Every ledger event is simultaneously an OpenTelemetry span, a structured log record, and a set of metrics -- all flowing into SigNoz for traces, dashboards, and alerts. |

---

## Architecture

```
                        +------------------+
                        |   SigNoz         |
                        |  (traces, logs,  |
                        |   metrics,       |
                        |   dashboards,    |
                        |   alerts)        |
                        +--------^---------+
                                 | OTLP (HTTP)
                                 |
+--------+    HTTP    +----------+---------+
| Client | --------> |      FastAPI        |
| /console|          |                     |
+--------+           |  +-- AgentOS -----+ |
                      |  | /agents/*/runs | |
                      |  | /sessions      | |
                      |  +----------------+ |
                      |                     |
                      |  +-- FoldOS ctrl -+ |
                      |  | /foldos/policy | |
                      |  | /foldos/state  | |
                      |  | /foldos/events | |
                      |  | /foldos/verify | |
                      |  | /foldos/back.. | |
                      |  | /foldos/count..| |
                      |  | /foldos/branch | |
                      |  +----------------+ |
                      |                     |
                      |  +-- Core --------+ |
                      |  | MemoryStore    | |
                      |  | Session        | |
                      |  | Reducers/Fold  | |
                      |  | Chain (SHA256) | |
                      |  | Policy engine  | |
                      |  +----------------+ |
                      |                     |
                      |  +-- OTel layer --+ |
                      |  | Emitter        | |
                      |  | TracerProvider | |
                      |  | MeterProvider  | |
                      |  | LoggerProvider | |
                      |  +----------------+ |
                      +---------------------+
                                 |
                      +----------v---------+
                      |    Ollama           |
                      |    (llama3.2)       |
                      +--------------------+
```

**Data flow:** Client sends a prompt to the AgentOS run endpoint. The Agno agent calls Ollama via an OpenAI-compatible client. The instrumented client records an `llm_call` event in the append-only ledger. The store's event sink forwards every event to the OTel `Emitter`, which creates a span, a log record, and increments metrics. All three signals flow to SigNoz over OTLP HTTP.

---

## Setup

### Prerequisites

- **Python 3.11 -- 3.13**
- **[uv](https://docs.astral.sh/uv/)** (recommended) or pip
- **[Ollama](https://ollama.ai)** with `llama3.2` pulled (`ollama pull llama3.2`)
- **[SigNoz](https://signoz.io)** running locally (UI on `localhost:8080`, OTLP on `localhost:4318`)

### Install

```bash
git clone <repo-url> foldos-handoff
cd foldos-handoff

cp .env.example .env          # review and adjust if needed

uv sync --extra dev            # or: pip install -e ".[dev]"
```

### Start dependencies

```bash
# SigNoz (via Foundry compose or Docker -- see casting.yaml)
# Verify:
curl -s http://localhost:8080/api/v1/health

# Ollama
ollama serve                   # if not already running
ollama pull llama3.2
```

### Run FoldOS

```bash
# Option A: uvicorn with factory
.venv/bin/python -m uvicorn foldos.app:create_app --host 127.0.0.1 --port 7777 --factory

# Option B: shorthand
uvicorn foldos.app:create_app --host 127.0.0.1 --port 7777 --factory
```

The app starts on `http://127.0.0.1:7777`.

---

## Quickstart

Start the backend and the Agno Agent UI together:

```bash
python3 scripts/dev.py
```

Then open:

- **Agno chat/management UI**: [http://localhost:3000](http://localhost:3000) — set the endpoint to `http://localhost:7777` in the sidebar.
- **FoldOS ledger console**: [http://localhost:7777/console](http://localhost:7777/console) — budgets, backtests, counterfactuals, chain verification.
- **SigNoz**: [http://localhost:8080](http://localhost:8080) — search for service `foldos`.

Or run the services individually:

```bash
make backend   # FoldOS backend on port 7777
make ui        # Agno UI on port 3000
```

### Try the API

```bash
curl -X POST http://localhost:7777/agents/analyst/runs \
  -F "message=Say hello in one word." \
  -F "session_id=quickstart-1" \
  -F "stream=false"

curl http://localhost:7777/foldos/events/quickstart-1 | python3 -m json.tool

curl http://localhost:7777/foldos/verify/quickstart-1
# {"ok": true, "broken_at": null, "events": ...}
```

---

## Demo beats

These five moments demonstrate FoldOS's core value. The `scripts/demo_scenario.py` script exercises all of them automatically.

### 1. Budget-as-event

Budget limits are ledger events, not environment variables. Setting a policy appends a `policy_set` event to the session's stream:

```bash
curl -X POST http://localhost:7777/foldos/policy \
  -H "Content-Type: application/json" \
  -d '{"session": "demo-1", "key": "budget_usd", "value": 0.50}'
```

The budget is now part of the immutable, auditable history.

### 2. Policy backtest

Ask "what if we had set a tighter budget?" without changing any data:

```bash
curl -X POST http://localhost:7777/foldos/backtest \
  -H "Content-Type: application/json" \
  -d '{"session": "demo-1", "policy": {"key": "budget_usd", "value": 0.001}}'
# Returns {"evaluated": N, "violations": [...]}
```

### 3. Counterfactual branching

Fork the ledger at event N, swap a payload, and compare outcomes:

```bash
curl -X POST http://localhost:7777/foldos/counterfactual \
  -H "Content-Type: application/json" \
  -d '{"session": "demo-1", "event_seq": 1, "payload_overrides": {"content": "rewritten"}, "thread": "what-if"}'
```

Returns both original and branch state side by side.

### 4. Tamper-evident verification

Every event is hash-chained. Verification walks the chain and confirms integrity:

```bash
curl http://localhost:7777/foldos/verify/demo-1
# {"ok": true, "broken_at": null, "events": N}
```

### 5. SigNoz deep link

Every ledger event maps to a SigNoz trace. The trace index lets you jump from a ledger position to the exact span:

```bash
curl "http://localhost:7777/foldos/trace-index?session=demo-1"
```

Then open `http://localhost:8080/trace/{trace_id}?spanId={span_id}` to see the span in SigNoz.

---

## Running the demo script

```bash
# Ensure SigNoz and Ollama are running, then:
.venv/bin/python scripts/demo_scenario.py
```

The script starts the FoldOS service, runs a full scenario (agent run, policy, backtest, counterfactual, verify), prints a summary with SigNoz deep links, and exits 0 on success.

---

## API reference

### AgentOS endpoints (provided by Agno)

| Method | Path | Description |
|---|---|---|
| GET | `/agents` | List registered agents |
| POST | `/agents/{agent_id}/runs` | Run an agent (multipart form: `message`, `session_id`, `stream`) |
| GET | `/sessions` | List sessions |

### FoldOS control endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/foldos/state/{session}` | Reconstructed state at head (or `?as_of=N`) |
| GET | `/foldos/events/{session}` | All events in the session's ledger stream |
| GET | `/foldos/events/{session}/stream` | SSE stream of events (real-time follow) |
| GET | `/foldos/usage/{session}` | Accumulated usage (tokens, cost, tool calls) |
| GET | `/foldos/verify/{session}` | Hash-chain verification |
| GET | `/foldos/attestation/{session}` | Full attestation (verify + head hash) |
| GET | `/foldos/cut` | Cross-stream time cut |
| GET | `/foldos/trace-index` | Map ledger positions to SigNoz trace/span IDs |
| POST | `/foldos/policy` | Append a policy event (e.g., budget limit) |
| POST | `/foldos/message` | Append a message event |
| POST | `/foldos/backtest` | Retroactive policy evaluation (read-only) |
| POST | `/foldos/branch` | Fork the ledger at a given sequence number |
| POST | `/foldos/counterfactual` | Branch + replace one event + compare states |
| POST | `/foldos/replay` | Replay a session with a new prompt (if runner configured) |

### Consoles

| Path / URL | Purpose |
|---|---|
| `/console` | FoldOS ledger console: events, state, budgets, backtests, counterfactuals, chain verification |
| `https://os.agno.com` (connected to `http://localhost:7777`) | Agno’s hosted chat/management UI: agent chat, sessions, traces |
| `ui/agent-ui` (run `scripts/start_agent_ui.sh`) | Self-hosted Agno Agent UI (submodule): same features as above, running locally on port 3000 |

Both Agno UIs connect directly to the running AgentOS backend. CORS is already configured for `https://os.agno.com` and `http://localhost:3000`.

Run everything at once with `python3 scripts/dev.py` or `make dev`.

---

## SigNoz usage summary

FoldOS uses SigNoz as its observability plane across all three signal types:

| Signal | What FoldOS emits | Where it appears in SigNoz |
|---|---|---|
| **Traces** | Every ledger event becomes a span (`foldos.llm.*`, `foldos.tool.*`, `foldos.{span_name}`). Parent-child relationships mirror the agent's logical span tree. | Traces page, filtered by `service.name = foldos` |
| **Logs** | Every event emits a structured log record with severity, payload JSON, and scope attributes. | Logs page, query by `foldos.event_kind` or `foldos.session` |
| **Metrics** | Counters: `foldos.llm.calls`, `foldos.llm.input_tokens`, `foldos.llm.output_tokens`, `foldos.llm.cost_usd`, `foldos.tool.calls`, `foldos.policy.violations`. Histogram: `foldos.llm.latency_ms`. Gauges: `foldos.chain.head_seq`, `foldos.chain.head_hash`. | Metrics page / dashboards |
| **Dashboards** | Pre-built governance dashboard (`foldos/provision/dashboards/foldos.json`) covering LLM usage, tool calls, policy violations, and chain head state. | Dashboards page (import the JSON) |
| **Alerts** | Budget breach alert (`foldos/provision/alerts/budget_breach.json`) fires when `foldos.policy.violations` exceeds 0 in a 5-minute window. | Alerts page (import the JSON) |
| **GenAI semconv** | `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cost_usd` on LLM spans. | SigNoz GenAI / LLM Observability page |

Deep links: `http://localhost:8080/trace/{trace_id}?spanId={span_id}` navigates directly to any span.

---

## Project structure

```
foldos-handoff/
  foldos/
    app.py                  # FastAPI application factory + AgentOS wiring
    bridge.py               # Sync-to-async bridge for Agno's sync DB interface
    db.py                   # FoldosDb: Agno InMemoryDb backed by the ledger
    core/
      types.py              # Event, Scope, ConcurrencyError, PolicyViolation
      store.py              # MemoryStore: append-only event store with policy checks
      session.py            # Session: high-level API for event creation
      reducers.py           # fold(): deterministic state reconstruction from events
      chain.py              # SHA-256 hash chaining and verification
      policy.py             # Invariant system, budget enforcement, backtesting
      counterfactual.py     # Fork + replace + replay for what-if analysis
      clock.py              # Time-to-position cut across streams
      usage.py              # Per-model pricing registry
    otel/
      config.py             # OTel provider setup (tracer, meter, logger)
      emitter.py            # Event-to-span/log/metric emission + trace index
      instrument_openai.py  # Monkey-patch OpenAI client for ledger capture
      traced_tool.py        # OTel-instrumented Agno tool wrapper
    control/
      routes.py             # FastAPI router for /foldos/* endpoints
      models.py             # Pydantic request/response models
      scopes.py             # Scope resolution from partial selectors
    console/
      static/               # Web console (HTML/JS)
    provision/
      dashboards/foldos.json        # SigNoz dashboard definition
      alerts/budget_breach.json     # SigNoz alert rule
      alerts/policy-violations.json # SigNoz policy violation alert
      client.py             # SigNoz provisioning API client
  tests/
    core/                   # Unit tests for ledger, reducers, chain, policy
    contract/               # Contract tests for API routes and models
    integration/            # Integration tests (OTel emitter, AgentOS)
  scripts/
    demo_scenario.py        # Full-stack deterministic demo (this README's demo beats)
    e2e_backend.py          # Backend end-to-end smoke test
    e2e_agno_ollama.py      # Agno + Ollama compatibility gate
  casting.yaml              # Foundry install spec
  casting.yaml.lock         # Foundry lock file
```

---

## Tests

```bash
.venv/bin/pytest -q                    # all tests
.venv/bin/pytest tests/core/ -q        # ledger core only
.venv/bin/ruff check foldos/ tests/    # lint
.venv/bin/mypy                         # type check
```

---

## Disclosures

Built with Claude Code and Devin.

FoldOS's event model was informed by [Statefold](https://github.com/ioteverythin/statefold)'s design; no Statefold code is used.

---

## License

FoldOS application code is provided as a hackathon submission. Key dependencies:
- **Agno** -- Apache-2.0
- **SigNoz** -- MIT (outside `ee/` and `cmd/enterprise/`, which are proprietary)
- **OpenTelemetry SDK** -- Apache-2.0
