"""New domain client against installed artifacts; no model or production grants.

The CI assembly supplies verified artifact paths. A normal checkout without
those artifacts skips this explicit assembly test, rather than fetching tools.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import queue
import secrets
import subprocess
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from moss_harness.mcp import aload_mcp_tools

from octop.infra.connectors.fixed_json import FixedJsonTransport
from octop.infra.connectors.tool_protocol import ToolConnection, open_tool_protocol
from octop.infra.sudograph.client import Operation, SudographClient
from octop.infra.sudograph.protocol import (
    ModelBinding,
    ModelPointer,
    ReviewDocument,
    SudographError,
    ToolResult,
    context,
    envelope,
)


@dataclass(frozen=True)
class InstalledWorker:
    endpoint: str
    token: str = field(repr=False)
    output: Path


@pytest.fixture
def installed_worker(tmp_path: Path) -> Iterator[InstalledWorker]:
    worker_path = os.environ.get("MOSSWORK_TEST_SUDOGRAPH_WORKER")
    compiler_path = os.environ.get("MOSSWORK_TEST_SUDOGRAPH_COMPILER")
    if not worker_path and not compiler_path:
        pytest.skip("Explicit installed compiler/worker assembly artifacts not supplied")
    assert worker_path and compiler_path, "Supply both installed artifact paths"
    helper_path = Path(__file__).parents[2] / "tools" / "accept_sudograph_worker.py"
    spec = importlib.util.spec_from_file_location("sudograph_assembly_fixture", helper_path)
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    worker, compiler = Path(worker_path), Path(compiler_path)
    assert helper.sha256(worker) == os.environ["MOSSWORK_TEST_SUDOGRAPH_WORKER_SHA256"]
    helper.Acceptance(tmp_path, compiler, worker).register_source()
    source_digest = helper.sha256(tmp_path / "source.db")
    config_path = tmp_path / "worker.json"
    config_path.write_text(
        json.dumps(
            {
                "binding": {
                    "authorityRef": "fixture-authority",
                    "modelRef": "fixture",
                    "sourceRefs": ["fixture-source"],
                },
                "runtime": {
                    "python": str(compiler),
                    "compilerRoot": str(tmp_path),
                    "modelDirectory": str(tmp_path),
                },
                "actor": "fixture-owner",
                "host": "127.0.0.1",
                "port": 0,
            }
        ),
        encoding="utf-8",
    )
    token = secrets.token_urlsafe(48)
    with (tmp_path / "worker.stderr.log").open("wb") as stderr:
        process = subprocess.Popen(
            [os.environ.get("MOSSWORK_TEST_SUDOGRAPH_NODE", "node"), str(worker), str(config_path)],
            cwd=tmp_path,
            env={**os.environ, "SUDOGRAPH_WORKER_TOKEN": token},
            stdout=subprocess.PIPE,
            stderr=stderr,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            assert process.stdout
            ready: queue.Queue[bytes] = queue.Queue()
            threading.Thread(
                target=lambda: ready.put(process.stdout.readline()), daemon=True
            ).start()
            reported = json.loads(ready.get(timeout=30))
            assert reported["format"] == "sudograph-worker-ready/v1"
            yield InstalledWorker(f"http://127.0.0.1:{reported['port']}", token, tmp_path)
            assert helper.sha256(tmp_path / "source.db") == source_digest
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def test_fixed_json_protocol_with_real_installed_worker(installed_worker: InstalledWorker) -> None:
    """New transport against installed producer bytes, with explicit fixture grants."""

    async def exercise() -> None:
        assertions = 0

        async def fixture_current() -> None:
            nonlocal assertions
            assertions += 1

        connection = ToolConnection(
            installed_worker.endpoint + "/mcp",
            (("Authorization", "Bearer " + installed_worker.token),),
            timeout_ms=15000,
        )
        binding = ModelBinding(
            "fixture-authority", "fixture", "fixture-review", ("fixture-source",)
        )
        async with open_tool_protocol(connection, check_current=fixture_current) as protocol:
            descriptors = {d.tool["name"]: d for d in await protocol.descriptors()}
            assert set(descriptors) == {
                "sudograph_context",
                "sudograph_context_read",
                "sudograph_model_contract",
                "sudograph_model_apply",
            }
            result = await protocol.call_reviewed(descriptors["sudograph_context"], {})
            packet = ToolResult(result["content"], is_error=result.get("isError", False)).decode()
            initial = context(envelope(packet, binding, review=False)["context"])
            assert "row-only-sentinel" not in initial["text"]
            result = await protocol.call_reviewed(
                descriptors["sudograph_context_read"],
                {
                    "revision": initial["revision"],
                    "context_digest": initial["context_digest"],
                    "id": "catalog",
                },
            )
            catalogue = ToolResult(
                result["content"], is_error=result.get("isError", False)
            ).decode()
            assert any(
                obj["name"] == "widgets"
                for source in catalogue["sources"]
                for obj in source["objects"]
            )
            # Exact-read results are compiler products, not review envelopes.
            # Prove the supplied pointer is enforced with the original error.
            wrong_digest = ("0" if initial["context_digest"][0] != "0" else "1") + initial[
                "context_digest"
            ][1:]
            stale_result = await protocol.call_reviewed(
                descriptors["sudograph_context_read"],
                {
                    "revision": initial["revision"],
                    "context_digest": wrong_digest,
                    "id": "catalog",
                },
            )
            with pytest.raises(SudographError) as stale:
                ToolResult(
                    stale_result["content"], is_error=stale_result.get("isError", False)
                ).decode()
            assert stale.value.code == "CONTEXT_STALE"
        async with httpx.AsyncClient(
            transport=FixedJsonTransport(
                installed_worker.endpoint + "/review",
                profile="review",
                before_dispatch=fixture_current,
            ),
            headers=dict(connection.headers),
            trust_env=False,
            follow_redirects=False,
        ) as http:
            response = await http.post(
                installed_worker.endpoint + "/review",
                json={
                    "resource": binding.resource_ref,
                    "operation": "document",
                    "endpoint": "/review/" + uuid.uuid4().hex + "/v1",
                },
            )
            response.raise_for_status()
            document = envelope(response.json(), binding, review=True)["result"]
            assert document["revision"] == initial["revision"]
            assert document["context_digest"] == initial["context_digest"]
            assert "<html" in document["html"].lower()
        (installed_worker.output / "fixed-json-client-receipt.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "worker_sha256": os.environ["MOSSWORK_TEST_SUDOGRAPH_WORKER_SHA256"],
                    "protocol": protocol.version,
                    "descriptor_digests": {name: d.digest for name, d in descriptors.items()},
                    "revision": initial["revision"],
                    "context_digest": initial["context_digest"],
                    "current_assertions": assertions,
                    "scope": "Installed worker/compiler via bounded official MCP and review HTTP; synthetic source and fixture assertions, not production grants/native execution/comments/model acceptance.",
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    asyncio.run(exercise())


def test_domain_client_with_real_installed_compiler_and_worker(
    installed_worker: InstalledWorker,
) -> None:
    endpoint, token, output = (
        installed_worker.endpoint,
        installed_worker.token,
        installed_worker.output,
    )

    async def exercise() -> None:
        loaded = await aload_mcp_tools(
            {
                "model": {
                    "transport": "streamable_http",
                    "url": endpoint + "/mcp",
                    "headers": {"Authorization": "Bearer " + token},
                }
            }
        )
        tools = {tool.name.removeprefix("model_"): tool for tool in loaded}
        binding = ModelBinding(
            "fixture-authority", "fixture", "fixture-review", ("fixture-source",)
        )
        async with httpx.AsyncClient(timeout=30) as http:

            class FixturePorts:
                enabled = True
                audit_events: list[tuple[str, str]] = []

                async def authorize(self, current: ModelBinding, operation: Operation) -> None:
                    if current != binding or not self.enabled:
                        raise PermissionError("Fixture caller has no current grant")

                async def call_tool(
                    self, current: ModelBinding, name: Operation, arguments: dict[str, Any]
                ) -> ToolResult:
                    message = await tools[name].ainvoke(
                        {
                            "type": "tool_call",
                            "name": tools[name].name,
                            "args": arguments,
                            "id": uuid.uuid4().hex,
                        }
                    )
                    return ToolResult(message.content, is_error=message.status == "error")

                async def read_review(
                    self, current: ModelBinding, request: dict[str, Any]
                ) -> dict[str, Any]:
                    response = await http.post(
                        endpoint + "/review",
                        headers={"Authorization": "Bearer " + token},
                        json=request,
                    )
                    response.raise_for_status()
                    return response.json()

                async def audit(
                    self, current: ModelBinding, operation: Operation, status: str
                ) -> None:
                    self.audit_events.append((operation, status))

            ports = FixturePorts()
            client = SudographClient(binding, ports)
            initial = await client.read_context()
            served = await client.review_document("/review/" + uuid.uuid4().hex + "/v1")
            (output / "served-review.json").write_text(
                json.dumps(served.record()), encoding="utf-8"
            )
            (output / "served-review.html").write_bytes(served.html.encode("utf-8"))
            assert served.pointer == ModelPointer(initial["revision"], initial["context_digest"])
            catalogue = await client.read(
                {
                    "revision": initial["revision"],
                    "context_digest": initial["context_digest"],
                    "id": "catalog",
                }
            )
            target = next(
                obj
                for source in catalogue["sources"]
                for obj in source["objects"]
                if obj["name"] == "widgets"
            )
            rows = await client.read(
                {
                    "revision": initial["revision"],
                    "context_digest": initial["context_digest"],
                    "id": target["id"],
                    "records": True,
                    "limit": 2,
                }
            )
            assert rows["rows"] == [[1, "row-only-sentinel"], [2, None]]
            contract = await client.contract()
            proposed = copy.deepcopy(contract["write"]["document_schema"]["examples"][0])
            change = {
                "base_revision": contract["revision"],
                "operation_id": uuid.uuid4().hex,
                "model": yaml.safe_dump(proposed, sort_keys=False),
            }
            committed = await client.apply(change)
            assert await client.apply(change) == committed
            current = await client.read_context()
            assert current["revision"] == committed["revision"] != initial["revision"]
            assert await client.host_context() == current["text"]
            restored = ReviewDocument.restore(
                binding,
                json.loads((output / "served-review.json").read_text(encoding="utf-8")),
                (output / "served-review.html").read_bytes().decode("utf-8"),
            )
            historical = await client.resolve_review_target(restored.pointer, target["id"])
            assert historical["revision"] == initial["revision"]
            assert historical["target"]["id"] == target["id"]
            field = target["fields"][0]
            resolved_field = await client.resolve_review_target(
                ModelPointer(current["revision"], current["context_digest"]), field["id"]
            )
            assert resolved_field["target"]["id"] == field["id"]
            assert "rows" not in historical["target"] and "rows" not in resolved_field["target"]
            current_catalogue = await client.read(
                {
                    "revision": current["revision"],
                    "context_digest": current["context_digest"],
                    "id": "catalog",
                }
            )
            definition_id = next(iter(current_catalogue["definitions"]))
            resolved_definition = await client.resolve_review_target(
                ModelPointer(current["revision"], current["context_digest"]), definition_id
            )
            assert resolved_definition["target"]["definition"]["id"] == definition_id
            with pytest.raises(SudographError) as stale:
                await client.resolve_review_target(
                    ModelPointer(initial["revision"], current["context_digest"]), target["id"]
                )
            assert stale.value.code == "CONTEXT_STALE"
            document = await client.review(
                {"operation": "document", "endpoint": "/review/" + uuid.uuid4().hex + "/v1"}
            )
            assert document["resource_ref"] == binding.resource_ref
            assert document["result"]["revision"] == current["revision"]
            assert target["id"] in document["result"]["html"]
            assert "row-only-sentinel" not in document["result"]["html"]
            invalid = copy.deepcopy(proposed)
            definition = next(iter(invalid["definitions"].values()))
            definition["targets"] = ["sg-source/unknown/object/missing/field/missing"]
            definition["version"] += 1
            with pytest.raises(SudographError) as refused:
                await client.apply(
                    {
                        "base_revision": current["revision"],
                        "operation_id": uuid.uuid4().hex,
                        "model": yaml.safe_dump(invalid),
                    }
                )
            assert refused.value.code == "COMPILER_REJECTED" and refused.value.recovery
            assert (await client.contract())["revision"] == current["revision"]
            ports.enabled = False
            with pytest.raises(SudographError) as denied:
                await client.read_context()
            assert denied.value.code == "ACCESS_DENIED"
            (output / "domain-client-receipt.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "revision": current["revision"],
                        "checks": [
                            "context_and_live_records",
                            "compiler_apply_and_same_operation_retry",
                            "review_same_identity_revision",
                            "compiler_repair_diagnostic_and_unchanged_head",
                            "fixture_grant_revocation",
                            "host_context_v1_live_text",
                            "exact_historical_object_current_field_and_business_targets",
                            "mixed_revision_digest_refused",
                            "original_served_review_pointer_restored_after_model_edit",
                        ],
                        "audit": ports.audit_events,
                        "scope": "Real installed worker/compiler through the new domain client; fixture grant and in-memory audit only. Not real model, host identity, durable inbox, production/user grants or active Mosswork packaging acceptance.",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    asyncio.run(exercise())
