from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from tests.unit.market.test_resource_market import market_env as market_env

from octop.api.deps import current_user, get_server
from octop.api.routers.resource_market import router
from octop.infra.errors import MossworkError
from octop.infra.market.service import example_package


@pytest.mark.asyncio
async def test_http_import_review_and_real_permission_gate(market_env):
    services, users = market_env
    user = users["developer"]
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_server] = lambda: SimpleNamespace(services=services)
    app.dependency_overrides[current_user] = lambda: user

    @app.exception_handler(MossworkError)
    async def error_handler(request, error):
        return JSONResponse(
            {"error": {"code": error.code, "details": error.details}}, status_code=error.status
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/market/import",
            content=example_package("expert"),
            headers={"Content-Type": "application/zip"},
        )
        assert response.status_code == 201, response.text
        draft = response.json()
        path = f"/api/market/drafts/{draft['draft_id']}"
        body = {"expected_sha256": draft["object_sha256"]}
        response = await client.post(path + "/submit", json=body)
        assert response.status_code == 200
        response = await client.post(path + "/review", json={**body, "action": "publish"})
        assert response.status_code == 403
        user = users["admin"]
        response = await client.post(path + "/review", json={**body, "action": "publish"})
        assert response.status_code == 200
        detail = (await client.get(f"/api/market/resources/{draft['resource_id']}")).json()
        response = await client.get(
            f"/api/market/versions/{detail['versions'][0]['version_id']}/download"
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        user = users["other"]
        user.permissions = []
        assert (await client.get("/api/market/resources")).status_code == 403
    spec = app.openapi()
    operation = spec["paths"]["/api/market/import"]["post"]
    assert operation["summary"]
    assert "application/zip" in operation["requestBody"]["content"]
    assert spec["components"]["schemas"]["Draft"]["properties"]["manifest"]


@pytest.mark.asyncio
async def test_market_requests_use_global_runtime_instead_of_employee_shards():
    from octop.api.common.runtime_routing import runtime_owner

    user = SimpleNamespace(id=42)
    assert await runtime_owner(None, "/api/market/import", user, {}) is None
    assert await runtime_owner(None, "/api/market/resources", user, {}) is None
    assert await runtime_owner(None, "/api/agents", user, {}) == 42
