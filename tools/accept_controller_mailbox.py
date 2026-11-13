"""Bounded model-free Python/Node/SDK/typed mTLS fixture acceptance.

Supply the selected installed controller package and its matching source test
helpers explicitly. This opens only ephemeral local fixture services, with
synthetic credentials, no native engine, current grants or production access.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from acp.connection import Connection
from acp.exceptions import RequestError

from octop.infra.native_host.cohost import CohostSessionEndpoint
from octop.infra.native_host.mailbox import ControllerMailboxStreams, MailboxOutcomeUnknown
from octop.infra.native_host.sdk import (
    ControllerSdkCommand,
    ControllerSdkConfiguration,
    ControllerSdkConnection,
    ControllerSdkError,
)


async def accept(args: argparse.Namespace) -> None:
    node, package, source = (
        Path(getattr(args, key)).resolve() for key in ("node", "package", "source")
    )
    fixture = (
        Path(__file__).resolve().parent.parent
        / "tests/unit/native_host/fixtures/mailbox_server.mjs"
    )
    controller = package / "bin/controller-rpc.mjs"
    checks: list[str] = []
    assertions: list[str] = []

    async def check() -> None:
        assertions.append("attachment-current")

    async def dispatch(method: str, _payload: str) -> None:
        assertions.append(method)

    with tempfile.TemporaryDirectory(prefix="mosswork-sdk-mailbox-") as directory:
        child = await asyncio.create_subprocess_exec(
            str(node),
            "--import",
            (source / "node_modules/tsx/dist/loader.mjs").as_uri(),
            str(fixture),
            str(source),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert child.stdin and child.stdout and child.stderr
        diagnostics = asyncio.create_task(child.stderr.read())
        child.stdin.write((json.dumps({"directory": directory}) + "\n").encode())
        await child.stdin.drain()
        sdk = streams = acp = None
        try:
            boot = json.loads(await child.stdout.readline())
            configuration = ControllerSdkConfiguration(
                "alice",
                boot["issuerEndpoint"],
                Path(boot["ca"]),
                Path(boot["cert"]),
                Path(boot["key"]),
                runtime_endpoint=boot["runtimeEndpoint"],
                server_name="issuer.test",
            )
            sdk = await ControllerSdkConnection.connect(
                ControllerSdkCommand(node, controller),
                configuration,
                before_connect=check,
                before_dispatch=dispatch,
                required_operations=frozenset(
                    {
                        "file.read",
                        "session.open",
                        "session.send",
                        "session.receive",
                        "session.close",
                    }
                ),
            )
            selected = json.loads(
                await sdk.call("managed_agent.get_session_v1", '{"session_id":"19.3"}', "")
            )
            endpoint = CohostSessionEndpoint(
                **{k: v for k, v in selected["session_endpoint"].items() if k != "protocol"}
            )
            streams = await ControllerMailboxStreams.attach(sdk, endpoint, check_attachment=check)
            notifications: asyncio.Queue[Any] = asyncio.Queue()

            async def handle(method: str, params: Any, notification: bool) -> Any:
                if notification:
                    await notifications.put((method, params))
                    return None
                assert method == "_sudocode/session/host_context" and params == {
                    "sessionId": "history-original"
                }
                return {"appendSystemPrompt": "当前原始上下文 🧩"}

            acp = Connection(handle, streams.writer, streams.reader)
            initialized = await acp.send_request("initialize", {"protocolVersion": 1})
            assert initialized["_meta"]["sudocode"]["hostContext"]["version"] == 1
            checks.append("official_acp_initialize_over_typed_owner_mtls_mailbox")
            assert await acp.send_request("session/load", {"sessionId": "history-original"}) == {}
            checks.append("original_empty_load_success_preserved_without_new")
            try:
                await acp.send_request("session/load", {"sessionId": "missing-original"})
            except RequestError as error:
                assert error.code == -32002 and error.data == {"sessionId": "missing-original"}
            else:
                raise AssertionError("Missing original load must refuse")
            checks.append("precise_original_missing_error_data_and_correlation_preserved")
            result = await acp.send_request("session/prompt", {"sessionId": "history-original"})
            assert result == {"stopReason": "end_turn"}
            method, event = await notifications.get()
            assert method == "_sudocode/session/input_update"
            assert (
                event["sessionId"] == "history-original"
                and event["update"]["clientId"] == "original-input"
            )
            checks.append("bidirectional_current_context_unicode_original_input_update")
            child.stdin.write(b'{"operation":"lose-reply"}\n')
            await child.stdin.drain()
            state = json.loads(await child.stdout.readline())
            assert state["inputs"] == 1
            try:
                await acp.send_request("session/prompt", {"sessionId": "history-original"})
            except (ConnectionError, MailboxOutcomeUnknown):
                pass
            else:
                raise AssertionError("Lost append reply must withdraw the original transport")
            child.stdin.write(b'{"operation":"state"}\n')
            await child.stdin.drain()
            state = json.loads(await child.stdout.readline())
            assert state["inputs"] == 2 and state["minted"] == 1 and state["calls"] == 1
            checks.append("committed_lost_reply_unknown_without_retry_new_identity_or_cancel")
            assert (
                await sdk.read_file("/original/transcript")
                == "original transcript 客户 📝\n".encode()
            )
            assert await sdk.read_file("/original/empty") == b""
            try:
                await sdk.read_file("/original/unavailable")
            except ControllerSdkError as error:
                assert error.code == "SDK_READ_FAILED" and error.is_outcome_uncertain
            else:
                raise AssertionError(
                    "Unavailable read cannot establish an empty or missing session"
                )
            checks.append("official_typed_file_read_exact_empty_and_failure_on_original_owner_mtls")
            try:
                await sdk.session_operation(
                    "send", '{"jsonrpc":"2.0","id":"no-replay","method":"session/prompt"}'
                )
            except ControllerSdkError as error:
                assert error.code == "OUTCOME_UNKNOWN" and error.is_outcome_uncertain
            else:
                raise AssertionError("Reading cannot clear original append uncertainty")
            child.stdin.write(b'{"operation":"state"}\n')
            await child.stdin.drain()
            state = json.loads(await child.stdout.readline())
            assert (
                state["fileReads"] == 3
                and state["minted"] == 1
                and state["calls"] == 1
                and state["inputs"] == 2
            )
            checks.append("file_observation_never_clears_original_unknown_write_or_remints")
        finally:
            if acp:
                await acp.close()
            if streams:
                await streams.close()
            if sdk:
                await sdk.close()
            child.stdin.close()
            await child.wait()
            private_errors = await diagnostics
            if child.returncode or private_errors:
                Path(args.receipt + ".fixture.stderr.log").write_bytes(private_errors)
            assert child.returncode == 0, (
                "Ephemeral fixture failed (diagnostics retained beside receipt)"
            )
            assert not private_errors
    receipt = {
        "passed": True,
        "checks": checks,
        "assertions": len(assertions),
        "scope": "Actual private Python streams/official ACP/parser/Node SDK0.8.1/typed gRPC mTLS. Synthetic fixture issuer and replies, no actual native engine/grants/managed provider/model/production acceptance.",
        "source": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                fixture,
                controller,
                package / "dist/controllerRpc.js",
                package / "dist/mailbox.js",
                Path(__file__).resolve(),
                Path(__file__).resolve().parent.parent / "src/octop/infra/native_host/sdk.py",
            )
        },
    }
    Path(args.receipt).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": True, "checks": len(checks), "receipt": args.receipt}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "package", "source", "receipt"):
        parser.add_argument("--" + name, required=True)
    arguments = parser.parse_args()
    asyncio.run(accept(arguments))
