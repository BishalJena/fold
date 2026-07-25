from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from foldos.provision.__main__ import _provision
from foldos.provision.client import SigNozAPIError, SigNozClient

API_KEY = "signoz-test-key"
BASE_URL = "http://signoz.test"


def test_api_key_header_sent_and_never_logged() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "success", "data": {"id": "org-1"}})

    client = SigNozClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),
    )
    client.validate_credentials()

    assert len(requests) == 1
    assert requests[0].headers["SIGNOZ-API-KEY"] == API_KEY
    assert API_KEY not in repr(client)
    client.close()


def test_missing_api_key_raises() -> None:
    with pytest.raises(SigNozAPIError, match="missing"):
        SigNozClient(base_url=BASE_URL, api_key="")


def test_validate_credentials_raises_on_auth_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = SigNozClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SigNozAPIError) as exc_info:
        client.validate_credentials()
    assert exc_info.value.status == 401
    assert "redacted" in str(exc_info.value).lower()
    client.close()


def test_error_response_body_is_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text='{"internal":"boom"}')

    client = SigNozClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SigNozAPIError) as exc_info:
        client.list_dashboards()
    assert exc_info.value.status == 500
    assert "redacted" in str(exc_info.value).lower()
    assert "boom" not in str(exc_info.value)
    client.close()


def test_dashboard_upsert_creates_then_updates() -> None:
    dashboards: list[dict[str, Any]] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/api/v2/dashboards":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "dashboards": dashboards,
                        "total": len(dashboards),
                        "tags": [],
                        "reservedKeywords": [],
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/api/v2/dashboards":
            payload = json.loads(request.content)
            created = {"id": "dash-1", **payload}
            dashboards.append(created)
            return httpx.Response(200, json={"status": "success", "data": created})
        if request.method == "PUT" and request.url.path == "/api/v2/dashboards/dash-1":
            payload = json.loads(request.content)
            updated = {"id": "dash-1", **payload}
            dashboards[0] = updated
            return httpx.Response(200, json={"status": "success", "data": updated})
        return httpx.Response(404)

    client = SigNozClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),
    )

    payload = {
        "name": "foldos-overview",
        "schemaVersion": "v2",
        "spec": {},
        "tags": [],
    }
    created = client.upsert_dashboard(payload)
    assert created["data"]["id"] == "dash-1"
    assert requests[0].method == "GET"
    assert requests[1].method == "POST"

    updated_payload = {
        **payload,
        "spec": {"display": {"name": "Overview"}},
    }
    updated = client.upsert_dashboard(updated_payload)
    assert updated["data"]["id"] == "dash-1"
    assert requests[2].method == "GET"
    assert requests[3].method == "PUT"
    client.close()


def test_rule_upsert_creates_then_updates() -> None:
    rules: list[dict[str, Any]] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/api/v2/rules":
            return httpx.Response(200, json={"status": "success", "data": rules})
        if request.method == "POST" and request.url.path == "/api/v2/rules":
            payload = json.loads(request.content)
            created = {"id": "rule-1", **payload}
            rules.append(created)
            return httpx.Response(200, json={"status": "success", "data": created})
        if request.method == "PUT" and request.url.path == "/api/v2/rules/rule-1":
            payload = json.loads(request.content)
            updated = {"id": "rule-1", **payload}
            rules[0] = updated
            return httpx.Response(200, json={"status": "success", "data": updated})
        return httpx.Response(404)

    client = SigNozClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),
    )

    payload = {
        "alert": "foldos-high-latency",
        "alertType": "METRIC_BASED_ALERT",
        "ruleType": "threshold_rule",
        "condition": {},
    }
    created = client.upsert_rule(payload)
    assert created["data"]["id"] == "rule-1"
    assert requests[0].method == "GET"
    assert requests[1].method == "POST"

    updated_payload = {**payload, "description": "updated"}
    updated = client.upsert_rule(updated_payload)
    assert updated["data"]["id"] == "rule-1"
    assert requests[2].method == "GET"
    assert requests[3].method == "PUT"
    client.close()


def test_cli_dry_run_skips_all_api_calls(tmp_path: Path) -> None:
    payload = {
        "name": "dry-run-dash",
        "schemaVersion": "v2",
        "spec": {},
        "tags": [],
    }
    file_path = tmp_path / "dash.json"
    file_path.write_text(json.dumps(payload))

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "success", "data": {}})

    client = SigNozClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),
    )

    code = _provision(
        base_url=BASE_URL,
        api_key=API_KEY,
        files=[str(file_path)],
        dry_run=True,
        client=client,
    )

    assert code == 0
    assert len(requests) == 0
    client.close()


def test_cli_provision_dashboard_and_rule(tmp_path: Path) -> None:
    dash = {"name": "cli-dash", "schemaVersion": "v2", "spec": {}, "tags": []}
    rule = {
        "alert": "cli-rule",
        "alertType": "METRIC_BASED_ALERT",
        "ruleType": "threshold_rule",
        "condition": {},
    }
    dash_path = tmp_path / "dash.json"
    rule_path = tmp_path / "rule.json"
    dash_path.write_text(json.dumps(dash))
    rule_path.write_text(json.dumps(rule))

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/api/v2/orgs/me":
            return httpx.Response(200, json={"status": "success", "data": {"id": "org-1"}})
        if request.method == "GET" and request.url.path == "/api/v2/dashboards":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "dashboards": [],
                        "total": 0,
                        "tags": [],
                        "reservedKeywords": [],
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/api/v2/dashboards":
            payload = json.loads(request.content)
            return httpx.Response(201, json={"status": "success", "data": {"id": "d1", **payload}})
        if request.method == "GET" and request.url.path == "/api/v2/rules":
            return httpx.Response(200, json={"status": "success", "data": []})
        if request.method == "POST" and request.url.path == "/api/v2/rules":
            payload = json.loads(request.content)
            return httpx.Response(201, json={"status": "success", "data": {"id": "r1", **payload}})
        return httpx.Response(404)

    client = SigNozClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),
    )

    code = _provision(
        base_url=BASE_URL,
        api_key=API_KEY,
        files=[str(dash_path), str(rule_path)],
        client=client,
    )

    assert code == 0
    assert any(r.method == "POST" and r.url.path == "/api/v2/dashboards" for r in requests)
    assert any(r.method == "POST" and r.url.path == "/api/v2/rules" for r in requests)
    client.close()
