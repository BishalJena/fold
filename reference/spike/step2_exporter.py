"""Step 2: the full PRD demo path, end to end, against real SigNoz + real Ollama.

  1 correlated trace   — Agno spans + statefold spans in one tree
  2 budget veto        — invariant rejects the write, error span + counter
  3 time travel        — as_of + fork, replay tagged with foldos.replay_of
  4 tamper detection   — mutate an event, verify_chain reports broken_at
"""
import asyncio, threading, time

from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

RES = Resource.create({"service.name": "foldos-demo"})
provider = TracerProvider(resource=RES)
provider.add_span_processor(SimpleSpanProcessor(
    OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")))
trace.set_tracer_provider(provider)

reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint="http://localhost:4318/v1/metrics"),
    export_interval_millis=3000)
meter_provider = MeterProvider(resource=RES, metric_readers=[reader])
metrics.set_meter_provider(meter_provider)

from openinference.instrumentation.agno import AgnoInstrumentor
AgnoInstrumentor().instrument(tracer_provider=provider)
tracer = trace.get_tracer("foldos")

from statefold import InMemoryStore, verify_chain, invariant, InvariantViolation
from statefold.adapters.agno import AgentStateDb
from statefold.adapters.generic import SessionState
from statefold.telemetry import register_pricing
from statefold.instrument import instrument_openai
from agno.agent import Agent
from agno.models.openai.like import OpenAILike

from foldos_otel import OtelStateStore

# synthetic pricing so a free local model still produces real cost curves
register_pricing("llama3.2", input_per_mtok=3.00, output_per_mtok=15.00)

BUDGET_USD = 0.0015
store = OtelStateStore(InMemoryStore())
db = AgentStateDb(store, tenant="acme", agent="analyst")
session = SessionState(store, tenant="acme", agent="analyst", session="s1")

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()
run = lambda coro: asyncio.run_coroutine_threadsafe(coro, _loop).result()


@invariant("llm_call")
def budget_cap(state, event):
    spent = state.get("usage", {}).get("cost_usd", 0.0)
    if spent > BUDGET_USD:
        raise InvariantViolation(f"session cost ${spent:.4f} exceeds budget ${BUDGET_USD}")


def lookup_population(city: str) -> str:
    """Look up the population of a city."""
    async def work():
        async with session.span("tool.lookup_population", city=city):
            t0 = time.perf_counter()
            await asyncio.sleep(0.05)
            await session.add_tool_call("lookup_population", args={"city": city},
                                        result="8.9M",
                                        latency_ms=(time.perf_counter() - t0) * 1000)
    run(work())
    return "8.9 million"


model = OpenAILike(id="llama3.2", base_url="http://localhost:11434/v1", api_key="ollama")
instrument_openai(model.get_client(), session)
agent = Agent(model=model, db=db, tools=[lookup_population], telemetry=False,
              instructions="Use the tool once, then answer in one short sentence.")

# --- moment 1: one correlated trace -----------------------------------------
print("=" * 68)
with tracer.start_as_current_span("demo.correlated_trace") as root:
    TRACE1 = format(root.get_span_context().trace_id, "032x")
    print(f"1. correlated trace   : {TRACE1}")
    out = agent.run("What is the population of London?", session_id="s1")
    print(f"   agent said         : {str(out.content)[:60]!r}")

print(f"   usage              : {run(session.usage())}")

# --- moment 2: budget veto ---------------------------------------------------
with tracer.start_as_current_span("demo.budget_veto") as v:
    print(f"\n2. budget veto trace : {format(v.get_span_context().trace_id,'032x')}")
    try:
        run(session.add_llm_call("llama3.2", 900_000, 200_000, latency_ms=120.0))
        print("   VETO DID NOT FIRE  : unexpected")
    except InvariantViolation as e:
        print(f"   vetoed             : {e}")
        store.note_violation(session.scope, str(e))
    head_after = run(store.head(session.scope))
    print(f"   head unchanged     : seq={head_after} (nothing persisted)")

# --- moment 3: time travel + branch ------------------------------------------
async def timetravel():
    head = await store.head(session.scope)
    early = await store.get_state(session.scope, as_of=2)
    now = await store.get_state(session.scope)
    child = await store.fork(session.scope, at_seq=2, new_thread="what-if")
    return head, early.get("usage", {}).get("cost_usd"), now.get("usage", {}).get("cost_usd"), child

head, cost_at_2, cost_now, child = asyncio.run(timetravel()) if False else run(timetravel())
print(f"\n3. time travel       : head={head}  cost@seq2=${cost_at_2}  cost@head=${cost_now}")
print(f"   branched thread    : {child.flatten()}")

with tracer.start_as_current_span("demo.replay") as r:
    r.set_attribute("foldos.replay_of", TRACE1)
    r.set_attribute("foldos.replay_at_seq", 2)
    branch = SessionState(store, tenant="acme", agent="analyst", session="s1", thread="what-if")
    run(branch.add_llm_call("llama3.2", 100, 40, latency_ms=90.0))
    print(f"   replay trace       : {format(r.get_span_context().trace_id,'032x')} "
          f"(tagged foldos.replay_of={TRACE1[:16]}…)")

# --- moment 4: tamper detection ----------------------------------------------
async def tamper():
    before = await verify_chain(store, session.scope)
    inner = store._inner
    evs = [e async for e in store.read_events(session.scope)]
    victim = evs[2]
    victim.payload["result"] = "TAMPERED"          # edit history in place
    after = await verify_chain(store, session.scope)
    return before, after, victim.seq

before, after, seq = run(tamper())
print(f"\n4. chain before      : {before}")
print(f"   edited seq {seq} payload, re-verified:")
print(f"   chain after        : {after}")
with tracer.start_as_current_span("demo.integrity_check") as t:
    t.set_attribute("statefold.chain_ok", after["ok"])
    t.set_attribute("statefold.broken_at", after["broken_at"] or -1)

# --- side index ---------------------------------------------------------------
print(f"\nseq -> trace/span index ({len(store.index)} entries):")
for (stream, seq), (tid, sid) in list(store.index.items())[:8]:
    print(f"   {stream} seq={seq:<3} -> trace={tid[:16]}… span={sid}")

provider.force_flush()
meter_provider.force_flush()
print("\nflushed to SigNoz.")
print(f"TRACE1={TRACE1}")
