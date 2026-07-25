# FoldOS — Formal Model & Gap Analysis

*Written 2026-07-25 after pressure-testing [FoldOS-ARCHITECTURE.md](FoldOS-ARCHITECTURE.md) against the running spike. Supersedes §1 and §5 of that document.*

---

## 0. Summary

The architecture document claims the product is a join between Statefold events and SigNoz spans, keyed on `(stream, seq) ↔ (trace_id, span_id)`. **That join is partial in both directions, and the demo trace proves it.** Six gaps follow from it, four of which are correctness bugs rather than framing problems.

This document replaces the join with one that is total, defines the operations that were previously conflated under the word "replay," and corrects a governance design that the Statefold API cannot express.

---

## 1. The gap, empirically

Query against the demo trace in ClickHouse — which spans carry `statefold.seq`:

```
 name                                has_seq  seq
 demo.correlated_trace                     0    0
 Agent.run                    (Agno)       0    0
 OpenAILike.invoke            (Agno)       0    0
 statefold.llm.llama3.2       (ours)       1    1
 lookup_population            (Agno)       0    0
 statefold.tool.lookup_...    (ours)       1    2
 statefold.tool.lookup_...    (ours)       1    3
 OpenAILike.invoke            (Agno)       0    0
 statefold.llm.llama3.2       (ours)       1    5
```

**4 of 9 spans are addressable. 5 are not.** The unaddressable ones are exactly the spans a user is most likely to click — `Agent.run`, the model invocations, the tool span itself. Agno's spans come from `openinference`, which knows nothing about the event log, so they carry no `seq` and never will.

The forward direction is also partial. The index from the spike run:

```
seq=1 → span   seq=2 → span   seq=3 → span   seq=5 → span
seq=4 (span_end)     — no entry
seq=6 (agno_session) — no entry
```

`span_end` folds into the span opened at `span_start`; `agno_session` and `state_delta` produce no span at all. So *"every event maps to a span"* is false, and *"every span maps to an event"* is false. The product thesis rests on a bijection that does not exist.

---

## 2. Corrected model

### 2.1 Objects

- A **stream** `S` is identified by `Scope(tenant, agent, session, thread)`. Its events carry gap-free positions `seq ∈ {1, 2, …}` and timestamps `ts(S, n)`.
- A **run** is one agent/team invocation, identified by an OTel `trace_id`. A run contains spans, each with `[start, end)` in wall-clock time.
- Streams and runs are **independent decompositions of the same activity**: a stream is ordered by logical position, a run by wall-clock nesting. One session accumulates many runs; one run may touch many streams.

### 2.2 The join, restated

Do not join events to spans. **Join wall-clock time to log position.**

For a stream `S`, define

> `pos_S(t) = max { n : ts(S, n) ≤ t }`, and `0` if no such `n`.

`pos_S` is **total** (defined for every instant) and **monotone non-decreasing**. Then for *any* span `σ`, addressable or not:

> `state_at(σ, S) = get_state(S, as_of = pos_S(σ.start))`

This answers "what did the agent know when this span began?" for all 9 spans in the trace above, not 4. Both previously-broken directions become well-defined:

- **Span → state:** always available via `pos_S`. `statefold.seq`, when present, is an *exact anchor* — a faster and more precise path, but no longer the mechanism.
- **Event → run:** the seq→span index still answers this exactly where a span exists; where it doesn't, map `ts(S, n)` into the runs whose interval contains it.

The reframing is small in code and large in meaning: **`seq` is a logical clock, the trace is a physical clock, and FoldOS is the order-preserving embedding between them.** That is the actual invention.

### 2.3 Multi-stream runs — consistent cuts

A team run touches several streams. "Rewind to this span" is then ambiguous, and the architecture document never said which stream it means.

Generalize position to a **cut** — a vector, one position per stream:

> `cut(t) = { S ↦ pos_S(t) : S ∈ streams(run) }`

Because every `pos_S` is monotone and derived from the same physical clock, `cut(t)` is a **consistent cut** in the Lamport sense: it never contains an effect without its cause. Time-travel to a span means restoring that vector, and branching means forking every stream in it at its own position.

**Stream topology decision (required, currently unspecified).** Recommended: **one stream per `(agent, session)`**, so a team of three produces three streams under one session, plus one for the coordinator. This gives per-member cost attribution for free and makes `by_model` folds meaningful per agent. The cost is that every cross-member question needs a cut rather than a single `as_of`. The alternative — one shared stream — makes time-travel trivial and attribution impossible. Pick per-agent; the vector-clock machinery is ~30 lines.

---

## 3. Operation taxonomy — "replay" is three different things

The PRD, the architecture doc, and the demo all say *replay* while meaning different operations. Judges will ask. Name them separately:

| Operation | Determinism | Cost | Definition |
|---|---|---|---|
| **Fold-replay** | Total | Free | Recompute state from events up to `n`. Pure function of the log. Same input, same output, forever. |
| **Counterfactual** | Total | Free | Fork at `n`, substitute a *recorded* event's payload, re-fold. Answers "what if the tool had returned X?" without calling a model. |
| **Re-execution** | None | Tokens | Fork at `n`, run the agent forward with new input. New LLM calls, new results, new trace. |

