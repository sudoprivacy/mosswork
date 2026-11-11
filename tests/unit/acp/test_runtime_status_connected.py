"""Exercise the production ACP service against a selected real native engine."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from moss_harness.acp.models import ACPConfig, ACPRunnerConfig
from moss_harness.acp.service import ACPService
from tests.support.acp_status_gate import gate_prefix

pytestmark = pytest.mark.skipif(
    os.environ.get("MOSSWORK_ACP_RUNTIME_E2E") != "1",
    reason="Set MOSSWORK_ACP_RUNTIME_E2E=1 and SCODE_BIN for the native ACP contract test",
)


@pytest.fixture
async def model_endpoint() -> AsyncIterator[tuple[str, list[tuple[bytes, bytes]]]]:
    """Serve catalog discovery and refuse every other provider request."""
    requests: list[tuple[bytes, bytes]] = []

    async def refuse(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with asyncio.timeout(10):
            method, path, _version = (await reader.readline()).split()
            while await reader.readline() not in (b"\r\n", b"\n", b""):
                pass
        requests.append((method, path))
        is_catalog = (method, path) == (b"GET", b"/v1/models")
        payload = (
            json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "claude-sonnet-4-6",
                            "object": "model",
                            "created": 0,
                            "owned_by": "acceptance",
                        }
                    ],
                }
            ).encode()
            if is_catalog
            else b'{"error":"inference is forbidden in this test"}'
        )
        status = b"200 OK" if is_catalog else b"503 Service Unavailable"
        writer.write(
            b"HTTP/1.1 "
            + status
            + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(payload)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + payload
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(refuse, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}", requests


async def wait_for_file(path: Path, *, larger_than: int = 0) -> None:
    async with asyncio.timeout(30):
        while not path.exists() or path.stat().st_size <= larger_than:
            await asyncio.sleep(0.05)


async def test_actual_connections_remain_distinct_and_queryable_during_quiet_work(
    tmp_path: Path,
    model_endpoint: tuple[str, list[tuple[bytes, bytes]]],
) -> None:
    binary = os.environ.get("SCODE_BIN", "")
    assert Path(binary).is_file(), "Native acceptance requires an existing SCODE_BIN"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gates = workspace / "gates"
    gates.mkdir()
    home = tmp_path / "home"
    config_home = home / ".nexus" / "sudocode"
    config_home.mkdir(parents=True)
    (config_home / "sudocode.json").write_text(
        json.dumps(
            {
                "auth_modes": {
                    "proxy": {
                        "acceptance": {
                            "apiKey": "protocol-only-no-network",
                            "baseUrl": model_endpoint[0],
                        }
                    }
                },
                "models": {
                    "claude-sonnet-4-6": {
                        "name": "claude-sonnet-4-6",
                        "providers": {"proxy": {"model": "claude-sonnet-4-6"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    hook_script = workspace / "hold_hook.py"
    hook_script.write_text(
        "from pathlib import Path\n"
        "import time\n"
        "progress = Path('hook-progress')\n"
        "progress.write_text('x')\n"
        "Path('hook-started').write_text('started')\n"
        "deadline = time.monotonic() + 45\n"
        "while not Path('hook-release').exists():\n"
        "    if time.monotonic() >= deadline:\n"
        "        raise RuntimeError('Test did not release its real hook')\n"
        "    with progress.open('a') as stream:\n"
        "        stream.write('x')\n"
        "    time.sleep(0.05)\n"
        "Path('hook-finished').write_text('finished')\n",
        encoding="utf-8",
    )
    command = [sys.executable, str(hook_script)]
    hook = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
    (config_home / "settings.json").write_text(
        json.dumps(
            {
                "agentName": "moss-runtime-fixture",
                "auth_profile": "acceptance",
                "model": "claude-sonnet-4-6",
                "hooks": {"PostToolUse": [hook]},
            }
        ),
        encoding="utf-8",
    )
    config = ACPConfig(
        runners={
            "native-fixture": ACPRunnerConfig(
                enabled=True,
                trusted=True,
                command=sys.executable,
                args=[
                    # A real launcher keeps the transport PID different from
                    # the forwarder's PID on every platform, including POSIX.
                    "-c",
                    "import subprocess\nimport sys\nraise SystemExit(subprocess.call(sys.argv[1:]))\n",
                    sys.executable,
                    str(Path(__file__).resolve().parents[2] / "support" / "acp_status_gate.py"),
                    str(Path(binary).resolve()),
                    "--auth",
                    "proxy",
                    "--model",
                    "claude-sonnet-4-6",
                    "--permission-mode",
                    "danger-full-access",
                    "acp",
                ],
                env={
                    "HOME": str(home),
                    "USERPROFILE": str(home),
                    "SUDO_CODE_CONFIG_HOME": str(config_home),
                    "SCODE_GLOBAL_CONFIG_DIR": str(config_home),
                    "ANTHROPIC_API_KEY": "",
                    "OPENAI_API_KEY": "",
                    "SUDOROUTER_API_KEY": "",
                    "PROXY_AUTH_TOKEN": "",
                    "PROXY_BASE_URL": "",
                    "NEXUS_AGENT_ID": "",
                    "NEXUS_GRPC_ENDPOINT": "",
                    "NO_COLOR": "1",
                    "MOSSWORK_ACP_TEST_GATE_DIR": str(gates),
                },
            )
        }
    )
    service = ACPService(config=config)
    pending: asyncio.Task[Any] | None = None

    async def observation(thread_id: str) -> Any:
        return await service.get_runtime_observation(thread_id=thread_id, runner="native-fixture")

    async def connect(thread_id: str) -> Any:
        return await service._get_or_create_session(
            thread_id=thread_id, runner="native-fixture", cwd=str(workspace), require_existing=False
        )

    try:
        async with asyncio.timeout(90):
            first = await connect("first")
            await connect("second")
            initial = await observation("first")
            other = await observation("second")
            assert initial.availability == other.availability == "fresh"
            assert initial.snapshot.foreground.state == other.snapshot.foreground.state == "idle"
            assert initial.instance_id != other.instance_id
            assert initial.session_id != other.session_id
            assert initial.is_complete is False

            pending = asyncio.create_task(
                first.conn.ext_method(
                    "sudocode/a2a/send",
                    {
                        "sessionId": first.acp_session_id,
                        "to": "fixture-recipient",
                        "message": "MOSS_RUNTIME_ACTUAL_DELIVERY",
                        "summary": "runtime acceptance",
                    },
                )
            )
            await wait_for_file(workspace / "hook-started")
            progress = (workspace / "hook-progress").stat().st_size
            working, still_idle = await asyncio.gather(observation("first"), observation("second"))
            assert working.availability == still_idle.availability == "fresh"
            assert working.snapshot.foreground.state == "running"
            assert still_idle.snapshot.foreground.state == "idle"
            assert working.instance_id == initial.instance_id
            await wait_for_file(workspace / "hook-progress", larger_than=progress)

            # Hold an actual engine reply after it observed running. Readers
            # must leave at their budget, without queuing another wire request
            # or cancelling the independently progressing native operation.
            gate = gate_prefix(gates, first.acp_session_id)
            counter = gate.with_suffix(".requests")
            original_requests = counter.stat().st_size
            gate.with_suffix(".hold").write_text("hold", encoding="utf-8")
            timed_out = await observation("first")
            assert timed_out.availability == "unavailable"
            assert timed_out.snapshot is None
            await wait_for_file(gate.with_suffix(".held-response"))
            again = await observation("first")
            assert again.availability == "unavailable"
            assert again.snapshot is None
            assert counter.stat().st_size == original_requests + 1

            (workspace / "hook-release").write_text("release", encoding="utf-8")
            await wait_for_file(workspace / "hook-finished")
            gate.with_suffix(".release").write_text("release", encoding="utf-8")
            assert first.runtime_status_task is not None
            stale = await first.runtime_status_task
            assert stale["foreground"]["state"] == "running"
            result = await pending
            assert result["isError"] is False
            receipt = json.loads(result["content"])
            assert "MOSS_RUNTIME_ACTUAL_DELIVERY" in Path(
                receipt["routing"]["mailbox_path"]
            ).read_text(encoding="utf-8")
            assert (workspace / "hook-finished").read_text(encoding="utf-8") == "finished"
            assert (await observation("first")).snapshot.foreground.state == "idle"

            await service.close_thread_session(thread_id="first", runner="native-fixture")
            disconnected = await observation("first")
            assert disconnected.availability == "disconnected"
            assert disconnected.snapshot is None
            assert (await observation("second")).instance_id == other.instance_id
            await connect("first")
            reopened = await observation("first")
            assert reopened.availability == "fresh"
            assert reopened.snapshot.foreground.state == "idle"
            assert reopened.instance_id != initial.instance_id
    finally:
        (workspace / "hook-release").write_text("cleanup", encoding="utf-8")
        for held_reply in gates.glob("*.hold"):
            held_reply.with_suffix(".release").write_text("cleanup", encoding="utf-8")
        try:
            if pending is not None:
                await asyncio.wait_for(pending, timeout=10)
        finally:
            await service.close_all_sessions()
    assert all(request == (b"GET", b"/v1/models") for request in model_endpoint[1]), (
        "Only model catalog discovery is allowed; no inference or other provider request may run"
    )
