"""tests/integration/test_server_lifecycle.py"""

from __future__ import annotations

from pathlib import Path

import pytest

from mosswork.infra.server import MossworkServer
from tests.support.app import ensure_control_plane_bound


async def _bind_default_sqlite(srv: MossworkServer) -> None:
    await ensure_control_plane_bound(srv)


@pytest.fixture
async def server(tmp_mosswork_home: Path):
    srv = MossworkServer(home=tmp_mosswork_home)
    await srv.start()
    await _bind_default_sqlite(srv)
    yield srv
    await srv.stop()


async def test_start_defers_db_until_bind(tmp_mosswork_home: Path):
    srv = MossworkServer(home=tmp_mosswork_home)
    await srv.start()
    try:
        assert tmp_mosswork_home.is_dir()
        assert (tmp_mosswork_home / "config.json").exists()
        assert not (tmp_mosswork_home / "mosswork.db").exists()
        assert srv.database_bound is False
        assert srv.services is None
        await _bind_default_sqlite(srv)
        assert (tmp_mosswork_home / "mosswork.db").exists()
        assert srv.database_bound is True
    finally:
        await srv.stop()


async def test_start_creates_root_and_db(server: MossworkServer, tmp_mosswork_home: Path):
    assert tmp_mosswork_home.is_dir()
    assert (tmp_mosswork_home / "mosswork.db").exists()
    assert (tmp_mosswork_home / "config.json").exists()


async def test_jwt_secret_seeded(server: MossworkServer):
    assert server.services is not None
    secret = server.services.secret_repo.get("jwt")
    assert secret is not None
    assert len(secret) >= 32


async def test_user_manager_loaded(server: MossworkServer):
    assert server.user_manager is not None
    assert server.user_manager.count() == 0


async def test_boot_then_stop_idempotent(tmp_mosswork_home: Path):
    srv = MossworkServer(home=tmp_mosswork_home)
    await srv.start()
    await _bind_default_sqlite(srv)
    await srv.stop()
    await srv.stop()  # idempotent
