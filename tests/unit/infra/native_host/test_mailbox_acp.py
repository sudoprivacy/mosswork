"""New strict-consumer/official-parser/socketpair composition, with fixture SDK authority."""

import asyncio
import json

import pytest
from tests.unit.infra.native_host.test_acp import BINDING, INITIALIZED, PRINCIPAL, Authority
from tests.unit.native_host.test_mailbox import ENDPOINT, Sdk

from octop.infra.native_host.acp import AcpOutcomeUnknown, AcpRequestFailed, CohostAcpSession
from octop.infra.native_host.context import HostUnavailable, resolve_trusted_host
from octop.infra.native_host.mailbox import ControllerMailboxStreams
from octop.infra.native_host.sdk import ControllerSdkError


class MailboxPeer(Sdk):
    """Native replies and SDK transport here are fixtures, not real runtime proof."""

    def __init__(self, *, missing=False, lost=None):
        super().__init__()
        self.requests = []
        self.responses = asyncio.Queue()
        self.missing, self.lost = missing, lost

    async def session_operation(self, operation, payload=""):
        if operation != "send":
            return await super().session_operation(operation, payload)
        self.operations.append((operation, payload))
        message = json.loads(payload)
        if "method" not in message:
            await self.responses.put(message)
            return ""
        self.requests.append(message)
        method = message["method"]
        if self.lost == method:
            raise ControllerSdkError("OUTCOME_UNKNOWN", is_outcome_uncertain=True)
        if method == "initialize":
            result = INITIALIZED
        elif method == "session/load":
            assert message["params"]["sessionId"] == BINDING.native.durable_session_id
            if self.missing:
                await self.incoming.put(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {
                                "code": -32002,
                                "message": "private-original-rejection",
                                "data": {
                                    "sessionId": BINDING.native.durable_session_id,
                                    "text": "原始 🧩",
                                },
                            },
                        }
                    )
                )
                return ""
            result = {}
        elif method == "session/prompt":
            result = {"stopReason": "end_turn"}
        else:
            raise AssertionError("No new-session or native cancellation in this fixture")
        await self.incoming.put(
            json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result})
        )
        return ""


async def setup(peer):
    authority = Authority()
    host = await resolve_trusted_host(PRINCIPAL, BINDING.selection, authority)

    async def current_attachment():
        await host.check_ready("session_load", "registered-execution")

    streams = await ControllerMailboxStreams.attach(
        peer.connection, ENDPOINT, check_attachment=current_attachment
    )
    updates = asyncio.Queue()
    context = {"text": "当前原始模型 🧩"}

    async def read_context():
        return context["text"]

    async def update(method, value):
        await updates.put((method, value))

    async def attach():
        return await CohostAcpSession.attach(
            host,
            reader=streams.reader,
            writer=streams.writer,
            execution_cwd="/proc/812/root/owner/workspace",
            mcp_servers=[],
            resource_ref="registered-execution",
            resolve_context=host.host_context_resolver("registered-model", read_context),
            on_update=update,
        )

    return streams, attach, context, updates, authority


async def test_public_mailbox_streams_attach_exact_original_and_exchange_fresh_context():
    peer = MailboxPeer()
    streams, attach, context, updates, authority = await setup(peer)
    session = None
    try:
        async with asyncio.timeout(8):
            session = await attach()
            assert [message["method"] for message in peer.requests] == [
                "initialize",
                "session/load",
            ]
            context["text"] = "新读取，保留同一原始会话 🧩"
            await peer.incoming.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "original-context-request",
                        "method": "_sudocode/session/host_context",
                        "params": {"sessionId": session.session_id},
                    }
                )
            )
            assert await peer.responses.get() == {
                "jsonrpc": "2.0",
                "id": "original-context-request",
                "result": {"appendSystemPrompt": context["text"]},
            }
            await peer.incoming.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "_sudocode/session/input_update",
                        "params": {
                            "sessionId": session.session_id,
                            "update": {"clientId": "original-input"},
                        },
                    }
                )
            )
            assert await updates.get() == (
                "_sudocode/session/input_update",
                {"clientId": "original-input"},
            )
            assert await session.prompt("同一原始会话继续") == {"stopReason": "end_turn"}
            assert ("sudograph_context", "registered-model") in authority.operations
    finally:
        if session is not None:
            await session.close()
        await streams.close()
    assert not any(
        message["method"] in {"session/new", "session/cancel"} for message in peer.requests
    )


async def test_original_mailbox_rejection_reaches_private_strict_consumer_receipt():
    peer = MailboxPeer(missing=True)
    streams, attach, *_ = await setup(peer)
    try:
        async with asyncio.timeout(8):
            with pytest.raises(AcpRequestFailed) as error:
                await attach()
            receipt = error.value.receipt
            assert receipt.binding == BINDING and receipt.phase == "load"
            assert receipt.request_id == peer.requests[-1]["id"]
            assert receipt.rejection.code == -32002
            assert receipt.rejection.data == {
                "sessionId": BINDING.native.durable_session_id,
                "text": "原始 🧩",
            }
            assert "private-original-rejection" not in str(error.value)
            assert [message["method"] for message in peer.requests] == [
                "initialize",
                "session/load",
            ]
    finally:
        await streams.close()


@pytest.mark.parametrize("method", ["session/load", "session/prompt"])
async def test_lost_append_withdraws_strict_consumer_without_replay_or_new(method):
    peer = MailboxPeer(lost=method)
    streams, attach, *_ = await setup(peer)
    session = None
    try:
        async with asyncio.timeout(8):
            if method == "session/load":
                with pytest.raises(AcpOutcomeUnknown) as error:
                    await attach()
            else:
                session = await attach()
                with pytest.raises(AcpOutcomeUnknown) as error:
                    await session.prompt("one original request")
                with pytest.raises(HostUnavailable):
                    await session.prompt("no replay")
            assert error.value.receipt.binding == BINDING
            assert error.value.receipt.outcome == "unknown"
            assert streams.failure is not None
            assert sum(message["method"] == method for message in peer.requests) == 1
    finally:
        if session is not None:
            await session.close()
        await streams.close()
    assert not any(
        message["method"] in {"session/new", "session/cancel"} for message in peer.requests
    )
