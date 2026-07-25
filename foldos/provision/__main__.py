from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from foldos.provision.client import SigNozAPIError, SigNozClient


def _classify_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if "schemaVersion" in payload and "spec" in payload:
        return "dashboard"
    if "alert" in payload:
        return "rule"
    raise ValueError("payload must be a dashboard (schemaVersion+spec) or rule (alert)")


def _provision(
    base_url: str,
    api_key: str,
    files: list[str],
    *,
    dry_run: bool = False,
    client: SigNozClient | None = None,
) -> int:
    if client is None:
        client = SigNozClient(base_url=base_url, api_key=api_key)

    try:
        if not dry_run:
            client.validate_credentials()
    except SigNozAPIError as exc:
        print(f"credential validation failed: {exc.message}", file=sys.stderr)
        return 1

    for path in files:
        payload = json.loads(Path(path).read_text())
        kind = _classify_payload(payload)
        if dry_run:
            print(f"dry-run: would upsert {kind} from {path}")
            continue
        try:
            if kind == "dashboard":
                result = client.upsert_dashboard(payload)
            else:
                result = client.upsert_rule(payload)
        except SigNozAPIError as exc:
            print(f"failed to upsert {kind} from {path}: {exc.message}", file=sys.stderr)
            return 1
        result_id = ""
        if isinstance(result, dict):
            result_id = str(result.get("data", {}).get("id", ""))
        print(f"upserted {kind} from {path}: id={result_id or 'unknown'}")

    client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision SigNoz dashboards and rules")
    parser.add_argument("--dry-run", action="store_true", help="preview only")
    parser.add_argument("files", nargs="*", help="JSON payload files")
    args = parser.parse_args(argv)

    base_url = os.environ.get("SIGNOZ_URL", "")
    api_key = os.environ.get("SIGNOZ_API_KEY", "")
    if not base_url or not api_key:
        print("SIGNOZ_URL and SIGNOZ_API_KEY must be set", file=sys.stderr)
        return 1

    return _provision(
        base_url=base_url,
        api_key=api_key,
        files=args.files,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
