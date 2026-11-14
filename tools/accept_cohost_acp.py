"""Check actual SudoCode wire capabilities and strict missing-session refusal.

No model call, managed cohost, native proof, real grants or application wiring
is qualified. The authority ports are explicit fixtures. The real supplied
binary runs with an isolated home/config and without inherited API credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acp.connection import StreamDirection, StreamEvent

from octop.infra.native_host.acp import CohostAcpSession
from octop.infra.native_host.context import (
    CohostSessionBinding,
    HostBinding,
    HostSelection,
    HostUnavailable,
    ResolvedHost,
    resolve_trusted_host,
)


class FixtureAuthority:
    """Only authorizes this isolated protocol test, never a managed allocation."""

    def __init__(self, principal: object, binding: HostBinding) -> None:
        self.principal, self.binding = principal, binding

    async def resolve(self, principal: object, selection: HostSelection) -> ResolvedHost:
        assert principal is self.principal and selection == self.binding.selection
        return ResolvedHost(self.binding, FixtureRuntime(self.binding))

    async def check_current(self, principal: object, binding: HostBinding) -> None:
        assert principal is self.principal and binding == self.binding

    async def authorize(
        self, principal: object, binding: HostBinding, operation: str, resource_ref: str
    ) -> None:
        await self.check_current(principal, binding)
        assert operation == "session_load" and resource_ref == "isolated-protocol-fixture"


class FixtureRuntime:
    def __init__(self, binding: HostBinding) -> None:
        self.binding = binding

    async def check_current(self, binding: HostBinding) -> None:
        assert binding == self.binding

    async def check_admission(self, binding: HostBinding) -> None:
        assert binding == self.binding


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def accept(binary: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    original_hash = sha256(binary)
    # Retain one immutable executable input; never modify/build the shared target.
    snapshot = output / binary.name
    shutil.copy2(binary, snapshot)
    assert sha256(snapshot) == original_hash == sha256(binary), "Binary changed during snapshot"
    home, workspace = output / "isolated-home", output / "workspace"
    home.mkdir()
    workspace.mkdir()
    retained_env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP"}
    }
    retained_env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        SCODE_GLOBAL_CONFIG_DIR=str(home / "config"),
        SCODE_PROJECT_CONFIG_DIR=str(home / "project-config"),
        NO_COLOR="1",
    )
    principal = object()
    binding = HostBinding(
        "fixture-human",
        "fixture-org",
        HostSelection("fixture-product-agent", "fixture-product-thread"),
        CohostSessionBinding(
            "fixture-owner",
            "fixture-native-agent",
            "00000000-0000-4000-8000-000000000003",
            str(workspace),
        ),
    )
    methods: list[str] = []
    responses: dict[str, Any] = {}
    requests: dict[int | str, str] = {}

    def observe(event: StreamEvent) -> None:
        msg = event.message
        if event.direction == StreamDirection.OUTGOING and "method" in msg:
            methods.append(msg["method"])
            if "id" in msg:
                requests[msg["id"]] = msg["method"]
        elif event.direction == StreamDirection.INCOMING and msg.get("id") in requests:
            responses[requests[msg["id"]]] = msg

    async def context(_request: Any) -> str:
        raise AssertionError("Missing session must not request model context")

    async def update(_method: str, _value: dict[str, Any]) -> None:
        raise AssertionError("Missing session must not publish model output")

    stderr_path = output / "scode.stderr.log"
    with stderr_path.open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            str(snapshot),
            "acp",
            cwd=workspace,
            env=retained_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
        )
        assert process.stdin is not None and process.stdout is not None
        host = await resolve_trusted_host(
            principal, binding.selection, FixtureAuthority(principal, binding)
        )
        try:
            async with asyncio.timeout(30):
                try:
                    session = await CohostAcpSession.attach(
                        host,
                        reader=process.stdout,
                        writer=process.stdin,
                        execution_cwd=str(workspace),
                        mcp_servers=[],
                        resource_ref="isolated-protocol-fixture",
                        resolve_context=context,
                        on_update=update,
                        observers=[observe],
                    )
                except HostUnavailable:
                    pass
                else:
                    await session.close()
                    raise AssertionError("Nonexistent original session unexpectedly loaded")
            assert methods == ["initialize", "session/load"], methods
            initialized = responses["initialize"]["result"]
            assert CohostAcpSession._supports_required_protocol(initialized)
            refused = responses["session/load"].get("error")
            assert isinstance(refused, dict) and isinstance(refused.get("code"), int)
        finally:
            host.close()
            process.stdin.close()
            try:
                async with asyncio.timeout(5):
                    await process.wait()
            except TimeoutError:
                process.terminate()
                await process.wait()
    repo = Path(__file__).resolve().parents[1]
    inputs = [
        Path(__file__),
        repo / "src/octop/infra/native_host/acp.py",
        repo / "src/octop/infra/native_host/context.py",
        repo / "uv.lock",
    ]
    receipt = {
        "status": "passed",
        "scope": "actual SudoCode ACP capabilities and missing-session refusal; fixture authority",
        "checks": [
            "real initialize capabilities",
            "strict original load request",
            "real missing-session error",
            "no session/new fallback",
        ],
        "binary": {"source": str(binary), "snapshot": str(snapshot), "sha256": original_hash},
        "sdk": {"agent-client-protocol": version("agent-client-protocol")},
        "initialize": initialized,
        "loadErrorCode": refused["code"],
        "methods": methods,
        "processExit": process.returncode,
        "sourceHashes": {str(path.relative_to(repo)): sha256(path) for path in inputs},
        "excluded": [
            "model/API call",
            "managed cohost transport",
            "current native proof and grants",
            "existing durable session resume",
            "product UI",
            "production",
            "phone",
        ],
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "passed",
                "checks": len(receipt["checks"]),
                "receipt": str(output / "receipt.json"),
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scode", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(accept(args.scode.resolve(strict=True), args.output.resolve()))


if __name__ == "__main__":
    main()
