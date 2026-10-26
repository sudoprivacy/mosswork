"""Exception handlers log server-side MossworkError details."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from tests.support.app import ensure_control_plane_bound, write_mosswork_config

from mosswork.api.app import build_app
from mosswork.infra.errors import ErrorCode, MossworkError
from mosswork.infra.server import MossworkServer


@pytest.fixture
async def client(tmp_path: Path):
    # Disable dashboard so SPA ``/{full_path:path}`` is not mounted; otherwise
    # routes registered after build_app never match (first-match-wins).
    write_mosswork_config(tmp_path, enable_dashboard=False)
    srv = MossworkServer(home=tmp_path)
    await srv.start()
    await ensure_control_plane_bound(srv)
    app = build_app(srv)

    # Outside /api so JWT middleware does not intercept.
    @app.get("/_test/mosswork-500")
    async def _boom() -> None:
        raise MossworkError(ErrorCode.INTERNAL_ERROR, "jwt secret missing")

    @app.get("/_test/mosswork-401")
    async def _auth() -> None:
        raise MossworkError(ErrorCode.AUTH_FAILED, "missing credentials")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
    await srv.stop()


async def test_mosswork_error_5xx_is_logged(client: httpx.AsyncClient) -> None:
    with patch("mosswork.api.app.logger") as mock_logger:
        r = await client.get("/_test/mosswork-500", headers={"Accept-Language": "zh"})
    assert r.status_code == 500
    assert r.json()["error"]["message"] == "服务器内部错误。"
    mock_logger.error.assert_called_once()
    args, kwargs = mock_logger.error.call_args
    rendered = args[0] % args[1:]
    assert "INTERNAL_ERROR" in rendered
    assert "jwt secret missing" in rendered
    assert kwargs.get("exc_info") is not None


async def test_mosswork_error_4xx_is_not_logged(client: httpx.AsyncClient) -> None:
    with patch("mosswork.api.app.logger") as mock_logger:
        r = await client.get("/_test/mosswork-401")
    assert r.status_code == 401
    mock_logger.error.assert_not_called()
