import asyncio
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import PostgresPool, SqlitePool
from octop.infra.db.repos.devices import DeviceRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.devices import manager as channel
from octop.infra.devices.manager import _digest
from tests.support.postgresql import requires_postgresql
from tests.unit.devices.test_channel import finish
from tests.unit.devices.test_file_tasks import bind, connect_files


async def test_pending_file_call_survives_old_cutoff(stack, monkeypatch):
    server, owner, _, _ = stack
    manager = server.devices
    now = int(time.time())
    monkeypatch.setattr(channel, "time", SimpleNamespace(time=lambda: now))
    socket, runner, selection = await connect_files(manager, owner)
    try:
        await socket.send({"type": "lease_enable"})
        await socket.send({"type": "heartbeat"})
        assert (await socket.receive())["type"] == "heartbeat_ack"
        conn = manager.connections[selection.device_id]
        original_end = conn.row["expires_at"]
        now = original_end - 3
        task = await bind(manager, owner, selection)
        pending = asyncio.create_task(
            manager._call(conn, owner, "local_write_file", {}, task=task, timeout=20)
        )
        call = await socket.receive()
        assert call["expires_at"] > original_end and task.expires_at > original_end
        ticket, _ = await manager.renewal_ticket(
            selection.device_id, owner, "session-alice", now + 10000
        )
        request = {"type": "renew", "ticket": ticket, "candidate": _digest("new-device-secret")}
        await socket.send(request)
        ack = await socket.receive()
        await socket.send(request)
        assert ack == await socket.receive() and ack["expires_at"] > original_end
        now = original_end + 1
        await socket.send(
            {
                "type": "result",
                "call_id": call["call_id"],
                "result": {"kind": "local_operation", "status": "completed", "path": "report.docx"},
            }
        )
        assert (await pending)["status"] == "completed"
        assert manager.connections[selection.device_id] is conn and not conn.pending
        assert task is manager.tasks[task.token]
    finally:
        await finish(runner)


@pytest.mark.parametrize(
    "driver",
    ["sqlite", pytest.param("postgresql", marks=[requires_postgresql, pytest.mark.postgresql])],
)
def test_atomic_renewal_expiry_and_revoke(driver, tmp_path):
    pool = (
        PostgresPool(os.environ["MOSSWORK_TEST_DATABASE_URL"])
        if driver == "postgresql"
        else SqlitePool(tmp_path / "leases.db")
    )
    try:
        run_migrations(pool)
        device = secrets.token_hex(16)
        owner = UserRepo(pool).create(username="lease-" + device, role="user")
        repo = DeviceRepo(pool)
        sid = "login-" + device
        now = int(time.time())
        initial, candidate, ticket = (_digest(secrets.token_hex()) for _ in range(3))
        repo.issue_code(device, owner, sid, now + 20, now + 30)
        assert repo.pair(device, device, initial, "test", "windows", 2, now)
        assert not repo.issue_renewal(device, owner, sid + "other", ticket, now + 20, now + 80, now)
        assert repo.issue_renewal(device, owner, sid, ticket, now + 20, now + 80, now)
        assert repo.rotate(ticket, "wrong", candidate, now) is None
        assert repo.rotate(ticket, initial, candidate, now, "windows") is None
        changed = repo.rotate(ticket, initial, candidate, now)
        assert changed and changed["expires_at"] == now + 80
        assert repo.rotate(ticket, initial, candidate, now) == changed
        assert repo.rotate(ticket, initial, initial, now) is None
        assert repo.authenticate(initial, now) is None
        assert repo.authenticate(initial, now, handover=True)
        assert repo.authenticate(initial, now + 30, handover=True) is None
        now += 81
        assert repo.authenticate(candidate, now) is None
        assert repo.issue_renewal(device, owner, sid, ticket, now + 60, now + 100, now)
        assert repo.rotate(ticket, candidate, initial, now) is None
        assert repo.rotate(ticket, candidate, initial, now, "darwin") is None
        assert repo.rotate(ticket, candidate, initial, now, "windows")
        assert repo.issue_renewal(device, owner, sid, ticket, now + 60, now + 200, now)
        barrier = threading.Barrier(2)

        def revoke():
            barrier.wait(5)
            return repo.revoke_session(sid, now)

        with ThreadPoolExecutor(max_workers=1) as executor:
            revocation = executor.submit(revoke)
            barrier.wait(5)
            repo.rotate(ticket, initial, candidate, now)
            assert revocation.result(timeout=5) == [device]
        assert repo.rotate(ticket, initial, candidate, now) is None
        assert repo.authenticate(initial, now, handover=True) is None
        assert repo.authenticate(candidate, now, handover=True) is None
    finally:
        pool.close()
