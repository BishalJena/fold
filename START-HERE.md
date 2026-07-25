# FoldOS — handoff package

Everything needed to build FoldOS from scratch, self-contained. Drop this folder into an empty repo and start.

---

## What this is

**FoldOS** is an agent platform whose state is an append-only ledger instead of an overwritten variable — so runs are rewindable, policies are backtestable against real history, and the record is tamper-evident. Every ledger entry is also an OpenTelemetry span and log record, so it's all visible in SigNoz.

Built for the Agents of SigNoz hackathon (WeMakeDevs × SigNoz). Agno is the rented runtime; SigNoz is the observability plane; the ledger core is ours.

---

## Read in this order

| # | File | Why |
|---|---|---|
| 0 | **[CLAUDE.md](CLAUDE.md)** | Environment, hard constraints, IP boundary, hackathon requirements. **Read before writing any code.** |
| 1 | [FoldOS-SPEC-v6.md](FoldOS-SPEC-v6.md) | Product, architecture, per-module spec, build phases with gates |
| 2 | [FoldOS-CONTRACTS.md](FoldOS-CONTRACTS.md) | **Normative** schemas. Wins any conflict with any other document |
| 3 | [FoldOS-FORMAL-MODEL.md](FoldOS-FORMAL-MODEL.md) | Semantics: the time↔position join, the three replay operations, why budgets are events |
| 4 | [FoldOS-USER-FLOWS.md](FoldOS-USER-FLOWS.md) | User journeys and the 3-minute demo script |

Then `reference/` (§ below), then build from **SPEC §10**.

---

## `reference/` — verified working code

**`reference/spike/`** — these ran successfully against real SigNoz and real Ollama. They are proof, not product; port the ideas, don't paste the files (the namespace is wrong — see CONTRACTS §7).

| File | Proves |
|---|---|
| `foldos_otel.py` | Event→span/metric emission. The `_emit` logic transfers almost unchanged into `foldos/otel/`. Note the six defects listed in FORMAL-MODEL §5 — fix them during the port |
| `step1_wiring.py` | Agno + OTel + ledger wiring, incl. the `OpenAILike`→Ollama path that makes LLM capture possible |
| `step2_exporter.py` | All four demo moments end-to-end: correlated trace, budget veto, time travel, tamper detection |
| `step3_agentos.py` | AgentOS boots on a custom store and accepts our own routes; 73 built-in paths + ours |

**`reference/deploy/`** — `casting.yaml` and `casting.yaml.lock`, the Foundry install files. **Both must be committed at your repo root** — a hard hackathon requirement, judges may re-run them. Set `mcp.spec.enabled: true` (~line 303 of the lock) before pouring; it ships disabled.

---

## Not included — fetch these yourself

Reference repos, omitted for size. Clone them and read rather than guessing at APIs:

```bash
git clone --depth 1 https://github.com/agno-agi/agno.git agno-src      # runtime; check AgentOS + models
git clone --depth 1 https://github.com/SigNoz/signoz.git signoz        # API routes, deep-link formats
```

**Deliberately excluded: Statefold.** FoldOS reimplements these capabilities independently and credits the design influence in the README. Reading that source while implementing would produce something structurally identical without intending to. CONTRACTS.md is complete enough to build from on its own — that is why it was written that way.

---

## First three commands

```bash
# 1. Confirm the observability plane is up
curl -s localhost:8080/api/v1/version          # expect {"version":"v0.133.0",...}

# 2. Confirm the model is up
ollama list                                    # expect llama3.2

# 3. Install
pip install -e ".[dev]"                        # agno[os]==2.8.2, openai, fastmcp, otel sdk+otlp
```

If step 1 fails, SigNoz isn't running — pour it from `reference/deploy/` first. Everything downstream assumes OTLP at `localhost:4318`.

---

## The one thing that must not be skipped

Before leaving Phase 1, this property test must pass for every `n`:

```python
fold(events[:n]) == get_state(scope, as_of=n)
```

It's the only check that catches fold/chain divergence early. A bug in the ledger core doesn't announce itself — it surfaces three phases later as an inexplicable cost number.
