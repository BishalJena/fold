# Changelog

## 2026-07-25 -- Agno Agent UI integration

- Added Agno's open-source `agent-ui` as a Git submodule under `ui/agent-ui`.
- Created `ui/README.md` and `scripts/start_agent_ui.sh` to run the self-hosted Agno chat/management UI on port 3000.
- Verified the UI dev server starts and serves on `http://localhost:3000`.
- Confirmed CORS from the FoldOS backend already allows `http://localhost:3000`.
- Updated `README.md` quickstart and consoles section to document both the hosted `os.agno.com` UI and the self-hosted option.

## 2025-07-25 -- Initial build

### Cost estimation and budget governance

- Fixed `instrument_openai.py` to estimate input/output tokens from request/response content when Ollama returns zero usage (common with local models). This makes `llm_call` events carry non-zero cost under the illustrative `llama3.2` pricing.
- Registered `enforce_budget` as an invariant on `llm_call`, `tool_call`, `message`, and `policy_set` events so budget policies are enforced live, not just in backtests.
- Updated demo budget calculation to be 20% above the first run's actual cost, guaranteeing the second run is vetoed while keeping the policy set step valid.

### Async client instrumentation

- Fixed `instrument_openai.py` to detect `openai.AsyncOpenAI` by `isinstance` rather than `inspect.iscoroutinefunction`, because the OpenAI SDK's async `create` method is not detected as a coroutine function. This ensures every model call emits an `llm_call` event reliably.

### Agent/control-plane alignment

- Aligned the AgentOS agent id (`analyst`) with the control-plane `ScopeSelector` default agent so queries and policies resolve to the same ledger scope. Updated `.env`, `.env.example`, README, and tests accordingly.

### Traced tools

- Extended `traced_tool.py` to accept a callable session source (like `instrument_openai`), allowing tools decorated at module import time to resolve the per-request FoldOS session at call time.
- Added a sample `get_foldos_status` tool to the demo agent, demonstrating tool-call tracing support.

### Demo script

- Reworked `scripts/demo_scenario.py` step B2 to exercise a live policy veto on the second agent run. The script now asserts that AgentOS returns status `ERROR` with the budget message, the ledger head stays unchanged, and no new `llm_call` is appended.

## 2025-07-25 -- Initial build

### Project setup

