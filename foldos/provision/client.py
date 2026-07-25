from __future__ import annotations

from typing import Any

import httpx

API_KEY_HEADER = "SIGNOZ-API-KEY"


class SigNozAPIError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


class SigNozClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise SigNozAPIError(401, "missing SigNoz API key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._own_client = client is None
        if client is not None:
            self._client = client
        else:
            kwargs: dict[str, Any] = {}
            if transport is not None:
                kwargs["transport"] = transport
            self._client = httpx.Client(base_url=self._base_url, **kwargs)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(base_url={self._base_url!r})"

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    def __enter__(self) -> SigNozClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._base_url}{path}"
        headers: dict[str, str] = dict(kwargs.pop("headers", {}))
        headers.setdefault("Accept", "application/json")
        headers.setdefault("Content-Type", "application/json")
        headers[API_KEY_HEADER] = self._api_key
        try:
            response = self._client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise SigNozAPIError(0, f"SigNoz request failed: {exc}") from exc
        if response.is_success:
            if response.status_code == 204 or not response.content:
                return None
            return response.json()
        raise self._error_from_response(response)

    @staticmethod
    def _error_from_response(response: httpx.Response) -> SigNozAPIError:
        status = response.status_code
        try:
            response.json()
        except Exception:
            pass
        return SigNozAPIError(status, f"SigNoz API error {status}: <body redacted>")

    def validate_credentials(self) -> Any:
        return self._request("GET", "/api/v2/orgs/me")

    def list_dashboards(
        self,
        *,
        query: str | None = None,
        sort: str | None = None,
        order: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if query is not None:
            params["query"] = query
        if sort is not None:
            params["sort"] = sort
        if order is not None:
            params["order"] = order
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return self._request("GET", "/api/v2/dashboards", params=params)

    def create_dashboard(self, payload: dict[str, Any]) -> Any:
        payload = {k: v for k, v in payload.items() if k != "id"}
        return self._request("POST", "/api/v2/dashboards", json=payload)

    def update_dashboard(self, dashboard_id: str, payload: dict[str, Any]) -> Any:
        payload = {k: v for k, v in payload.items() if k != "id"}
        return self._request("PUT", f"/api/v2/dashboards/{dashboard_id}", json=payload)

    def upsert_dashboard(self, payload: dict[str, Any]) -> Any:
        name = payload.get("name")
        if not name:
            raise SigNozAPIError(400, "dashboard payload missing 'name' stable name")
        existing = self._find_dashboard(str(name))
        if existing is not None:
            return self.update_dashboard(str(existing["id"]), payload)
        return self.create_dashboard(payload)

    def _find_dashboard(self, name: str) -> dict[str, Any] | None:
        data = self.list_dashboards(limit=1000)
        dashboards = data.get("data", {}).get("dashboards", [])
        for dashboard in dashboards:
            if isinstance(dashboard, dict) and dashboard.get("name") == name:
                return dashboard
        return None

    def list_rules(self) -> Any:
        return self._request("GET", "/api/v2/rules")

    def create_rule(self, payload: dict[str, Any]) -> Any:
        payload = {k: v for k, v in payload.items() if k != "id"}
        return self._request("POST", "/api/v2/rules", json=payload)

    def update_rule(self, rule_id: str, payload: dict[str, Any]) -> Any:
        payload = {k: v for k, v in payload.items() if k != "id"}
        return self._request("PUT", f"/api/v2/rules/{rule_id}", json=payload)

    def upsert_rule(self, payload: dict[str, Any]) -> Any:
        name = payload.get("alert")
        if not name:
            raise SigNozAPIError(400, "rule payload missing 'alert' stable name")
        data = self.list_rules()
        for rule in data.get("data", []):
            if rule.get("alert") == name:
                return self.update_rule(str(rule["id"]), payload)
        return self.create_rule(payload)
