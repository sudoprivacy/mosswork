import asyncio
import json
import os
import shutil
import socket
import time
import zipfile
from contextlib import suppress
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from octop.api.middleware.jwt_auth import install
from octop.api.routers import devices
from octop.infra.devices.protocol import DeviceSelection
from tests.unit.devices.test_file_tasks import bind


@pytest.mark.skipif(
    os.environ.get("MOSSWORK_TEST_NATIVE_LEASE") != "1",
    reason="opt-in native Go toolchain and loopback server",
)
async def test_native_word_task_survives_real_server_renewal(stack, tmp_path, monkeypatch):
    server, owner, _, tokens = stack
    manager = server.devices
    root = Path(__file__).resolve().parents[3]
    binary = tmp_path / ("leaseclient.exe" if os.name == "nt" else "leaseclient")
    build = await asyncio.create_subprocess_exec(
        shutil.which("go") or "go",
        "build",
        "-o",
        str(binary),
        "./internal/company/testdata/leaseclient",
        cwd=root / "desktop" / "src",
    )
    assert await build.wait() == 0
    app = FastAPI()
    app.state.mosswork_server = server
    app.include_router(devices.router, prefix="/api")
    install(app, server)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    web = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error", lifespan="off"))
    serving = asyncio.create_task(web.serve(sockets=[listener]))
    work = tmp_path / "work"
    work.mkdir()
    native = await asyncio.create_subprocess_exec(
        str(binary),
        origin,
        str(work),
        str(tmp_path / "journal"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )

    async def command(method, ticket=""):
        native.stdin.write(json.dumps({"method": method, "ticket": ticket}).encode() + b"\n")
        await native.stdin.drain()
        return json.loads(await asyncio.wait_for(native.stdout.readline(), 10))

    writing = None
    try:
        async with asyncio.timeout(20):
            while not web.started:
                await asyncio.sleep(0.01)
            cutoff = int(time.time()) + 4
            pairing = await manager.issue_pairing(owner, "session-alice", cutoff)
            status = await command("pair", pairing["code"])
            device = status["device_id"]
            conn = manager.connections[device]
            while not conn.grants:
                await asyncio.sleep(0.01)
            grant = conn.grants[0]
            task = await bind(
                manager,
                owner,
                DeviceSelection(
                    mode="local", device_id=device, grant_id=grant.id, grant_version=grant.version
                ),
            )
            dispatched, release = asyncio.Event(), asyncio.Event()
            original_send = conn.send

            async def send(frame):
                if frame["type"] == "call" and frame["tool"] == "local_write_file":
                    assert frame["expires_at"] > cutoff
                    dispatched.set()
                    await release.wait()
                await original_send(frame)

            monkeypatch.setattr(conn, "send", send)
            writing = asyncio.create_task(
                manager.execute(
                    task,
                    "local_write_file",
                    {"path": "report.docx", "content": "native lease acceptance"},
                )
            )
            await dispatched.wait()
            async with httpx.AsyncClient(base_url=origin, trust_env=False) as api:
                reply = await api.post(
                    f"/api/devices/{device}/renewal-ticket",
                    headers={"Authorization": "Bearer " + tokens["alice"]},
                )
                reply.raise_for_status()
                issued = reply.json()
            await command("renew", issued["ticket"] + "." + issued["key_hint"])
            while (await command("status"))["expires_at"] <= cutoff:
                await asyncio.sleep(0.01)
            await asyncio.sleep(max(0, cutoff - time.time()) + 0.05)
            release.set()
            assert (await writing)["status"] == "completed"
            assert manager.connections[device] is conn and manager.tasks[task.token] is task
            assert (await manager.probe(owner, device))["status"] == "completed"
            assert list(work.iterdir()) == [work / "report.docx"]
            with zipfile.ZipFile(work / "report.docx") as doc:
                assert b"native lease acceptance" in doc.read("word/document.xml")
    finally:
        if writing is not None and not writing.done():
            writing.cancel()
            with suppress(asyncio.CancelledError):
                await writing
        native.stdin.close()
        if native.returncode is None:
            try:
                await asyncio.wait_for(native.wait(), 3)
            except TimeoutError:
                native.kill()
                await native.wait()
        web.should_exit = True
        await asyncio.wait_for(serving, 8)
        listener.close()
