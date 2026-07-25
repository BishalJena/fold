"""Step 3: can AgentOS be the platform backend?

Checks whether AgentOS boots on a Statefold-backed db running through our OTel
store wrapper, exposes its REST surface, accepts our own custom routes, and can
turn on its built-in MCP server.
"""
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

provider = TracerProvider(resource=Resource.create({"service.name": "foldos-agentos"}))
provider.add_span_processor(SimpleSpanProcessor(
    OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")))
trace.set_tracer_provider(provider)
from openinference.instrumentation.agno import AgnoInstrumentor
AgnoInstrumentor().instrument(tracer_provider=provider)

from agno.agent import Agent
from agno.models.openai.like import OpenAILike
from agno.os import AgentOS
from statefold import InMemoryStore
from statefold.adapters.agno import AgentStateDb
from foldos_otel import OtelStateStore

store = OtelStateStore(InMemoryStore())
db = AgentStateDb(store, tenant="acme", agent="analyst")
model = OpenAILike(id="llama3.2", base_url="http://localhost:11434/v1", api_key="ollama")

agent = Agent(id="analyst", name="Analyst", model=model, db=db, telemetry=False)

agent_os = AgentOS(
    id="foldos",
    name="FoldOS",
    description="Event-sourced agent runtime with OpenTelemetry export to SigNoz",
    agents=[agent],
    mcp_server=True,          # AgentOS exposes its own MCP server
    telemetry=False,
)
app = agent_os.get_app()
print(f"AgentOS boots            : OK  ({type(app).__name__})")

# can we add our own routes to the same app?
from fastapi import APIRouter
r = APIRouter(prefix="/foldos", tags=["foldos"])


@r.get("/timetravel/{session_id}")
async def timetravel(session_id: str, as_of: int | None = None):
    from statefold.types import Scope
    return await store.get_state(Scope("acme", "analyst", session_id, "main"), as_of=as_of)


@r.get("/verify/{session_id}")
async def verify(session_id: str):
    from statefold import verify_chain
    from statefold.types import Scope
    return await verify_chain(store, Scope("acme", "analyst", session_id, "main"))


@r.get("/trace-index")
async def trace_index():
    return [{"stream": s, "seq": q, "trace_id": t, "span_id": p}
            for (s, q), (t, p) in store.index.items()]


app.include_router(r)
print("custom routes mount      : OK")

paths = sorted({getattr(x, "path", "") for x in app.routes})
mcp = [p for p in paths if "mcp" in p.lower()]
ours = [p for p in paths if p.startswith("/foldos")]
print(f"total routes exposed     : {len(paths)}")
print(f"  our custom routes      : {ours}")
print(f"  mcp routes             : {mcp}")
print("\nsample of AgentOS's built-in surface:")
for p in [x for x in paths if not x.startswith("/foldos")][:18]:
    print("   ", p)
