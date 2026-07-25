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
        sys.executable,
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
    ui_cmd = [pnpm, "dev", "-p", "3000"]
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

    processes = [backend, ui]
    try:
        while all(proc.poll() is None for proc in processes):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[dev] Shutting down...")
    finally:
        _shutdown(processes)
        print("[dev] Shutdown complete.")


if __name__ == "__main__":
    main()
