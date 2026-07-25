"""Deterministic demo scenario for FoldOS.

Exercises the full stack through the HTTP API:
  - AgentOS agent run (POST /agents/analyst/runs)
  - Ledger events and state
  - Budget policy (POST /foldos/policy)
  - Backtest (POST /foldos/backtest)
  - Counterfactual (POST /foldos/counterfactual)
  - Chain verification (GET /foldos/verify/{session})
  - Trace index and SigNoz deep links

Run from the repo root:

    .venv/bin/python scripts/demo_scenario.py

Requirements:
    - Ollama running with llama3.2.
    - SigNoz is checked but not required (OTLP export may fail silently).
    - Does NOT require a SIGNOZ_API_KEY.

Note: SigNoz local setup may need SIGNOZ_API_KEY for dashboard/alert
provisioning via the provisioning client, but this demo script only
reads the health endpoint and does not provision anything.

Exit codes:
    0 -- all assertions passed
    1 -- one or more assertions failed
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, cast

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
SIGNOZ_URL = os.environ.get("SIGNOZ_URL", "http://localhost:8080")
FOLDOS_HOST = os.environ.get("FOLDOS_HOST", "127.0.0.1")
FOLDOS_PORT = int(os.environ.get("FOLDOS_PORT", "7777"))
BASE_URL = f"http://{FOLDOS_HOST}:{FOLDOS_PORT}"

# Unique session ID per run to avoid collisions.
SESSION = f"demo-{uuid.uuid4().hex[:8]}"

passed: list[str] = []
failed: list[str] = []
first_trace_id: str | None = None
first_span_id: str | None = None
policy_data: dict[str, Any] = {}


def _load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _step(name: str) -> None:
    print(f"\n{'='*60}")
    print(f"  STEP: {name}")
    print(f"{'='*60}")


def _assert(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        passed.append(label)
        print(f"  PASS: {label}")
    else:
        failed.append(label)
        msg = f"  FAIL: {label}"
        if detail:
            msg += f" -- {detail}"
        print(msg)


def _wait_for_url(url: str, timeout: float = 30.0, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=2.0)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# 0. Check SigNoz health (informational, not blocking)
# ---------------------------------------------------------------------------

def check_signoz() -> bool:
    _step("Check SigNoz health")
    url = f"{SIGNOZ_URL.rstrip('/')}/api/v1/health"
    print(f"  Checking {url} ...")
    try:
        resp = httpx.get(url, timeout=5.0)
        if resp.status_code == 200:
            print(f"  SigNoz is healthy: {resp.text[:200]}")
            return True
        print(f"  SigNoz returned status {resp.status_code}")
    except Exception as exc:
        print(f"  SigNoz not reachable: {exc}")
    print("  (SigNoz is optional for this demo; OTLP export may fail silently)")
    return False


# ---------------------------------------------------------------------------
# 1. Start the FoldOS service
# ---------------------------------------------------------------------------

def start_service() -> subprocess.Popen[str]:
    _step("Start FoldOS service")
    print(f"  Starting on {BASE_URL} ...")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "foldos.app:create_app",
            "--host",
            FOLDOS_HOST,
            "--port",
            str(FOLDOS_PORT),
            "--factory",
        ],
        cwd=str(REPO_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    probe = f"{BASE_URL}/foldos/cut?session=__probe__"
    if not _wait_for_url(probe, timeout=40.0):
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise RuntimeError("FoldOS service did not start within 40 seconds")
    print("  FoldOS service is ready")
    return proc


def stop_service(proc: subprocess.Popen[str]) -> None:
    _step("Stop FoldOS service")
    proc.terminate()
    try:
        proc.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    print("  Service stopped")


# ---------------------------------------------------------------------------
# Scenario steps
# ---------------------------------------------------------------------------

def step_a_agent_run(client: httpx.Client) -> dict[str, Any]:
    """Run the Agno agent and verify llm_call events appear in the ledger."""
    global first_trace_id, first_span_id

    _step("A: Agent run via POST /agents/analyst/runs")

    # Send a simple prompt (no tool calls -- the agent has no tools mounted).
    run_resp = client.post(
        "/agents/analyst/runs",
        data={
            "message": "Say exactly 'hello from FoldOS' and nothing else.",
            "session_id": SESSION,
            "stream": "false",
        },
        timeout=60.0,
    )
    print(f"  POST /agents/analyst/runs -> {run_resp.status_code}")
    _assert(run_resp.status_code == 200, "agent_run_200", run_resp.text[:200])

    # Give the instrumented client a moment to flush ledger writes.
    time.sleep(1.0)

    # Verify events were recorded.
    events_resp = client.get(f"/foldos/events/{SESSION}")
    _assert(events_resp.status_code == 200, "events_after_run_200")

    events_data: dict[str, Any] = events_resp.json()
    events: list[dict[str, Any]] = events_data.get("events", [])
    print(f"  Ledger has {len(events)} event(s) after agent run")

    # We expect at least one agno_session event and at least one llm_call.
    llm_calls = [e for e in events if e.get("kind") == "llm_call"]
    _assert(len(llm_calls) >= 1, "llm_call_event_present",
            f"got {len(llm_calls)} llm_call(s), kinds={[e['kind'] for e in events]}")

    # Grab the trace index for the first event.
    trace_resp = client.get(f"/foldos/trace-index?session={SESSION}")
    if trace_resp.status_code == 200:
        trace_entries: list[dict[str, Any]] = trace_resp.json()
        if trace_entries:
            first_trace_id = trace_entries[0].get("trace_id")
            first_span_id = trace_entries[0].get("span_id")
            print(f"  First trace: trace_id={first_trace_id}, span_id={first_span_id}")

    return events_data


def step_b_set_policy(client: httpx.Client) -> dict[str, Any]:
    """Set a tight budget policy via POST /foldos/policy."""
    global policy_data
    _step("B: Set budget policy via POST /foldos/policy")

    # Set a budget 20% above the first run's actual cost. The first run already
    # stays under it; the second run adds more tokens and pushes cumulative
    # spend over the cap, demonstrating a live policy veto.
    first_run_usage = client.get(f"/foldos/usage/{SESSION}?thread=main").json()
    current_cost = float(first_run_usage.get("cost_usd", 0.0))
    budget = max(0.000001, current_cost * 1.2)
    policy_resp = client.post(
        "/foldos/policy",
        json={
            "session": SESSION,
            "key": "budget_usd",
            "value": budget,
            "reason": "demo budget cap",
        },
    )
    print(f"  POST /foldos/policy -> {policy_resp.status_code}")
    _assert(policy_resp.status_code == 200, "policy_set_200", policy_resp.text[:200])

    policy_data = policy_resp.json()
    _assert("seq" in policy_data, "policy_has_seq")
    _assert("event_id" in policy_data, "policy_has_event_id")
    print(f"  Policy event at seq={policy_data.get('seq')}")

    # Verify the budget is in the state.
    state_resp = client.get(f"/foldos/state/{SESSION}")
    _assert(state_resp.status_code == 200, "state_after_policy_200")
    state: dict[str, Any] = state_resp.json()
    state_budget = state.get("data", {}).get("budget_usd")
    _assert(state_budget == budget, "budget_in_state", f"budget_usd={state_budget}")
    print(f"  State data.budget_usd = {state_budget}")

    return policy_data


def step_b2_policy_veto(client: httpx.Client) -> dict[str, Any]:
    """Run the agent again; the new llm_call must exceed the budget and be vetoed.

    AgentOS reports the model-call failure as a 200 with status ERROR because
    the policy violation is raised from inside the run; the key FoldOS check
    is that no new llm_call event is appended to the ledger.
    """
    _step("B2: Live policy veto on second agent run")

    before_events = client.get(f"/foldos/events/{SESSION}").json().get("events", [])

    run_resp = client.post(
        "/agents/analyst/runs",
        data={
            "message": "Say exactly 'second run' and nothing else.",
            "session_id": SESSION,
            "stream": "false",
        },
        timeout=60.0,
    )
    print(f"  POST /agents/analyst/runs (second) -> {run_resp.status_code}")
    _assert(run_resp.status_code == 200, "policy_veto_agent_run_200", run_resp.text[:200])
    body = cast(dict[str, Any], run_resp.json())
    _assert(body.get("status") == "ERROR", "policy_veto_status_error",
            f"status={body.get('status')}")
    # The agent surfaced the budget violation in its response content.
    _assert("budget_usd limit" in str(body.get("content", "")), "policy_veto_in_content",
            f"content={body.get('content')}")

    events_after = client.get(f"/foldos/events/{SESSION}").json().get("events", [])
    llm_calls_before = sum(1 for e in before_events if e.get("kind") == "llm_call")
    llm_calls_after = sum(1 for e in events_after if e.get("kind") == "llm_call")
    _assert(llm_calls_after == llm_calls_before, "no_new_llm_call_after_veto",
            f"llm_calls_after={llm_calls_after}, expected={llm_calls_before}")
    usage_after = client.get(f"/foldos/usage/{SESSION}?thread=main").json()
    before_llm_calls = sum(1 for e in before_events if e.get("kind") == "llm_call")
    _assert(usage_after.get("llm_calls") == before_llm_calls, "no_second_llm_call",
            f"llm_calls={usage_after.get('llm_calls')}, expected={before_llm_calls}")
    print("  Veto confirmed; no new llm_call in ledger, cost stayed at "
          f"{usage_after.get('cost_usd')}")

    return body


def step_c_verify_chain(client: httpx.Client) -> dict[str, Any]:
    """Verify chain integrity via GET /foldos/verify/{session}."""
    _step("C: Verify chain integrity")

    verify_resp = client.get(f"/foldos/verify/{SESSION}")
    print(f"  GET /foldos/verify/{SESSION} -> {verify_resp.status_code}")
    _assert(verify_resp.status_code == 200, "verify_200")

    verify_data: dict[str, Any] = verify_resp.json()
    _assert(verify_data.get("ok") is True, "chain_ok",
            f"ok={verify_data.get('ok')}, broken_at={verify_data.get('broken_at')}")
    _assert(verify_data.get("events", 0) >= 2, "chain_has_events",
            f"events={verify_data.get('events')}")
    print(f"  Chain OK, {verify_data.get('events')} event(s)")

    return verify_data


def step_d_backtest(client: httpx.Client) -> dict[str, Any]:
    """Backtest with a very low budget via POST /foldos/backtest.

    The app registers illustrative pricing for llama3.2 so that local runs
    carry a nominal cost. A backtest budget below the already-spent cost
    therefore returns violations, proving the policy would have blocked the
    run had the limit been in place earlier.
    """
    _step("D: Backtest with budget_usd=0.00000001")

    backtest_resp = client.post(
        "/foldos/backtest",
        json={
            "session": SESSION,
            "policy": {"key": "budget_usd", "value": 0.00000001},
        },
    )
    print(f"  POST /foldos/backtest -> {backtest_resp.status_code}")
    _assert(backtest_resp.status_code == 200, "backtest_200", backtest_resp.text[:200])

    backtest_data: dict[str, Any] = backtest_resp.json()
    evaluated = backtest_data.get("evaluated", 0)
    violations = backtest_data.get("violations", [])
    _assert(evaluated >= 2, "backtest_evaluated_events",
            f"evaluated={evaluated}")
    _assert(isinstance(violations, list), "backtest_violations_is_list")
    print(f"  Backtest evaluated {evaluated} event(s), {len(violations)} violation(s)")
    _assert(len(violations) >= 1, "backtest_has_violations",
            f"expected at least 1 violation, got {len(violations)}")
    if violations:
        print(f"  First violation at seq={violations[0].get('seq')}, "
              f"observed={violations[0].get('observed')}, "
              f"limit={violations[0].get('limit')}")

    return backtest_data


def step_e_counterfactual(client: httpx.Client) -> dict[str, Any]:
    """Run a counterfactual: rewrite the first message event's payload."""
    _step("E: Counterfactual on first message event")

    # First, find the first message event.
    events_resp = client.get(f"/foldos/events/{SESSION}")
    events: list[dict[str, Any]] = events_resp.json().get("events", [])

    # Find the first event suitable for counterfactual.
    # We'll use the first event regardless of kind.
    target_seq = events[0]["seq"] if events else 1
    target_kind = events[0]["kind"] if events else "unknown"
    print(f"  Target event: seq={target_seq}, kind={target_kind}")

    cf_thread = f"what-if-{uuid.uuid4().hex[:6]}"
    cf_resp = client.post(
        "/foldos/counterfactual",
        json={
            "session": SESSION,
            "event_seq": target_seq,
            "payload_overrides": {"content": "rewritten by counterfactual"},
            "thread": cf_thread,
        },
    )
    print(f"  POST /foldos/counterfactual -> {cf_resp.status_code}")
    _assert(cf_resp.status_code == 200, "counterfactual_200", cf_resp.text[:300])

    cf_data: dict[str, Any] = cf_resp.json()
    _assert("original_scope" in cf_data, "cf_has_original_scope")
    _assert("branch_scope" in cf_data, "cf_has_branch_scope")
    _assert(cf_data.get("replaced_seq") == target_seq, "cf_replaced_seq",
            f"replaced_seq={cf_data.get('replaced_seq')}")

    original_state: dict[str, Any] = cf_data.get("original_state", {})
    branch_state: dict[str, Any] = cf_data.get("branch_state", {})
    _assert(original_state != branch_state or target_kind not in ("message",),
            "cf_states_differ_or_non_message",
            "branch state should differ from original when replacing a message")
    print(f"  Original scope: {cf_data.get('original_scope')}")
    print(f"  Branch scope:   {cf_data.get('branch_scope')}")

    # Verify the branch thread exists in state.
    branch_state_resp = client.get(
        f"/foldos/state/{SESSION}",
        params={"thread": cf_thread},
    )
    _assert(branch_state_resp.status_code == 200, "branch_state_accessible")

    return cf_data


