from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from foldos.app import create_app


@pytest.mark.asyncio
async def test_app_starts_and_serves_health_check() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/foldos/cut", params={"session": "test"})
        assert response.status_code == 200
        data = response.json()
        assert "at" in data
        assert "cut" in data
