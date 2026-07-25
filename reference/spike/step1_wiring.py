"""Step 1: does the wiring hold at all?

Checks, in order:
  A. AgentStateDb constructs against agno 2.8.2's InMemoryDb (API drift)
  B. AgnoInstrumentor with OUR TracerProvider -> Agno spans reach SigNoz
  C. statefold session.span() nests under the Agno tool span (one trace)
  D. instrument_openai reaches the client OpenAILike builds for Ollama
"""
import asyncio, os, threading

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

OTLP = "http://localhost:4318/v1/traces"

# --- our provider FIRST, then instrument Agno onto it explicitly -------------
provider = TracerProvider(resource=Resource.create({"service.name": "foldos-spike"}))
provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=OTLP)))
trace.set_tracer_provider(provider)

from openinference.instrumentation.agno import AgnoInstrumentor
AgnoInstrumentor().instrument(tracer_provider=provider)

tracer = trace.get_tracer("foldos")

from statefold import InMemoryStore
from statefold.adapters.agno import AgentStateDb
from statefold.adapters.generic import SessionState

store = InMemoryStore()

# --- A: does AgentStateDb construct against this agno version? ---------------
try:
    db = AgentStateDb(store, tenant="acme", agent="analyst")
    print("A. AgentStateDb constructs           : OK")
except Exception as e:
    print(f"A. AgentStateDb constructs           : FAIL {type(e).__name__}: {e}")
    raise SystemExit(1)

# --- D: can we instrument the client OpenAILike builds for Ollama? -----------
from agno.models.openai.like import OpenAILike

model = OpenAILike(id="llama3.2", base_url="http://localhost:11434/v1", api_key="ollama")
session = SessionState(store, tenant="acme", agent="analyst", session="s1")

from statefold.instrument import instrument_openai
try:
    client = model.get_client()
    print(f"D. OpenAILike client type            : {type(client).__module__}.{type(client).__name__}")
    instrument_openai(client, session)
    print("D. instrument_openai wraps it        : OK")
except Exception as e:
    print(f"D. instrument_openai                 : FAIL {type(e).__name__}: {e}")

# --- B + C: run an agent with a tool that opens a statefold span -------------
from agno.agent import Agent

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()


def lookup_population(city: str) -> str:
    """Look up the population of a city."""
    span = trace.get_current_span().get_span_context()
    print(f"C.   inside tool, ambient otel span  : trace={format(span.trace_id,'032x')[:16]}… "
          f"span={format(span.span_id,'016x')} recording={trace.get_current_span().is_recording()}")

    async def work():
        async with session.span("statefold.lookup", city=city) as sid:
            await session.add_tool_call("lookup_population", args={"city": city},
                                        result="8.9M", latency_ms=3.0)
            return sid
    sid = asyncio.run_coroutine_threadsafe(work(), _loop).result()
    print(f"C.   statefold span id               : {sid}")
    return "8.9 million"


agent = Agent(model=model, db=db, tools=[lookup_population], telemetry=False,
              instructions="Use the tool. Answer in one short sentence.")

with tracer.start_as_current_span("demo.root") as root:
    root_ctx = root.get_span_context()
    print(f"B. root trace_id                     : {format(root_ctx.trace_id,'032x')}")
    out = agent.run("What is the population of London?", session_id="s1")
    print(f"B. agent replied                     : {str(out.content)[:70]!r}")

provider.force_flush()

# --- what landed in the statefold log ---------------------------------------
async def dump():
    evs = [e async for e in store.read_events(session.scope)]
    print(f"\nstatefold events on {session.scope.flatten()}: {len(evs)}")
    for e in evs:
        name = e.payload.get("name") or e.payload.get("model") or e.payload.get("span_id", "")
        print(f"  seq={e.seq:<3} {e.kind:<12} causation={str(e.causation_id)[:26]:<26} {name}")
    print("usage:", await session.usage())
    from statefold import verify_chain
    print("chain:", await verify_chain(store, session.scope))

asyncio.run(dump())
