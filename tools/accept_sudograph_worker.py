"""Qualify Mosswork's MCP client against an installed, pinned Sudograph worker.

This developer acceptance creates its own SQLite source, canonical model and
Mosswork connector database. It does not register production credentials or
qualify gateway authorization, agent management, A2A, or frontend integration.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import queue
import secrets
import sqlite3
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

# Match the real application's initialization, including its existing handling
# of Windows/macOS system proxies for loopback MCP servers.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import httpx  # noqa: E402
import yaml  # noqa: E402
from moss_harness.mcp import (  # noqa: E402
    aload_mcp_tools,
    filter_tools_for_mcp_servers,
    mcp_tool_names,
)

import octop  # noqa: E402, F401
from octop.config import MossworkConfig  # noqa: E402
from octop.infra.connectors.custom_mcp import CUSTOM_MCP_KIND  # noqa: E402
from octop.infra.connectors.service import ConnectorService  # noqa: E402
from octop.infra.db.migrate import run_migrations  # noqa: E402
from octop.infra.db.pool import SqlitePool  # noqa: E402
from octop.infra.db.repos.connectors import ConnectorRepo  # noqa: E402
from octop.infra.db.repos.secrets import SecretRepo  # noqa: E402
from octop.infra.db.repos.settings import SettingsRepo  # noqa: E402
from octop.infra.db.repos.users import UserRepo  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def decode_content(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        result = json.loads(value)
    else:
        assert isinstance(value, list), "Require MCP text content"
        texts = [
            item["text"] for item in value if isinstance(item, dict) and item.get("type") == "text"
        ]
        assert len(texts) == 1, "Require one exact worker response"
        result = json.loads(texts[0])
    assert isinstance(result, dict), "Require a structured worker result"
    return result


class Acceptance:
    def __init__(self, output: Path, compiler: Path, worker: Path) -> None:
        self.output, self.compiler, self.worker = output, compiler, worker
        self.source = output / "source.db"
        self.token = secrets.token_urlsafe(48)
        self.events: list[dict[str, Any]] = []

    def record(self, name: str, **evidence: Any) -> None:
        self.events.append({"name": name, "passed": True, **evidence})
        write_json(self.output / "checks.json", self.events)
        print(json.dumps({"check": name, "passed": True}), flush=True)

    def register_source(self) -> None:
        with sqlite3.connect(self.source) as connection:
            connection.execute("CREATE TABLE widgets(id INTEGER PRIMARY KEY, label TEXT)")
            connection.executemany(
                "INSERT INTO widgets VALUES(?,?)",
                [(1, "row-only-sentinel"), (2, None), (3, "third")],
            )
        request = {
            "source": "sqlite:///" + self.source.as_posix(),
            "namespace": "mosswork-interop",
            "operation_id": str(uuid.uuid4()),
            "language": "en",
            "snapshot_records": False,
        }
        registration = subprocess.run(
            [
                str(self.compiler),
                "-m",
                "compiler.registration",
                "--store",
                str(self.output / "fixture.db"),
                "--actor",
                "fixture-owner",
            ],
            input=json.dumps(request),
            cwd=self.output,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        assert registration.returncode == 0, "Installed compiler registration failed"
        result = json.loads(registration.stdout)
        write_json(self.output / "registration.json", result)
        assert result["ok"], result
        self.record("installed_compiler_registers_isolated_source")

    async def qualify_registered_worker(self, endpoint: str) -> str:
        pool = SqlitePool(self.output / "mosswork.db")
        try:
            run_migrations(pool)
            users = UserRepo(pool)
            owner = users.create(username="fixture-owner", role="user")
            other = users.create(username="fixture-other", role="user")
            connectors = ConnectorRepo(pool)
            service = ConnectorService(
                repo=connectors,
                secret_repo=SecretRepo(pool),
                settings_repo=SettingsRepo(pool),
                config=MossworkConfig(),
            )
            service.put_custom_servers(
                owner,
                {
                    "model": {
                        "transport": "streamable_http",
                        "url": endpoint,
                        "headers": {"Authorization": "Bearer " + self.token},
                    }
                },
            )
            row = connectors.get_by_user_kind(owner, CUSTOM_MCP_KIND)
            assert row is not None and row.credential_blob
            assert self.token.encode() not in row.credential_blob
            assert self.token not in json.dumps(service.list_instances_for_api(owner))
            self.record("existing_connector_registration_encrypts_worker_credential")

            assert service.validate_mcp_servers_for_user(owner, ["model"]) == ["model"]
            assert service.get_custom_servers_for_api(other) == {}
            assert service.list_instances_for_api(other) == []
            assert await service.mcp_configs_for_user(other) == {}
            try:
                service.validate_mcp_servers_for_user(other, ["model"])
            except ValueError:
                pass
            else:
                raise AssertionError("Another user must not select the private connector")
            self.record("another_user_cannot_discover_or_select_private_connector")

            configs = await service.mcp_configs_for_user(owner)
            assert set(configs) == {"model"}
            revision = await self.qualify(endpoint, configs)
            service.patch_custom_server_enabled(owner, "model", enabled=False)
            assert service.list_active_mcp_server_names(owner) == []
            assert await service.mcp_configs_for_user(owner) == {}
            assert await aload_mcp_tools(await service.mcp_configs_for_user(owner)) == []
            try:
                service.validate_mcp_servers_for_user(owner, ["model"])
            except ValueError:
                pass
            else:
                raise AssertionError("Disabled connectors must not enter a new turn")
            self.record("disabled_connector_refused_for_new_turns")
            return revision
        finally:
            pool.close()

    async def qualify(self, endpoint: str, configs: dict[str, Any]) -> str:
        tools = await aload_mcp_tools(configs)
        by_name = {tool.name.removeprefix("model_"): tool for tool in tools}
        assert set(by_name) == {
            "sudograph_context",
            "sudograph_context_read",
            "sudograph_model_contract",
            "sudograph_model_apply",
        }, "The actual Mosswork loader must discover the complete worker contract"
        self.record("mosswork_discovers_published_worker_tools", tools=sorted(by_name))

        names = mcp_tool_names(tools)
        selection = {"mcp_tool_names": names, "server_names": frozenset({"model"})}
        assert not filter_tools_for_mcp_servers(tools, **selection, active_servers=None)
        assert len(filter_tools_for_mcp_servers(tools, **selection, active_servers=["model"])) == 4
        self.record("existing_per_request_tool_visibility")

        async def invoke(name: str, arguments: dict[str, Any]) -> Any:
            tool = by_name[name]
            # This is the agent-facing API. Bare ainvoke(args) deliberately
            # returns handled error content without ToolMessage.status.
            return await tool.ainvoke(
                {
                    "type": "tool_call",
                    "name": tool.name,
                    "args": arguments,
                    "id": str(uuid.uuid4()),
                }
            )

        async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            message = await invoke(name, arguments)
            assert message.status == "success", message.content
            return decode_content(message.content)

        packet = await call("sudograph_context", {})
        assert packet["format"] == "sudograph-worker-context/v1"
        assert packet["model_ref"] == "fixture" and packet["authority_ref"] == "fixture-authority"
        context = packet["context"]
        assert "row-only-sentinel" not in context["text"], "Records must remain on-demand reads"
        pointer = {"revision": context["revision"], "context_digest": context["context_digest"]}
        catalogue = await call("sudograph_context_read", {**pointer, "id": "catalog"})
        objects = [obj for source in catalogue["sources"] for obj in source["objects"]]
        target = next(obj for obj in objects if obj["name"] == "widgets")
        rows = await call(
            "sudograph_context_read", {**pointer, "id": target["id"], "records": True, "limit": 2}
        )
        assert rows["rows"] == [[1, "row-only-sentinel"], [2, None]]
        self.record(
            "exact_context_and_real_record_reads",
            revision=context["revision"],
            source_object=target["id"],
        )

        contract = await call("sudograph_model_contract", {})
        proposed = copy.deepcopy(contract["write"]["document_schema"]["examples"][0])
        request = {
            "base_revision": contract["revision"],
            "operation_id": str(uuid.uuid4()),
            "model": yaml.safe_dump(proposed, sort_keys=False),
        }
        committed = await call("sudograph_model_apply", request)
        assert committed["revision"] != contract["revision"]
        assert await call("sudograph_model_apply", request) == committed
        current = (await call("sudograph_context", {}))["context"]
        assert current["revision"] == committed["revision"]
        self.record("compiler_contract_example_updates_context", revision=current["revision"])
        self.record("same_operation_retry_is_idempotent")

        invalid = copy.deepcopy(proposed)
        definition = next(iter(invalid["definitions"].values()))
        definition["targets"] = ["sg-source/unknown/object/missing/field/missing"]
        definition["version"] += 1
        refused = await invoke(
            "sudograph_model_apply",
            {
                "base_revision": current["revision"],
                "operation_id": str(uuid.uuid4()),
                "model": yaml.safe_dump(invalid, sort_keys=False),
            },
        )
        assert refused.status == "error", "Invalid edits must be failed agent-visible tool results"
        diagnostic = decode_content(refused.content)
        assert diagnostic["code"] and diagnostic["recovery"]
        assert self.token not in json.dumps(diagnostic) and str(self.output) not in json.dumps(
            diagnostic
        )
        assert (await call("sudograph_model_contract", {}))["revision"] == committed["revision"]
        self.record("invalid_reference_refused_without_commit", diagnostic=diagnostic)

        stale = await invoke(
            "sudograph_context_read",
            {
                "revision": current["revision"],
                "context_digest": context["context_digest"],
                "id": "catalog",
            },
        )
        assert stale.status == "error" and decode_content(stale.content)["code"] == "CONTEXT_STALE"
        self.record("mixed_revision_digest_refused")

        async with httpx.AsyncClient(timeout=30) as client:
            refused_request = await client.post(
                endpoint, headers={"Authorization": "Bearer wrong"}, json={}
            )
            assert refused_request.status_code == 401
            self.record("wrong_worker_credential_refused")
            review = await client.post(
                endpoint.removesuffix("/mcp") + "/review",
                headers={"Authorization": "Bearer " + self.token},
                json={"operation": "document", "endpoint": "/review/" + uuid.uuid4().hex + "/v1"},
            )
            assert review.status_code == 200, review.text[:300]
            document = review.json()["result"]
            assert document["revision"] == current["revision"]
            assert current["revision"] in document["html"] and target["id"] in document["html"]
            assert "row-only-sentinel" not in document["html"]
            self.record(
                "review_html_uses_same_revision_and_source_identity", revision=current["revision"]
            )
        return str(current["revision"])

    def run(self) -> None:
        process = None
        try:
            self.register_source()
            source_digest = sha256(self.source)
            config = {
                "binding": {
                    "authorityRef": "fixture-authority",
                    "modelRef": "fixture",
                    "sourceRefs": ["fixture-source"],
                },
                "runtime": {
                    "python": str(self.compiler),
                    "compilerRoot": str(self.output),
                    "modelDirectory": str(self.output),
                },
                "actor": "fixture-owner",
                "host": "127.0.0.1",
                "port": 0,
            }
            config_path = self.output / "worker.json"
            write_json(config_path, config)
            with (self.output / "worker.stderr.log").open("wb") as stderr:
                process = subprocess.Popen(
                    ["node", str(self.worker), str(config_path)],
                    cwd=self.output,
                    env={**os.environ, "SUDOGRAPH_WORKER_TOKEN": self.token},
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                assert process.stdout is not None
                ready: queue.Queue[bytes] = queue.Queue()
                threading.Thread(
                    target=lambda: ready.put(process.stdout.readline()), daemon=True
                ).start()
                reported = json.loads(ready.get(timeout=30))
                assert reported["format"] == "sudograph-worker-ready/v1"
                revision = asyncio.run(
                    self.qualify_registered_worker(f"http://127.0.0.1:{reported['port']}/mcp")
                )
            assert sha256(self.source) == source_digest
            self.record("raw_source_bytes_unchanged", sha256=source_digest)
            write_json(
                self.output / "receipt.json",
                {
                    "format": "mosswork-sudograph-interop/v1",
                    "passed": True,
                    "at": datetime.now(UTC).isoformat(),
                    "worker_sha256": sha256(self.worker),
                    "acceptance_script_sha256": sha256(Path(__file__)),
                    "host_initializer_sha256": sha256(Path(octop.__file__)),
                    "revision": revision,
                    "checks": self.events,
                    "client_packages": {
                        name: version(name)
                        for name in [
                            "moss-harness",
                            "langchain-mcp-adapters",
                            "mcp",
                            "langchain-core",
                        ]
                    },
                    "scope": "Actual host initialization, encrypted private connector registration, per-user connector selection and disabling for new turns, MCP loading/filtering, agent tool results and published compiler/worker against an isolated source/model. No LLM call or workflow prompts.",
                    "limits": "Not acceptance of frontend/comments, SudoCode/Nexus management/A2A, per-user gateway policy, or live-tenant migration. Tool visibility is not authorization. Fixture worker credentials must not replace the existing production authority.",
                },
            )
        except BaseException as error:
            write_json(
                self.output / "incomplete.json",
                {
                    "passed": False,
                    "at": datetime.now(UTC).isoformat(),
                    "checks": self.events,
                    "error": f"{type(error).__name__}: {error}".replace(self.token, "[private]"),
                },
            )
            raise
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--worker", type=Path, required=True, help="Installed sudograph-context-worker.mjs"
    )
    parser.add_argument(
        "--worker-sha256", required=True, help="Expected SHA-256 of the verified installed worker"
    )
    parser.add_argument(
        "--compiler-python",
        type=Path,
        required=True,
        help="Python with the installed Sudograph compiler",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New private acceptance directory; existing paths are refused",
    )
    args = parser.parse_args()
    worker, compiler, output = (
        args.worker.resolve(strict=True),
        args.compiler_python.resolve(strict=True),
        args.output.resolve(),
    )
    if not worker.is_file() or not compiler.is_file() or sha256(worker) != args.worker_sha256:
        parser.error("Require existing compiler/worker files and the exact verified worker SHA-256")
    output.mkdir(parents=True, exist_ok=False)
    Acceptance(output, compiler, worker).run()
    print(json.dumps({"passed": True, "receipt": str(output / "receipt.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
