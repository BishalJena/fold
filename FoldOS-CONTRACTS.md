# FoldOS — Data & Interface Contracts

*Normative. Where this document and any other disagree, **this one wins**. Every name here is exact — do not paraphrase keys, rename attributes, or "improve" a schema mid-build. Cross-session consistency depends on it.*

---

## 1. Event kinds → payload schemas

Every event has the envelope from SPEC §6.1: `kind, payload, actor, causation_id, id, ts, seq, hash`.

| `kind` | `actor` | `payload` keys | Emitted by |
|---|---|---|---|
| `message` | role (`user`/`assistant`/`system`) | `role: str`, `content: str`, `**meta` | `session.add_message` |
| `tool_call` | `tool:{name}` | `name: str`, `args: dict`, `result: Any`, `latency_ms: float\|None`, `error: str\|None` | `@traced_tool`, `session.add_tool_call` |
| `llm_call` | `llm:{model}` | `model: str`, `input_tokens: int`, `output_tokens: int`, `latency_ms: float\|None`, `cost_usd: float\|None`, `**meta` | `instrument_openai`, `session.add_llm_call` |
| `span_start` | `trace` | `span_id: str`, `parent_id: str\|None`, `name: str`, `attrs: dict` | `session.span()` enter |
| `span_end` | `trace` | `span_id: str`, `status: "ok"\|"error"`, `error: str\|None`, `duration_ms: float` | `session.span()` exit |
| `state_delta` | `agent` | `set: dict` and/or `unset: list[str]` | `session.set/unset` |
| `policy_set` | `policy` | `key: str`, `value: Any`, `reason: str\|None` | `POST /foldos/policy` |
| `agno_session` | `agno` | `session: dict\|None`, `type: "agent"\|"team"\|"workflow"`, `deleted: bool?` | `FoldosDb.upsert_session` |

**Rules.**
- `causation_id` carries the enclosing `span_id`, set automatically from the contextvar in `_append`. Never set it by hand.
- `cost_usd` is normally `None` at write time; cost is resolved **at fold time** from the pricing table (§3). Pass it explicitly only when the provider returned a real price.
- Unknown `kind` values must fold to unchanged state, never raise. Forward compatibility.

---

## 2. Folded state shape

`get_state(scope, as_of=None)` returns exactly this shape. Missing sections are present-and-empty, never absent.

```python
{
  "messages": [ {"role": str, "content": str, "seq": int}, ... ],
  "tools":    [ {"name": str, "args": dict, "result": Any,
                 "latency_ms": float|None, "error": str|None, "seq": int}, ... ],
  "spans":    { span_id: {"name": str, "parent_id": str|None, "attrs": dict,
                          "status": "ok"|"error"|"open", "duration_ms": float|None} },
  "data":     { ... },              # working memory + policy values (e.g. "budget_usd")
  "usage":    {
      "llm_calls": int, "input_tokens": int, "output_tokens": int,
      "cost_usd": float, "uncosted_calls": int,
      "by_model": { model: {"calls": int, "input_tokens": int, "output_tokens": int,
                            "cost_usd": float, "latency_ms_total": float} },
      "tools":    { name:  {"calls": int, "errors": int, "latency_ms_total": float} }
  }
}
```

**Invariants the property test must assert:**
- `fold(events[:n]) == get_state(scope, as_of=n)` for every `n`. Non-negotiable — this is what catches fold/chain divergence.
- `usage.cost_usd` never silently counts an unpriced call; those increment `uncosted_calls`.
- Folding is pure: no I/O, no clock reads, no randomness.

---

## 3. Pricing

`register_pricing(model: str, input_per_mtok: float, output_per_mtok: float)`.
`cost = (input_tokens * in + output_tokens * out) / 1_000_000`.
Resolution order: explicit `payload["cost_usd"]` → pricing table at fold time → `None` (increments `uncosted_calls`).

Demo value: `register_pricing("llama3.2", 3.00, 15.00)` — synthetic, so a free local model still produces real cost curves. **Say it's synthetic on camera.**

---

## 4. OpenTelemetry contract

### 4.1 Span names

| Source event | Span name |
|---|---|
| `span_start`/`span_end` | `foldos.{payload.name}` |
| `tool_call` | `foldos.tool.{name}` |
| `llm_call` | `foldos.llm.{model}` |
| policy violation | `foldos.policy.violation` |

### 4.2 Span attributes (every FoldOS span)

```
foldos.tenant, foldos.agent, foldos.session, foldos.thread   (string)
foldos.stream        string   "tenant/agent/session/thread"
foldos.seq           int      the logical clock — THE JOIN KEY
foldos.event_kind    string
foldos.actor         string
```

Additional, per kind:

| Kind | Attributes |
|---|---|
| span | `foldos.span_id`, `foldos.duration_ms`, `foldos.attr.{k}` (stringified) |
| tool | `tool.name`, `tool.latency_ms`, `foldos.error` on failure |
| llm | `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cost_usd`, `foldos.uncosted` (bool) |
| violation | `foldos.policy.key`, `foldos.policy.limit`, `foldos.policy.observed`, `foldos.reason` |

`gen_ai.*` follows OTel semantic conventions — keep those exact so SigNoz's built-in views work.

Errors set `Status(StatusCode.ERROR, message)`.

### 4.3 Metrics