**The demo currently performs re-execution and calls it replay.** That is the weakest of the three claims — it is just "running the agent again from a saved prefix," which any framework with checkpoints can do.

**Fold-replay and counterfactual are the defensible ones**, because they are only possible on an event-sourced log and are exactly what a snapshot-based system cannot do. Recommendation: lead the demo with **counterfactual** — fork, rewrite a recorded tool result, re-fold, and show the cost curve diverging *without spending a token*. Then show re-execution as the follow-on. Same effort, much stronger claim.

---

## 4. Governance: the budget cannot live where the doc puts it

Architecture doc §6.4 says "make budgets per-`(tenant, agent)` config." **This is not expressible.** From `memory.py:61-62`:

```python
if has_invariants():
    validate_batch(await self.get_state(scope), events)
```

and `invariants.py:58` — `validate_batch(state_before, events)`. The scope is **not passed**. An invariant receives `(state, event)` only; `Event` has no scope field. An invariant therefore cannot know which tenant it is validating, and `INVARIANTS` is a global module-level registry applied to every stream in the process.

**Correct design — make the budget an event.**

```python
await session.set(budget_usd=0.50)          # a state_delta event in the stream

@invariant("llm_call")
def budget_cap(state, event):
    budget = state.get("data", {}).get("budget_usd")
    if budget is None:
        return                               # ungoverned stream: allow
    if state.get("usage", {}).get("cost_usd", 0.0) > budget:
        raise InvariantViolation(...)
```

This is strictly better than config, and worth saying out loud in the demo:

- **Per-stream by construction** — the state passed in *is* that stream's state.
- **Time-travelable** — you can ask what the budget was at step 40.
- **Auditable** — a budget change is an event in the hash chain, so raising a limit is tamper-evident.
- **Replayable** — a counterfactual can lower the budget and re-fold to show what *would* have been blocked.

"The policy is inside the audit log it governs" is a genuinely strong line, and it falls out of a limitation rather than a feature.

---

## 5. Defects in the built exporter

Found by re-reading [spike-v4/foldos_otel.py](spike-v4/foldos_otel.py) against this model. All are real; none are hard.

| # | Defect | Consequence | Fix |
|---|---|---|---|
| 1 | `self._open` grows without bound | A span whose `span_end` never appends (crash, killed process) leaks memory and is never exported — silently missing from the trace | TTL sweep; on shutdown, end orphans with `StatusCode.ERROR` |
| 2 | `self._open` / `self.index` mutated from both the caller and the `SyncBridge` thread | Torn reads under concurrent runs. Not hit in the single-threaded demo; will be hit by a team | Guard with `threading.Lock` |
| 3 | Cost emitted once at append; the fold recomputes at read | Register a price later and Statefold back-fills history — SigNoz cannot. Permanent divergence between the metric and the log | Emit `cost_usd` only when a price is known; count `uncosted_calls` as its own metric; treat the log as authoritative |
| 4 | No inverse index | `/foldos/span/{trace_id}/{span_id}` in the architecture doc is unimplementable as specified | Maintain `(trace_id, span_id) → (stream, seq)`; fall back to `pos_S` (§2.2) |
| 5 | Retroactive spans use `now - latency_ms` | Bridge-thread queuing makes `now` later than the true end; spans drift a few ms and can appear to start before their parent | Acceptable; document it. Fix properly by recording `t0` in the event payload |
| 6 | `__getattr__` delegation silently forwards unknown attributes | A typo'd method hits the inner store instead of raising | Explicit passthrough list |

---

## 6. What this changes upstream

| Document | Section | Change |
|---|---|---|
| ARCHITECTURE §1 | Product thesis | Join is time→position, not event↔span |
| ARCHITECTURE §5.3 | Index | Now an optimization + an inverse index, not the mechanism |
| ARCHITECTURE §6.3 | `/foldos/*` | Add `GET /foldos/cut?at=<ts>`; redefine `/span/...` via `pos_S` |
| ARCHITECTURE §6.4 | Governance | Budget is an event in the stream, not config (§4) |
| ARCHITECTURE §9 | Phase 5 | Split into fold-replay / counterfactual / re-execution (§3) |
| PRD v5 §7 | Demo moment 3 | Lead with counterfactual, not re-execution |

Net additional work: **~2 hours** (vector cut, inverse index, three exporter fixes, budget-as-event). Phases 0-2 are unaffected — the exporter as built is still correct for the single-stream case.

---

## 7. The one decision I can't make for you

**Is FoldOS a debugging tool or an audit tool?** The evidence points both ways and the two want different demos:

- *Debugging:* traces, latency, "why was this slow," counterfactual — audience is the engineer.
- *Audit:* hash chain, budget veto, tamper detection, "prove nothing was edited" — audience is the person who has to sign off on an autonomous agent spending money.

The build serves both, so this is a **framing** choice, not an engineering one — but the 3-minute video cannot serve both. My recommendation is **audit**, for three reasons: it is the harder claim to fake, it makes the hash chain load-bearing rather than decorative, and "budget policy that lives inside its own audit log" (§4) is a sharper sentence than "we added tracing." Debugging is the more crowded field at an observability hackathon.

That said, "Impact" and "Creativity" are separate judging criteria, and the debugging frame may score higher on the former. Worth a decision before the video is scripted, not after.
