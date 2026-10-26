"""Integration tests for the /api/docs Scalar endpoint."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from mosswork.api.app import build_app
from tests.support.app import mosswork_client, write_mosswork_config
from tests.support.auth import bootstrap_admin


async def test_api_docs_endpoint(tmp_mosswork_home: Path) -> None:
    write_mosswork_config(tmp_mosswork_home, enable_api_docs=True)
    async with mosswork_client(tmp_mosswork_home, patch_llm=False) as (_client, srv):
        app = build_app(srv)
        with TestClient(app) as c:
            r = c.get("/api/docs")
            assert r.status_code == 200
            assert "text/html" in r.headers["content-type"]
            assert "/api/openapi.json" in r.text
            spec = c.get("/api/openapi.json")
            assert spec.status_code == 200
            assert spec.json()["info"]["title"] == "moss-server API"


async def test_api_docs_disabled_by_default(tmp_mosswork_home: Path) -> None:
    async with mosswork_client(tmp_mosswork_home, patch_llm=False) as (client, _srv):
        await bootstrap_admin(client, tmp_mosswork_home)
        assert (await client.get("/api/docs")).status_code == 404
        assert (await client.get("/api/openapi.json")).status_code == 404