| Name | Type | Unit | Attributes |
|---|---|---|---|
| `foldos.llm.calls` | Counter | `1` | tenant, agent, session, model |
| `foldos.llm.input_tokens` | Counter | `{token}` | same |
| `foldos.llm.output_tokens` | Counter | `{token}` | same |
| `foldos.llm.cost_usd` | Counter | `USD` | same |
| `foldos.llm.latency_ms` | Histogram | `ms` | same |
| `foldos.tool.calls` | Counter | `1` | tenant, agent, session, tool, error |
| `foldos.policy.violations` | Counter | `1` | tenant, agent, session, policy |
| `foldos.chain.head_seq` | Gauge | `1` | tenant, agent, session |
| `foldos.chain.head_hash` | Gauge (value `1`) | `1` | + `hash` attribute — **the external tamper anchor** |

`foldos.chain.head_hash` carries the hash in an *attribute* with a constant value of `1`, because metric values are numeric. Once ingested, ClickHouse holds a record the store cannot retroactively alter.

### 4.4 Log records — every event, always

Emit one structured log per event, in the ambient span context so `trace_id`/`span_id` attach automatically.

```
body:      "{kind} seq={seq} {summary}"
severity:  ERROR if event carries an error or is a violation, else INFO
attributes: all §4.2 base attributes + "foldos.payload" (JSON string, truncated 4000 chars)
```

This is what makes SigNoz's Logs tab the event-log browser. Do not skip it — it replaces UI we are deliberately not building.

---

## 5. Control API shapes

`GET /foldos/cut?at=<iso8601>&session=<id>`
→ `{"at": str, "cut": {"tenant/agent/session/thread": int, ...}}`

`GET /foldos/state/{session}?as_of=<int>` → §2 state object.

`GET /foldos/verify/{session}` → `{"ok": bool, "broken_at": int|null, "events": int}`

`GET /foldos/attestation/{session}`
→ `{"stream": str, "events": int, "head_seq": int, "head_hash": str, "verified_at": iso8601, "ok": bool}`

`POST /foldos/policy` body `{"session": str, "key": "budget_usd", "value": 0.50, "reason": str?}`
→ `{"seq": int, "event_id": str}`

`POST /foldos/backtest` body `{"session": str, "policy": {"key": str, "value": Any}}`
→ `{"evaluated": int, "violations": [{"seq": int, "ts": str, "observed": Any, "limit": Any}]}`
**Read-only. Must not append.**

`POST /foldos/branch` body `{"session": str, "at_seq": int, "thread": str}`
→ `{"scope": str, "from_seq": int}`

`GET /foldos/trace-index?session=<id>`
→ `[{"stream": str, "seq": int, "trace_id": str, "span_id": str}]`

`GET /foldos/trace-index?trace_id=<id>&span_id=<id>`
→ `{"stream": str|null, "seq": int|null, "exact": bool, "at": iso8601|null, "cut": {str: int}|null}`

`GET /foldos/events/{session}?after=<int>`
→ `{"stream": str, "head": int, "events": [Event, ...]}`

`GET /foldos/events/{session}/stream?after=<int>` uses Server-Sent Events. Each persisted event is sent as:

```text
id: <event.seq>
event: foldos.event
data: <exact Event JSON>
```

`Last-Event-ID` and `after` resume strictly after that sequence without duplicates.

`GET /foldos/usage/{session}?as_of=<int>` → §2 `usage` object.

`POST /foldos/counterfactual` body `{"session": str, "event_seq": int, "thread": str, "payload_overrides": dict}`
→ `{"original_scope": str, "branch_scope": str, "replaced_seq": int, "original_state": §2, "branch_state": §2}`

Counterfactual creates a persistent branch, replaces payload fields only, copies the unchanged recorded suffix, and re-folds without invoking a model. Kind, actor, causation, and timestamp are preserved. It does not predict subsequent model behavior.

`POST /foldos/replay` body `{"session": str, "thread": str, "prompt": str}`
→ `{"scope": str, "agno_session_id": str, "run_id": str, "trace_id": str, "replay_of": str, "replay_at_seq": int}`

Replay means non-deterministic, token-consuming agent re-execution from a branch. The main thread uses the original Agno session id; a branch uses `f"{session}--{thread}"` and never overwrites the original session.

All session routes and request bodies accept optional `tenant`, `agent`, and `thread` selectors. Defaults are `acme`, `analyst`, and `main`. If no default exists and several scopes match, return 409 with the available stream keys.

---

## 6. Errors

| Exception | HTTP | Meaning |
|---|---|---|
| `ConcurrencyError(expected, actual)` | 409 | `head != expected_seq`; caller retries (8× internally) |
| `PolicyViolation(reason)` | 422 | Write vetoed pre-persist. **`head` unchanged** |
| `ChainBroken(seq)` | 500 | Verification failed |

HTTP errors use `{"error": str, "message": str, "details": dict}`. `details` carries structured fields such as concurrency positions, `broken_at`, or available stream keys.

---

## 7. Naming conventions

- Telemetry namespace: `foldos.*`. Never `statefold.*` — that namespace belongs to a project we no longer depend on, and it appears in the v5 spike code.
- Python: `snake_case`; async by default; sync wrappers only where Agno's interface demands them (`FoldosDb`).
- Stream key format: `f"{tenant}/{agent}/{session}/{thread}"` — exact, it's used as a dict key and a span attribute.
- Timestamps: ISO 8601 UTC with offset (`datetime.now(timezone.utc).isoformat()`).
- Event ids: sortable — `f"{int(time.time()*1000):013d}-{os.urandom(8).hex()}"`.
