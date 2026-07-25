# FoldOS — build context

Read this before writing any code. New to the project? Start with [START-HERE.md](START-HERE.md).

---

## What we're building

**FoldOS** is an agent platform whose state is an append-only ledger instead of an overwritten variable. That makes agent runs rewindable, policy backtestable, and history tamper-evident. Every ledger entry is also an OpenTelemetry span *and* a log record, so the whole thing is observable in SigNoz.

**Agno is rented** (runtime + AgentOS control plane). **SigNoz is the observability plane** — a hard requirement, it's the hackathon sponsor. **The ledger core, governance, and the time↔position join are ours.**

---

## Documents

| # | File | Role |
|---|---|---|
| 1 | [FoldOS-SPEC-v6.md](FoldOS-SPEC-v6.md) | Product, architecture, per-module spec, build phases + gates |
| 2 | [FoldOS-CONTRACTS.md](FoldOS-CONTRACTS.md) | **Normative** schemas. Wins any conflict, including with this file |
| 3 | [FoldOS-FORMAL-MODEL.md](FoldOS-FORMAL-MODEL.md) | Semantics: `pos`/`cut`, the three replay operations, budgets-as-events |
| 4 | [FoldOS-USER-FLOWS.md](FoldOS-USER-FLOWS.md) | Journeys + the 3-minute demo script |
| — | [reference/spike/](reference/spike/) | Verified working code. Port the ideas; the namespace is wrong (CONTRACTS §7) |

---

## Verified environment (checked 2026-07-25 — re-verify before trusting)

| Thing | State |
|---|---|
| SigNoz | Running via Foundry compose. UI `localhost:8080`, OTLP `localhost:4318` (HTTP) / `4317` (gRPC). v0.133.0 |
| SigNoz auth | API needs the `SIGNOZ-API-KEY` header; bare requests return 401 |
| SigNoz MCP | Ships **disabled** — `mcp.spec.enabled: false` in `reference/deploy/casting.yaml.lock` (~line 303). Flip it and re-pour in Phase 0, or use Foundry's ready-made [compose-mcp example](https://github.com/SigNoz/foundry/tree/main/docs/examples/docker/compose-mcp) |
| SigNoz agent skills | Official Claude Code plugin: [SigNoz/agent-skills](https://github.com/SigNoz/agent-skills) — queries, dashboards, alerts, docs, MCP setup. **Install it**; it removes the guesswork from dashboard/alert JSON |
| SigNoz LLM Observability | A built-in GenAI page keyed on `gen_ai.request.model`. Emit full `gen_ai.*` semconv and it populates for free |
| Ollama | Model **`llama3.2`** (not 3.1) |
| Agno | **2.8.2** — pin it |
| SigNoz deep link | `http://localhost:8080/trace/{trace_id}?spanId={span_id}` — deep-links *and* auto-expands to the span |

---

## Hard constraints — each cost a failed run to discover

1. **Own the `TracerProvider`, then call `AgnoInstrumentor().instrument(tracer_provider=provider)` explicitly.** Never call Agno's `setup_tracing()` — it early-returns when a provider already exists (`agno/tracing/setup.py:71-75`), *before* the line that instruments, silently giving you **zero Agno spans**.
2. **Use `OpenAILike(id="llama3.2", base_url="http://localhost:11434/v1", api_key="ollama")`, never `agno.models.ollama`.** The Ollama model class imports the `ollama` SDK, which can't be wrapped for LLM capture. `OpenAILike.get_client()` returns a real `openai.OpenAI`, which can.
3. **Capture ambient OTel context on the caller's thread before any `await`.** Context does survive a thread hop through `asyncio.run_coroutine_threadsafe` (`call_soon_threadsafe` → `Handle.__init__` → `copy_context()`), but only if captured before yielding.
4. **Hold spans open** — start a real non-current span at `span_start`, `.end(end_time=…)` at `span_end`. Children finish before parents; retroactive emission breaks parenting.
5. **Never mutate an event.** Chain verification must stay green — it's the standing regression test.
6. **Wrap all telemetry emission in `try/except: pass`.** It must never break a write.
7. **Undeclared dependencies:** `agno` does not pull `openai`; `AgentOS` needs `agno[os]`; `mcp_server=True` needs `fastmcp`.
8. **A vetoed write leaves `head` unchanged.** Invariants run pre-persist, all-or-nothing. That property *is* the demo.

---

## IP boundary

FoldOS reimplements event-sourced agent state independently. The README credits the design influence of [Statefold](https://github.com/ioteverythin/statefold) (Apache-2.0) in one plain sentence; **no Statefold code is used, and its source is deliberately not in this package.** Do not fetch it to "check" an implementation — CONTRACTS.md is complete on its own.

Licences: Agno Apache-2.0 · SigNoz MIT **outside** `ee/` and `cmd/enterprise/`, which are proprietary — never copy from those.

---

## Build order

Phases and gates are in SPEC §10. **Phase 1 (`foldos/core/`) is the critical path.** Before leaving it, this must pass for every `n`:

```python
fold(events[:n]) == get_state(scope, as_of=n)
```

Phases 1-4 are a complete submission. 5-6 are differentiators. 7-8 are non-negotiable delivery.

---

## Hackathon requirements (failing these loses everything)

- **AI disclosure is mandatory — non-disclosure is disqualification.** "Built with Claude Code" in the README *and* the submission form. Do not bury it.
- `casting.yaml` **and** `casting.yaml.lock` committed at repo root (copy from `reference/deploy/`). Judges may re-run them.
- Demo video **under 3 minutes**, on YouTube.
- A **new** blog post (Medium / Dev.to / Substack — not LinkedIn), not reused from the pre-hackathon challenge.
- Judging rewards **breadth of SigNoz usage**: traces, logs, metrics, dashboards, alerts, Query Builder, MCP. We use all seven by design (SPEC §7).
- Maintain `CHANGELOG.md` in plain language after every meaningful step — the blog post is generated from it.

---

## Conventions

Telemetry namespace is `foldos.*` — **never `statefold.*`**, which appears in the reference spike code and must not survive into the product. Async by default; sync wrappers only where Agno's interface demands them. Everything else is CONTRACTS §7.