- Established the FoldOS project as an independent `foldos-handoff` app, separate from the reference spike code.
- Created `pyproject.toml` with pinned dependencies: `agno[os]==2.8.2`, `openai`, `fastmcp`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`.
- Added `.env.example` with all configuration knobs (tenant, agent, host, port, Ollama URL, SigNoz URL, OTLP endpoint).
- Committed `casting.yaml` and `casting.yaml.lock` at repo root for Foundry/SigNoz deployment (hackathon requirement).

### Core ledger (foldos/core/)

- Implemented `Event`, `Scope`, `ConcurrencyError`, `PolicyViolation`, `ChainBroken` types.
- Built `MemoryStore` with append-only semantics, optimistic concurrency control (`expected_seq`), SHA-256 hash chaining on every append, and policy invariant enforcement via `validate_batch`.
- Implemented `fold()` reducer: deterministic state reconstruction from any prefix of the event stream. Handles `message`, `llm_call`, `tool_call`, `span_start`, `span_end`, `state_delta`, `policy_set`, and `agno_session` event kinds.
- Added `Session` class with high-level API: `add_message`, `add_llm_call`, `add_tool_call`, `set/unset/get`, `fork`, `span` context manager, `usage`, `state`, `trace`.
- Implemented `chain.py` with `hash_event` (SHA-256 over canonical JSON + previous hash) and `verify` (full chain walk).
- Built `policy.py` with invariant registration system, `budget_cap`, `enforce_budget`, and `backtest` (retroactive policy evaluation against real event history).
- Added `counterfactual.py`: fork at any event, replace one payload, replay suffix, return both states for comparison.
- Implemented `clock.py` for cross-stream time-to-position cuts.
- Added `usage.py` with per-model pricing registry.

### OpenTelemetry emission (foldos/otel/)

- Built `OtelProviders` dataclass wrapping `TracerProvider`, `MeterProvider`, and `LoggerProvider` with OTLP HTTP exporters to SigNoz.
- Implemented `Emitter` class: every ledger event produces an OTel span, a structured log record, and increments metrics. Maintains a bidirectional trace index (ledger position to trace/span ID and back).
- Emits counters (`foldos.llm.calls`, `foldos.llm.input_tokens`, `foldos.llm.output_tokens`, `foldos.llm.cost_usd`, `foldos.tool.calls`, `foldos.policy.violations`), histogram (`foldos.llm.latency_ms`), and observable gauges (`foldos.chain.head_seq`, `foldos.chain.head_hash`).
- Added `gen_ai.*` semantic convention attributes on LLM spans for SigNoz GenAI observability.
- Built `instrument_openai.py`: monkey-patches `openai.OpenAI` and `openai.AsyncOpenAI` chat completion clients to record `llm_call` events in the ledger (supports both streaming and non-streaming responses).
- Added `traced_tool.py` for OTel-instrumented Agno tool wrappers.
- All telemetry emission wrapped in `try/except: pass` to never break writes.

### AgentOS integration (foldos/app.py, foldos/db.py, foldos/bridge.py)

- Created `FoldosDb` extending Agno's `InMemoryDb`: every session upsert/delete is captured as an `agno_session` event in the ledger, with full rehydration on startup.
- Built `SyncBridge` for crossing the sync/async boundary (Agno's DB interface is synchronous, the ledger is async).
- Wired `AgentOS` with a single `foldos-agent` backed by `OpenAILike(id="llama3.2")` pointing at Ollama, pre/post hooks to set the current FoldOS session per agent run.
- Used `on_route_conflict="preserve_base_app"` to keep FoldOS control routes alongside AgentOS routes.

### Control plane (foldos/control/)

- Implemented FastAPI router with all FoldOS endpoints under `/foldos/*`:
  - `GET /foldos/state/{session}` -- reconstructed state with optional `as_of` parameter.
  - `GET /foldos/events/{session}` -- event list with head position.
  - `GET /foldos/events/{session}/stream` -- SSE stream with `Last-Event-ID` support.
  - `GET /foldos/usage/{session}` -- accumulated usage summary.
  - `GET /foldos/verify/{session}` -- hash-chain verification.
  - `GET /foldos/attestation/{session}` -- full attestation with head hash.
  - `GET /foldos/cut` -- cross-stream time cut.
  - `GET /foldos/trace-index` -- bidirectional ledger-to-trace mapping.
  - `POST /foldos/policy` -- append policy events (budget limits, etc.).
  - `POST /foldos/message` -- append message events.
  - `POST /foldos/backtest` -- retroactive policy evaluation (read-only).
  - `POST /foldos/branch` -- fork ledger at a sequence number.
  - `POST /foldos/counterfactual` -- fork + replace + compare.
  - `POST /foldos/replay` -- replay with a new prompt (if runner configured).
- Added `ScopeResolver` for resolving partial scope selectors (tenant/agent/session/thread) with ambiguity detection.
- Pydantic request/response models with strict validation and alias support.

### Console (foldos/console/)

- Built interactive web console served at `/console` with static HTML/JS.
- Console provides session exploration, event browsing, state inspection, budget setting, and backtest UI.

### SigNoz assets (foldos/provision/)

- Created governance dashboard JSON (`foldos/provision/dashboards/foldos.json`) covering LLM calls, token usage, cost, tool calls, policy violations, and chain head state.
- Created budget breach alert JSON (`foldos/provision/alerts/budget_breach.json`) firing when `foldos.policy.violations` exceeds 0 in a 5-minute window.
- Created policy violations alert JSON (`foldos/provision/alerts/policy-violations.json`).
- Added provisioning client (`foldos/provision/client.py`) for programmatic dashboard/alert import into SigNoz.

### Tests

- Core unit tests: store append/concurrency, fold determinism, chain verification, policy invariants, backtest, counterfactual, clock cuts.
- Contract tests: all control plane routes, request/response models, error handling, SSE streaming.
- Integration tests: OTel emitter span/log/metric emission, AgentOS agent run with Ollama producing ledger events.
- Property tests with Hypothesis for fold determinism.

### Documentation and scripts

- Wrote comprehensive README.md with product description, differentiators, architecture diagram, setup instructions, quickstart, demo beats, API reference, SigNoz usage summary, and required disclosures.
- Created `scripts/demo_scenario.py`: deterministic full-stack demo exercising agent run, policy, backtest, counterfactual, verification, and SigNoz deep links.
- Created `scripts/e2e_backend.py`: backend end-to-end smoke test.
- Created `scripts/e2e_agno_ollama.py`: Agno + Ollama compatibility gate.