def step_f_usage(client: httpx.Client) -> dict[str, Any]:
    """Check usage summary.

    After the counterfactual step, there are two scopes for the same session
    (main + what-if branch), so we must specify thread=main to avoid a 409
    ambiguous scope error.
    """
    _step("F: Usage summary")

    usage_resp = client.get(
        f"/foldos/usage/{SESSION}",
        params={"thread": "main"},
    )
    print(f"  GET /foldos/usage/{SESSION}?thread=main -> {usage_resp.status_code}")
    _assert(usage_resp.status_code == 200, "usage_200")

    usage_data: dict[str, Any] = usage_resp.json()
    print(f"  LLM calls: {usage_data.get('llm_calls', 0)}")
    print(f"  Input tokens: {usage_data.get('input_tokens', 0)}")
    print(f"  Output tokens: {usage_data.get('output_tokens', 0)}")
    print(f"  Cost USD: {usage_data.get('cost_usd', 0.0)}")

    return usage_data


def print_summary() -> None:
    """Print final summary with SigNoz deep link."""
    _step("Summary")
    total = len(passed) + len(failed)
    print(f"\n  Results: {len(passed)}/{total} passed, {len(failed)}/{total} failed")

    if failed:
        print("\n  Failed assertions:")
        for label in failed:
            print(f"    - {label}")

    if first_trace_id and first_span_id:
        deep_link = (
            f"{SIGNOZ_URL.rstrip('/')}/trace/{first_trace_id}"
            f"?spanId={first_span_id}"
        )
        print("\n  SigNoz deep link for first trace:")
        print(f"    {deep_link}")
    else:
        print("\n  (No trace index entries found -- SigNoz deep link unavailable)")

    print(f"\n  Session: {SESSION}")
    print(f"  Service: {BASE_URL}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    _load_dotenv()

    signoz_ok = check_signoz()
    if not signoz_ok:
        print("  Continuing without SigNoz ...")

    proc = start_service()
    try:
        with httpx.Client(base_url=BASE_URL, timeout=30.0) as client:
            step_a_agent_run(client)
            policy = step_b_set_policy(client)
            step_b2_policy_veto(client)
            step_c_verify_chain(client)
            step_d_backtest(client)
            step_e_counterfactual(client)
            step_f_usage(client)
            _ = policy
    except Exception as exc:
        failed.append(f"unhandled_exception: {exc}")
        print(f"\n  UNHANDLED EXCEPTION: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        stop_service(proc)

    print_summary()

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
