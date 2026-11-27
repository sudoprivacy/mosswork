import asyncio
import json
import os
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

import pytest

from octop.config import load_config
from tests.support.http import serve_http

from .test_runtime import install_agent


@pytest.mark.parametrize(
    "bundle", os.environ.get("MOSSWORK_TEST_WORKER_BUNDLES", "").split(os.pathsep)
)
async def test_installed_worker_continues_local_task(company, tmp_path, bundle):
    if not bundle:
        pytest.skip("Set MOSSWORK_TEST_WORKER_BUNDLES to installed desktop runtime directories")
    root = Path(bundle).resolve(strict=True)
    manifest = json.loads((root / "company-runtime-manifest.json").read_bytes())
    server, client, credentials, _ = company
    install_agent(server)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "company_runtime_enabled": True,
                "company_runtime_snapshot_profiles": {
                    "*": {"manifest": str(root / "company-runtime-manifest.json")}
                },
            }
        )
    )
    server.services = replace(server.services, config=load_config(config_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    python = root / ("runtime/python.exe" if os.name == "nt" else "runtime/bin/python3")
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.update(
        PYTHONPATH="",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
        MOSSWORK_HOME=str(tmp_path / "home"),
        MOSSWORK_COMPANY_TOOL_BIN=str(root / "tools"),
        MOSSWORK_COMPANY_PACKAGES=str(root / "packages"),
    )
    async with serve_http(client.http._transport.app) as origin:
        print(
            f"\nHTTP {origin} | server moss-harness {version('moss-harness')} | installed worker {manifest['harness_version']}"
        )
        worker = await asyncio.create_subprocess_exec(
            python,
            "-u",
            root / "launch.py",
            "company-runtime",
            env=env,
            cwd=workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=4 * 1024 * 1024,
        )
        assert worker.stdin and worker.stdout

        async def receive(predicate):
            async with asyncio.timeout(90):
                while line := await worker.stdout.readline():
                    message = json.loads(line)
                    if predicate(message):
                        return message
            pytest.fail("Worker exited before returning its receipt")

        async def request(method, params):
            worker.stdin.write(
                json.dumps({"id": method, "method": method, "params": params}).encode() + b"\n"
            )
            await worker.stdin.drain()
            reply = await receive(lambda message: message.get("id") == method)
            assert "result" in reply, reply.get("error")
            return reply["result"]

        try:
            await request(
                "initialize",
                {
                    "workspace": str(workspace),
                    "state_dir": str(tmp_path / "state"),
                    "company_url": origin,
                    "credential": credentials[0],
                    "execute_authorized": True,
                },
            )
            for task_id in ("first", "followup"):
                await request(
                    "start",
                    {
                        "task_id": task_id,
                        "thread_id": "thread1",
                        "agent_id": "expert",
                        "text": "Write report.txt" if task_id == "first" else "What did you write?",
                    },
                )
                done = await receive(lambda event: event.get("event") == "done")
                row = server.services.company_runtime_repo.task(task_id)
                print(
                    f"{task_id}: worker={done['status']} | server receipt={row['status']} | snapshot={json.loads(row['expert_json'])['harness_version']}"
                )
                if done.get("error"):
                    print(done["error"])
                assert done["status"] == "completed", done.get("error")
                assert server.services.company_runtime_repo.task(task_id)["status"] == "completed"
                assert (workspace / "thread1" / "report.txt").read_text() == "local report 42"
                server.services = replace(
                    server.services,
                    config=replace(server.services.config, company_runtime_snapshot_profiles={}),
                )
            turns = server.services.company_runtime_repo.artifact_turns("thread1")
            assert [turn["task_id"] for turn in turns] == ["first"]
        finally:
            worker.stdin.close()
            try:
                await asyncio.wait_for(worker.wait(), 10)
            except TimeoutError:
                worker.kill()
                await worker.wait()
            await client.close()
