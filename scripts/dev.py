#!/usr/bin/env python3
"""Start the FoldOS backend and the Agno Agent UI with one command."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_DIR = REPO_ROOT / "ui" / "agent-ui"


def _python_executable() -> str:
    venv_python = REPO_ROOT / ".venv" / "bin" / "python3"
    if venv_python.exists():
        return str(venv_python)
    venv_python_alt = REPO_ROOT / ".venv" / "bin" / "python"
    if venv_python_alt.exists():
        return str(venv_python_alt)
    return sys.executable


def _run(cmd: list[str], cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        start_new_session=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def _ensure_pnpm() -> str:
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        print("ERROR: pnpm is required to run the Agno UI.")
        print("Install it from https://pnpm.io/installation")
        sys.exit(1)
    return pnpm


def _install_ui_deps() -> None:
    if (UI_DIR / "node_modules").exists():
        return
    print("[dev] Installing UI dependencies (one-time)...")
    pnpm = _ensure_pnpm()
    subprocess.run([pnpm, "install"], cwd=UI_DIR, check=True)


def _shutdown(processes: list[subprocess.Popen]) -> None:
    for proc in processes:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
    for proc in processes:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


def main() -> None:
    if not (UI_DIR / "package.json").exists():
        print("ERROR: ui/agent-ui is missing. Run:")
        print("  git submodule update --init --recursive")
        sys.exit(1)

    _install_ui_deps()

    backend_cmd = [
        _python_executable(),
        "-m",
        "uvicorn",
        "foldos.app:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        "7777",
    ]
    backend = _run(backend_cmd, REPO_ROOT)

    pnpm = _ensure_pnpm()
    ui_cmd = [pnpm, "dev"]
    ui = _run(ui_cmd, UI_DIR)

    print()
    print("=" * 60)
    print("FoldOS is starting up...")
    print("  Backend: http://127.0.0.1:7777/console")
    print("  Agent UI: http://localhost:3000")
    print("  SigNoz:  http://localhost:8080")
    print("Press Ctrl+C to stop both services.")
    print("=" * 60)
    print()

    processes = {"backend": backend, "ui": ui}
    exited_name: str | None = None
    try:
        while True:
            for name, proc in processes.items():
                if proc.poll() is not None:
                    exited_name = name
                    break
            if exited_name is not None:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[dev] Shutting down...")
    else:
        if exited_name is not None:
            print(f"\n[dev] {exited_name} exited unexpectedly (code {processes[exited_name].returncode}).")
            print("[dev] Shutting down...")
    finally:
        _shutdown(list(processes.values()))
        print("[dev] Shutdown complete.")


if __name__ == "__main__":
    main()
