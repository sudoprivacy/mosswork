from types import SimpleNamespace

import pytest

from mosswork.config import MossworkConfig
from mosswork.infra.auth.tokens import sign_token
from mosswork.infra.db.migrate import run_migrations
from mosswork.infra.db.pool import SqlitePool
from mosswork.infra.db.services import build_shared_services
from mosswork.infra.devices.manager import DeviceManager
from mosswork.infra.users.identity import User
from mosswork.infra.utils.paths import PathLayout


@pytest.fixture
def stack(tmp_path):
    db = SqlitePool(tmp_path / "devices.db")
    run_migrations(db)
    services = build_shared_services(paths=PathLayout(tmp_path), config=MossworkConfig(), db=db)
    services.secret_repo.get_or_create("jwt", lambda: b"test-device-channel-signing-secret-32")
    alice = services.user_repo.create(username="alice", role="user")
    bob = services.user_repo.create(username="bob", role="admin")
    alice = services.user_repo.get(alice)
    bob = services.user_repo.get(bob)

    def get_user(user_id):
        row = services.user_repo.get(user_id)
        return User(row.id, row.username, row.role, None) if row and not row.disabled else None

    server = SimpleNamespace(
        _started=True,
        services=services,
        devices=DeviceManager(services.device_repo),
        user_manager=SimpleNamespace(get_by_id=get_user),
    )
    tokens = {
        row.username: sign_token(
            services.secret_repo.get("jwt"),
            sub=row.id,
            uname=row.username,
            role=row.role,
            sid="session-" + row.username,
        )
        for row in (alice, bob)
    }
    yield server, alice.id, bob.id, tokens
    db.close()
