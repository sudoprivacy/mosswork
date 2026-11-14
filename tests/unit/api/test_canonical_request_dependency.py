"""Explicit adapted-route dependency; no middleware or global auth bypass."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from tests.unit.identity.test_canonical_human_request_projection import (
    _IDS,
    _resolver,
    _snapshots,
    _token,
)
from tests.unit.identity.test_canonical_human_request_projection import (
    legacy_db as legacy_db,
)
from tests.unit.identity.test_canonical_human_request_projection import (
    product as product,
)

from octop.api.common.canonical_identity import canonical_authenticated_request
from octop.api.routers.canonical_identity import build_canonical_identity_router
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserRepo
from octop.infra.errors import MossworkError
from octop.infra.identity.request_principal import CanonicalAuthenticatedRequest


def _app(path: Path, product: SqlitePool) -> FastAPI:
    app = FastAPI()
    app.state.canonical_requests = _resolver(path, product)
    app.include_router(build_canonical_identity_router(_snapshots(path)), prefix="/identity")

    @app.exception_handler(MossworkError)
    async def error(_request: Request, exc: MossworkError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    @app.get("/identity-probe")
    async def probe(
        principal: Annotated[
            CanonicalAuthenticatedRequest, Depends(canonical_authenticated_request)
        ],
    ) -> dict[str, str | int]:
        # Test-only identity probe: this performs no native/resource effect.
        current = await principal.revalidate()
        return {
            "account": current.snapshot.account_id,
            "member": current.snapshot.member.owner_id,
            "local_id": current.projection.local_user_id,
        }

    return app


async def test_actual_header_principal_and_readonly_me_coexist_without_global_allocation(
    legacy_db: Path, product: SqlitePool
) -> None:
    before = legacy_db.read_bytes()
    app = _app(legacy_db, product)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        me = await client.get("/identity/me", headers={"Authorization": f"Bearer {_token()}"})
        assert me.status_code == 200
        assert UserRepo(product).count() == 0
        response = await client.get(
            "/identity-probe", headers={"Authorization": f"bEaReR {_token()}"}
        )
        assert response.status_code == 200
        assert response.json() == {
            "account": _IDS["account"],
            "member": _IDS["homeMember"],
            "local_id": 1,
        }
        assert UserRepo(product).count() == 1
    assert legacy_db.read_bytes() == before


@pytest.mark.parametrize(
    "source", ["none", "query", "body", "cookie", "meta", "basic", "duplicate"]
)
async def test_no_query_body_cookie_or_raw_metadata_authentication(
    legacy_db: Path, product: SqlitePool, source: str
) -> None:
    app = _app(legacy_db, product)
    app.state.current_user = {"account_id": _IDS["account"], "role": "admin"}
    headers = []
    if source == "basic":
        headers = [("Authorization", f"Basic {_token()}")]
    elif source == "meta":
        headers = [("X-Canonical-Meta", '{"role":"admin"}')]
    elif source == "duplicate":
        headers = [("Authorization", f"Bearer {_token()}"), ("Authorization", f"Bearer {_token()}")]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        if source == "cookie":
            client.cookies.set("access_token", _token())
        response = await client.request(
            "GET",
            "/identity-probe",
            params={"access_token": _token()} if source == "query" else {},
            json={"access_token": _token()} if source == "body" else None,
            headers=headers,
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_FAILED"
    assert UserRepo(product).count() == 0


@pytest.mark.parametrize("injection", ["missing", "metadata"])
async def test_missing_or_metadata_resolver_injection_fails_closed(
    legacy_db: Path, product: SqlitePool, injection: str
) -> None:
    app = _app(legacy_db, product)
    if injection == "missing":
        del app.state.canonical_requests
    else:
        app.state.canonical_requests = SimpleNamespace(
            snapshot={"account_id": _IDS["account"], "role": "admin"}
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/identity-probe", headers={"Authorization": f"Bearer {_token()}"}
        )
    assert response.status_code == 503
    assert response.json()["error"]["details"] == {"dependency": "canonical_request_resolver"}
    assert UserRepo(product).count() == 0
