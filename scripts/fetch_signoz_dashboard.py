#!/usr/bin/env python3
"""Fetch a SigNoz dashboard JSON export using API credentials from the environment.

Examples:
    SIGNOZ_URL=http://localhost:8080 SIGNOZ_API_KEY=... \
        python scripts/fetch_signoz_dashboard.py --name foldos-governance
    SIGNOZ_URL=http://localhost:8080 SIGNOZ_API_KEY=... \
        python scripts/fetch_signoz_dashboard.py --id <dashboard-id> > dashboard.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _request(url: str, api_key: str) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "SIGNOZ-API-KEY": api_key,
        },
    )
    try:
        with urlopen(request) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"SigNoz API request failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"SigNoz API request failed: {exc.reason}") from exc


def _dashboards(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        return []
    data = response.get("data", {})
    if not isinstance(data, dict):
        return []
    dashboards = data.get("dashboards", [])
    return [dashboard for dashboard in dashboards if isinstance(dashboard, dict)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch an exported SigNoz dashboard JSON payload")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--id", help="SigNoz dashboard ID")
    selector.add_argument("--name", help="exact SigNoz dashboard name")
    args = parser.parse_args(argv)

    base_url = os.environ.get("SIGNOZ_URL", "").rstrip("/")
    api_key = os.environ.get("SIGNOZ_API_KEY", "")
    if not base_url or not api_key:
        print("SIGNOZ_URL and SIGNOZ_API_KEY must be set", file=sys.stderr)
        return 2

    try:
        if args.id:
            dashboard = _request(f"{base_url}/api/v2/dashboards/{args.id}", api_key)
        else:
            url = f"{base_url}/api/v2/dashboards?{urlencode({'limit': 1000})}"
            matches = [item for item in _dashboards(_request(url, api_key)) if item.get("name") == args.name]
            if not matches:
                print(f"no dashboard named {args.name!r} found", file=sys.stderr)
                return 1
            dashboard_id = matches[0].get("id")
            if not dashboard_id:
                print(f"dashboard named {args.name!r} has no ID", file=sys.stderr)
                return 1
            dashboard = _request(f"{base_url}/api/v2/dashboards/{dashboard_id}", api_key)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # GET /dashboards/{id} returns {"data": <dashboard>} in SigNoz v0.133.
    if isinstance(dashboard, dict) and isinstance(dashboard.get("data"), dict):
        dashboard = dashboard["data"]
    print(json.dumps(dashboard, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
