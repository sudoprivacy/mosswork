from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from octop.api.deps import is_jwt_exempt_path
from octop.api.middleware.jwt_auth import install as install_jwt
from octop.api.middleware.setup_lockdown import install as install_lockdown
from octop.api.routers.update import router
from octop.infra.errors import MossworkError
from octop.infra.setup import desktop_update

VERSION = "0.0.8"
FILENAME = "mosswork-company-windows-amd64-0.0.8-update.exe"
MANIFEST = {
    "schemaVersion": 1,
    "channel": "stable",
    "version": VERSION,
    "metadata": {"distribution": "mosswork-company"},
    "artifacts": [
        {
            "filename": FILENAME,
            "url": f"../releases/v{VERSION}/{FILENAME}",
            "platform": "windows",
            "arch": "amd64",
            "size": 19,
            "digestAlgo": "sha256",
            "digest": "unchanged",
        }
    ],
}


def mock_distribution(
    monkeypatch: pytest.MonkeyPatch, manifest: Any, status: int = 200
) -> list[str]:
    requests: list[str] = []
    original = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if str(request.url) == desktop_update.FEED_URL:
            return httpx.Response(status, json=manifest)
        if str(request.url) == desktop_update.RELEASE_ROOT + f"releases/v{VERSION}/{FILENAME}":
            return httpx.Response(200, content=b"complete executable")
        return httpx.Response(404)

    monkeypatch.setattr(
        desktop_update.httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle)),
    )
    return requests


@pytest.mark.asyncio
async def test_manifest_and_bytes_available_before_login_without_update_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_client = httpx.AsyncClient
    requests = mock_distribution(monkeypatch, copy.deepcopy(MANIFEST))
    app = FastAPI()
    app.include_router(router, prefix="/api")
    server = SimpleNamespace(services=None, config=None, canonical_configuration=object())
    install_jwt(app, server)
    install_lockdown(app, server)
    schema = app.openapi()
    assert schema["paths"]["/api/update/desktop/latest/desktop-update.json"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]["$ref"].endswith("DesktopUpdateManifest")
    async with original_client(
        transport=httpx.ASGITransport(app=app), base_url="http://company"
    ) as client:
        response = await client.get("/api/update/desktop/latest/desktop-update.json")
        assert response.status_code == 200
        artifact = response.json()["artifacts"][0]
        assert artifact == MANIFEST["artifacts"][0]
        download = await client.get(f"/api/update/desktop/releases/v{VERSION}/{FILENAME}")
        assert download.status_code == 200
        assert download.content == b"complete executable"
        assert "location" not in download.headers
        assert (await client.post("/api/update/upgrade")).status_code == 503
    assert requests == [
        desktop_update.FEED_URL,
        desktop_update.RELEASE_ROOT + f"releases/v{VERSION}/{FILENAME}",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://elsewhere.example/update.exe",
        "../latest/update.exe",
        "../releases/v0.0.7/" + FILENAME,
    ],
)
async def test_manifest_rejects_foreign_or_mutable_downloads(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    manifest = copy.deepcopy(MANIFEST)
    manifest["artifacts"][0]["url"] = url
    mock_distribution(monkeypatch, manifest)
    with pytest.raises(ValueError):
        await desktop_update.fetch_manifest()


@pytest.mark.parametrize(
    "path",
    [
        "/api/update/status",
        "/api/update/upgrade",
        "/api/update/desktop/anything",
        f"/api/update/desktop/releases/v0.0.7/{FILENAME}",
        "/api/update/desktop/releases/v0.0.8/installer.exe",
    ],
)
def test_public_download_exemption_does_not_open_adjacent_routes(path: str) -> None:
    assert not is_jwt_exempt_path(path)


@pytest.mark.asyncio
async def test_invalid_artifact_never_contacts_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = mock_distribution(monkeypatch, MANIFEST)
    with pytest.raises(ValueError):
        await desktop_update.open_artifact(VERSION, "../../arbitrary.exe")
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_status", "expected_status", "code"),
    [
        (404, 404, "DESKTOP_UPDATE_NOT_PUBLISHED"),
        (403, 503, "DESKTOP_UPDATE_SOURCE_UNAVAILABLE"),
        (502, 503, "DESKTOP_UPDATE_SOURCE_UNAVAILABLE"),
    ],
)
async def test_manifest_returns_specific_upstream_errors(
    monkeypatch: pytest.MonkeyPatch,
    upstream_status: int,
    expected_status: int,
    code: str,
) -> None:
    original = httpx.AsyncClient
    mock_distribution(monkeypatch, None, upstream_status)
    app = FastAPI()
    app.include_router(router, prefix="/api")

    @app.exception_handler(MossworkError)
    async def error_response(_: Any, exc: MossworkError) -> JSONResponse:
        return JSONResponse(exc.to_envelope(locale="zh"), status_code=exc.status)

    async with original(
        transport=httpx.ASGITransport(app=app), base_url="http://company"
    ) as client:
        response = await client.get("/api/update/desktop/latest/desktop-update.json")
    assert response.status_code == expected_status
    error = response.json()["error"]
    assert error["code"] == code
    assert error["details"]["upstream_status"] == upstream_status
    assert error["message"] != "服务器内部错误。"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [httpx.ConnectError("private proxy address"), httpx.ReadTimeout("timeout")]
)
async def test_manifest_network_failure_is_diagnostic_without_exposing_connection_details(
    monkeypatch: pytest.MonkeyPatch,
    error: httpx.HTTPError,
) -> None:
    from octop.api.routers.update import desktop_update_manifest

    async def fail() -> dict[str, Any]:
        raise error

    monkeypatch.setattr(desktop_update, "fetch_manifest", fail)
    with pytest.raises(MossworkError) as raised:
        await desktop_update_manifest()
    exc = raised.value
    assert exc.status == 503
    assert exc.code.value == "DESKTOP_UPDATE_SOURCE_UNAVAILABLE"
    assert exc.details["reason"] in {"source_connection_failed", "source_timeout"}
    assert "private proxy address" not in str(exc.to_envelope())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest", [[], {"metadata": None}, {"version": 7}, {**MANIFEST, "artifacts": [None]}]
)
async def test_malformed_manifest_is_reported_as_invalid_not_internal_error(
    monkeypatch: pytest.MonkeyPatch,
    manifest: Any,
) -> None:
    from octop.api.routers.update import desktop_update_manifest

    mock_distribution(monkeypatch, manifest)
    with pytest.raises(MossworkError) as raised:
        await desktop_update_manifest()
    assert raised.value.status == 502
    assert raised.value.code.value == "DESKTOP_UPDATE_MANIFEST_INVALID"


@pytest.mark.asyncio
async def test_incomplete_artifact_is_caught_before_response_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from octop.api.routers.update import desktop_update_manifest

    manifest = copy.deepcopy(MANIFEST)
    del manifest["artifacts"][0]["digest"]
    mock_distribution(monkeypatch, manifest)
    with pytest.raises(MossworkError) as raised:
        await desktop_update_manifest()
    assert raised.value.code.value == "DESKTOP_UPDATE_MANIFEST_INVALID"
