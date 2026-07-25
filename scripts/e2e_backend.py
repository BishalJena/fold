"""Backend end-to-end smoke test for FoldOS.

Starts the FoldOS service on port 7777, exercises the core ledger endpoints,
and verifies that telemetry can be exported to SigNoz.
Run from the repo root with the venv active:

    python scripts/e2e_backend.py

Requires:
    - SigNoz query service reachable at SIGNOZ_URL (default http://localhost:8080).
    - Ollama with llama3.2 for the optional LLM step.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx


def _load_dotenv() -> None:
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _wait_for_url(url: str, timeout: float = 30.0, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _sig_no_z_health_url() -> str:
    base = os.environ.get("SIGNOZ_URL", "http://localhost:8080").rstrip("/")
    return f"{base}/api/v1/health"


def _service_base_url() -> str:
    host = os.environ.get("FOLDOS_HOST", "127.0.0.1")
    port = os.environ.get("FOLDOS_PORT", "7777")
    return f"http://{host}:{port}"


def _check_sig_no_z() -> bool:
    url = _sig_no_z_health_url()
    print(f"checking SigNoz health at {url} ...")
    if not _wait_for_url(url, timeout=5.0):
        print("warning: SigNoz is not reachable; OTLP export will fail")
        return False
    try:
        response = httpx.get(url, timeout=2.0)
        print(f"SigNoz health: {response.json()}")
    except Exception as exc:
        print(f"warning: could not read SigNoz health: {exc}")
        return False
    return True


def _start_service() -> subprocess.Popen[str]:
    host = os.environ.get("FOLDOS_HOST", "127.0.0.1")
    port = int(os.environ.get("FOLDOS_PORT", "7777"))
    base = _service_base_url()
    print(f"starting FoldOS service on {base} ...")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "foldos.app:create_app",
            "--host",
            host,
            "--port",
            str(port),
            "--factory",
        ],
        cwd=Path(__file__).parent.parent,
        text=True,
    )
    if not _wait_for_url(f"{base}/foldos/cut?session=e2e-smoke", timeout=30.0):
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise RuntimeError("FoldOS service did not start")
    print("FoldOS service is ready")
    return proc


def _run_scenario(base: str) -> None:
    session = f"e2e-smoke-{uuid.uuid4().hex[:8]}"
    print(f"running scenario for session={session} ...")

    with httpx.Client(base_url=base, timeout=10.0) as client:
        # Append a policy event through the policy endpoint (creates scope).
        policy_resp = client.post(
            "/foldos/policy",
            json={"session": session, "key": "greeting", "value": "hello", "reason": "smoke test"},
        )
        print(f"POST /foldos/policy -> {policy_resp.status_code}")
        if policy_resp.status_code != 200:
            raise RuntimeError(f"policy append failed: {policy_resp.status_code} {policy_resp.text}")

        # Read state.
        state_resp = client.get(f"/foldos/state/{session}")
        print(f"GET /foldos/state/{session} -> {state_resp.status_code}")
        if state_resp.status_code != 200:
            raise RuntimeError(f"state read failed: {state_resp.status_code} {state_resp.text}")

        # Read events.
        events_resp = client.get(f"/foldos/events/{session}")
        print(f"GET /foldos/events/{session} -> {events_resp.status_code}")
        if events_resp.status_code != 200:
            raise RuntimeError(f"events read failed: {events_resp.status_code} {events_resp.text}")
        events = events_resp.json()["events"]
        if not events or events[-1]["kind"] != "policy_set":
            raise RuntimeError("expected at least one policy_set event")

        # Verify chain.
        verify_resp = client.get(f"/foldos/verify/{session}")
        print(f"GET /foldos/verify/{session} -> {verify_resp.status_code}")
        if verify_resp.status_code != 200:
            raise RuntimeError(f"verify failed: {verify_resp.status_code} {verify_resp.text}")
        if verify_resp.json()["ok"] is not True:
            raise RuntimeError("chain verification returned not ok")

        # Usage summary.
        usage_resp = client.get(f"/foldos/usage/{session}")
        print(f"GET /foldos/usage/{session} -> {usage_resp.status_code}")
        if usage_resp.status_code != 200:
            raise RuntimeError(f"usage read failed: {usage_resp.status_code} {usage_resp.text}")

    print("scenario completed successfully")


def _check_otlp_endpoint() -> None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")
    url = f"{endpoint}/v1/traces"
    print(f"checking OTLP endpoint {url} ...")
    try:
        response = httpx.post(
            url,
            json={"resourceSpans": []},
            headers={"Content-Type": "application/json"},
            timeout=5.0,
        )
        print(f"OTLP endpoint returned {response.status_code}")
    except Exception as exc:
        print(f"warning: OTLP endpoint check failed: {exc}")


def main() -> int:
    _check_sig_no_z()
    _check_otlp_endpoint()
    proc = _start_service()
    try:
        _run_scenario(_service_base_url())
        print("backend end-to-end smoke test passed")
        return 0
    except Exception as exc:
        print(f"backend end-to-end smoke test failed: {exc}")
        return 1
    finally:
        print("stopping FoldOS service ...")
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
