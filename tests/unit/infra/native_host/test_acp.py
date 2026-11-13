"""Real ACP socket workflows; authority/runtime ports here are explicit fixtures."""

from __future__ import annotations

import asyncio
import copy
import pickle
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from acp.connection import Connection
from acp.exceptions import RequestError

from octop.infra.native_host.acp import AcpOutcomeUnknown, CohostAcpSession
from octop.infra.native_host.context import (
    CohostSessionBinding,
    HostBinding,
    HostSelection,
    HostUnavailable,
    ResolvedHost,
    resolve_trusted_host,
)

INITIALIZED = {
    "protocolVersion": 1,
    "agentCapabilities": {"loadSession": True},
    "_meta": {"sudocode": {"hostContext": {"version": 1}, "inputDelivery": {"version": 1}}},
}
BINDING = HostBinding(
    "canonical-account",
    "selected-org",
    HostSelection("product-agent", "product-thread"),
    CohostSessionBinding("org-member", "native-agent", "durable-original", "/canonical/repo"),
)


@dataclass
class Authority:
    """A fixture, not a production identity/grant adapter."""

    denied: bool = False
    operations: list[tuple[str, str]] = field(default_factory=list)

    async def resolve(self, principal, selection):
        assert principal is PRINCIPAL
        assert selection == BINDING.selection
        return ResolvedHost(BINDING, Runtime())

    async def check_current(self, principal, binding):
        assert principal is PRINCIPAL and binding == BINDING
        if self.denied:
            raise PermissionError("sensitive-provider-detail")

    async def authorize(self, principal, binding, operation, resource_ref):
        await self.check_current(principal, binding)
        self.operations.append((operation, resource_ref))


class Runtime:
    async def check_current(self, binding):
        assert binding == BINDING

    async def check_admission(self, binding):
        assert binding == BINDING


PRINCIPAL = object()


class Peer:
    def __init__(self):
        self.initialized = copy.deepcopy(INITIALIZED)
        self.loaded = {}
        self.load_error = False
        self.after_load = None
        self.prompt_handler = None
        self.calls = []
        self.connection = None
        self.writer = None
        self.cancelled = asyncio.Event()

    async def receive(self, method, params, notification):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "initialize":
            return self.initialized
        if method == "session/load":
            if isinstance(self.load_error, RequestError):
                raise self.load_error
            if self.load_error:
                raise RequestError(-32602, "sensitive missing-session detail")
            if self.after_load:
                await self.after_load()
            return self.loaded
        if method == "session/prompt":
            if self.prompt_handler:
                return await self.prompt_handler(params)
            return {"stopReason": "end_turn"}
        if method == "session/cancel" and notification:
            self.cancelled.set()
            return None
        raise AssertionError(f"unexpected RPC {method}")


@asynccontextmanager
async def wire(peer=None, authority=None, *, bound_host=None):
    """Use OS-assigned ports and the official ACP implementation on both ends."""
    peer = peer or Peer()
    authority = authority or Authority()
    accepted = asyncio.Queue()

    async def accept(reader, writer):
        await accepted.put((reader, writer))

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    client_reader, client_writer = await asyncio.open_connection(
        "127.0.0.1", server.sockets[0].getsockname()[1]
    )
    peer_reader, peer_writer = await accepted.get()
    peer.connection = Connection(peer.receive, peer_writer, peer_reader)
    peer.writer = peer_writer
    host = (
        bound_host
        if bound_host is not None
        else await resolve_trusted_host(PRINCIPAL, BINDING.selection, authority)
    )
    context = {"text": "revision=1"}
    updates = []
    peer.lifecycle_updates = []
    session = None

    async def read_context():
        return context["text"]

    async def on_update(method, update):
        updates.append((method, update))

    async def record_lifecycle(binding, update):
        # Synthetic original connection/custody and recording, never a grant.
        assert binding == host.binding
        peer.lifecycle_updates.append(copy.deepcopy(update))

    async def attach(**overrides):
        nonlocal session
        session = await CohostAcpSession.attach(
            host,
            reader=client_reader,
            writer=client_writer,
            execution_cwd="/proc/812/root/owner/workspace",
            mcp_servers=overrides.pop("mcp_servers", []),
            resource_ref=overrides.pop("resource_ref", "registered-execution"),
            resolve_context=overrides.pop(
                "resolve_context", host.host_context_resolver("registered-model", read_context)
            ),
            on_update=overrides.pop("on_update", on_update),
            on_input_lifecycle=overrides.pop("on_input_lifecycle", record_lifecycle),
            **overrides,
        )
        return session

    try:
        async with asyncio.timeout(8):
            yield peer, authority, host, context, updates, attach
    finally:
        if session:
            await session.close()
        await peer.connection.close()
        client_writer.close()
        peer_writer.close()
        await asyncio.gather(client_writer.wait_closed(), peer_writer.wait_closed())
        server.close()
        await server.wait_closed()


async def test_exact_load_and_fresh_execution_context():
    async with wire() as (peer, authority, _host, context, _updates, attach):
        session = await attach()
        assert peer.calls[1] == (
            "session/load",
            {
                "sessionId": "durable-original",
                "cwd": "/proc/812/root/owner/workspace",
                "mcpServers": [],
                "_meta": {"sudocode": {"hostContext": {"version": 1}}},
            },
        )
        # Mutated after load; the callback reads at execution, without putting
        # business text into initialize/load metadata or user history.
        context["text"] = "revision=2"
        first = await peer.connection.send_request(
            "_sudocode/session/host_context", {"sessionId": session.session_id}
        )
        context["text"] = "revision=3"
        second = await peer.connection.send_request(
            "_sudocode/session/host_context", {"sessionId": session.session_id}
        )
        assert first == {"appendSystemPrompt": "revision=2"}
        assert second == {"appendSystemPrompt": "revision=3"}
        assert ("sudograph_context", "registered-model") in authority.operations
        assert await session.prompt("continue") == {"stopReason": "end_turn"}
        assert all(method != "session/new" for method, _ in peer.calls)


async def test_load_preserves_registered_private_connector_configuration():
    async with wire() as (peer, _authority, _host, _context, _updates, attach):
        servers = [
            {
                "type": "http",
                "name": "registered-domain",
                "url": "http://127.0.0.1/mcp",
                "headers": [],
            }
        ]
        await attach(mcp_servers=servers)
        assert peer.calls[1][1]["mcpServers"] == servers


@pytest.mark.parametrize("feature", ["hostContext", "inputDelivery", "loadSession", "boolean"])
async def test_missing_required_capability_never_loads_or_creates(feature):
    peer = Peer()
    if feature == "loadSession":
        peer.initialized["agentCapabilities"]["loadSession"] = False
    elif feature == "boolean":
        peer.initialized["_meta"]["sudocode"]["hostContext"]["version"] = True
    else:
        del peer.initialized["_meta"]["sudocode"][feature]
    async with wire(peer) as (*_, attach):
        with pytest.raises(HostUnavailable):
            await attach()
        assert [method for method, _ in peer.calls] == ["initialize"]


@pytest.mark.parametrize(
    "loaded", [None, {"sessionId": "foreign"}, {"id": "foreign"}, {"session_id": "foreign"}]
)
async def test_invalid_load_cannot_replace_original_binding(loaded):
    peer = Peer()
    peer.loaded = loaded
    async with wire(peer) as (*_, attach):
        with pytest.raises(HostUnavailable):
            await attach()
        assert [method for method, _ in peer.calls] == ["initialize", "session/load"]


async def test_explicit_load_error_is_sanitized_and_never_creates():
    peer = Peer()
    peer.load_error = True
    async with wire(peer) as (*_, attach):
        with pytest.raises(HostUnavailable, match="Original ACP session attachment failed") as exc:
            await attach()
        assert "sensitive" not in str(exc.value)
        assert [method for method, _ in peer.calls] == ["initialize", "session/load"]


async def test_membership_revoked_during_load_cannot_become_ready():
    peer, authority = Peer(), Authority()

    async def revoke():
        authority.denied = True

    peer.after_load = revoke
    async with wire(peer, authority) as (*_, attach):
        with pytest.raises(HostUnavailable):
            await attach()
        assert len(peer.calls) == 2


async def test_foreign_context_request_is_refused_without_provider_data():
    async with wire() as (peer, _authority, _host, _context, _updates, attach):
        await attach()
        with pytest.raises(RequestError, match="Current host context is unavailable"):
            await peer.connection.send_request(
                "_sudocode/session/host_context", {"sessionId": "foreign"}
            )


async def test_context_callback_maps_product_thread_without_trusting_wire_aliases():
    async with wire() as (peer, _authority, _host, _context, _updates, attach):
        captured = []

        async def resolve(request):
            captured.append(request)
            return "current model"

        session = await attach(resolve_context=resolve)
        await peer.connection.send_request(
            "_sudocode/session/host_context",
            {"sessionId": session.session_id, "nativeSessionId": "untrusted-alias"},
        )
        assert captured == [{"sessionId": "product-thread", "nativeSessionId": "durable-original"}]


async def test_closed_transport_cannot_return_an_in_flight_context():
    async with wire() as (peer, _authority, _host, _context, _updates, attach):
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(_request):
            entered.set()
            await release.wait()
            return "must not be returned"

        session = await attach(resolve_context=blocked)
        pending = asyncio.create_task(
            peer.connection.send_request(
                "_sudocode/session/host_context", {"sessionId": session.session_id}
            )
        )
        await entered.wait()
        # Complete an actual half-close: the original socket delivers EOF to
        # the consumer while the writer is still technically writable.
        peer.writer.write_eof()
        await peer.writer.drain()
        # The observer-only request never goes to the engine; waiting on the
        # reader itself gives the event loop a deterministic EOF boundary.
        while not session._reader.at_eof():
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(RequestError, match="Current host context is unavailable"):
            await pending
        with pytest.raises(HostUnavailable):
            await session.prompt("closed transport")


async def test_stop_discards_in_flight_context_then_allows_fresh_read():
    async with wire() as (peer, _authority, _host, _context, _updates, attach):
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(_request):
            entered.set()
            await release.wait()
            return "current model"

        session = await attach(resolve_context=blocked)
        pending = asyncio.create_task(
            peer.connection.send_request(
                "_sudocode/session/host_context", {"sessionId": session.session_id}
            )
        )
        await entered.wait()
        await session.cancel()
        await peer.cancelled.wait()
        release.set()
        with pytest.raises(RequestError, match="Current host context is unavailable"):
            await pending
        assert await peer.connection.send_request(
            "_sudocode/session/host_context", {"sessionId": session.session_id}
        ) == {"appendSystemPrompt": "current model"}


async def test_revocation_during_context_read_never_returns_stale_text():
    async with wire() as (peer, authority, _host, _context, _updates, attach):

        async def revoke(_request):
            authority.denied = True
            return "confidential model"

        session = await attach(resolve_context=revoke)
        with pytest.raises(RequestError) as exc:
            await peer.connection.send_request(
                "_sudocode/session/host_context", {"sessionId": session.session_id}
            )
        assert exc.value.data is None
        assert "confidential" not in str(exc.value)


async def test_prompt_disconnect_retains_unknown_and_does_not_replay():
    peer = Peer()
    async with wire(peer) as (*_, attach):
        entered = asyncio.Event()

        async def disconnect(_params):
            entered.set()
            peer.writer.close()
            # A broken transport has no prompt response. The peer's handler is
            # cancelled by normal connection teardown after the client refuses.
            await asyncio.Future()

        peer.prompt_handler = disconnect
        session = await attach()
        with pytest.raises(AcpOutcomeUnknown):
            await session.prompt("one request")
        await entered.wait()
        with pytest.raises(HostUnavailable):
            await session.prompt("no automatic replay")
        assert sum(method == "session/prompt" for method, _ in peer.calls) == 1


async def test_native_prompt_error_is_sanitized_and_never_replayed():
    async with wire() as (peer, _authority, _host, _context, _updates, attach):

        async def reject(_params):
            raise RequestError(-32603, "secret-provider-detail")

        peer.prompt_handler = reject
        session = await attach()
        with pytest.raises(HostUnavailable, match="Original ACP prompt failed") as exc:
            await session.prompt("one request")
        assert "secret" not in str(exc.value)
        with pytest.raises(HostUnavailable):
            await session.prompt("must not replay")
        assert sum(method == "session/prompt" for method, _ in peer.calls) == 1


async def test_foreign_notification_closes_projection_and_future_dispatch():
    async with wire() as (peer, _authority, _host, _context, updates, attach):
        session = await attach()
        await peer.connection.send_notification(
            "session/update",
            {"sessionId": "foreign", "update": {"sessionUpdate": "agent_message_chunk"}},
        )
        # A subsequent request is processed after the notification on this
        # connection, giving a deterministic boundary without timing sleeps.
        with pytest.raises(RequestError):
            await peer.connection.send_request(
                "_sudocode/session/host_context", {"sessionId": session.session_id}
            )
        assert updates == []
        with pytest.raises(HostUnavailable):
            await session.prompt("must refuse")


async def test_close_and_unwired_permission_do_not_claim_native_cleanup():
    async with wire() as (peer, _authority, _host, _context, _updates, attach):
        session = await attach()
        assert await peer.connection.send_request(
            "session/request_permission", {"sessionId": session.session_id}
        ) == {"outcome": {"outcome": "cancelled"}}
        await session.close()
        assert [method for method, _ in peer.calls] == ["initialize", "session/load"]
        with pytest.raises(HostUnavailable):
            await session.prompt("closed")


@pytest.mark.parametrize(
    ("code", "data"),
    [
        (-32002, {"sessionId": "durable-original"}),
        (-32002, {"sessionId": "foreign"}),
        (-32002, None),
        (-32603, {"sessionId": "durable-original"}),
        (-32001, {"reason": "secret-provider-detail"}),
    ],
)
async def test_load_rejection_retains_private_correlated_receipt(code, data):
    peer = Peer()
    peer.load_error = RequestError(code, "secret-native-rejection", data)
    async with wire(peer) as (*_, attach):
        with pytest.raises(HostUnavailable) as exc:
            await attach()
        receipt = getattr(exc.value, "receipt", None)
        assert receipt is not None
        assert receipt.binding == BINDING
        assert receipt.phase == "load" and receipt.outcome == "rejected"
        assert receipt.request_id == 1
        assert receipt.rejection.code == code
        assert receipt.rejection.message == "secret-native-rejection"
        assert receipt.rejection.data == data
        assert "secret" not in str(exc.value)
        assert "secret" not in repr(receipt)
        with pytest.raises(TypeError, match="private"):
            pickle.dumps(receipt)
        assert [method for method, _ in peer.calls] == ["initialize", "session/load"]


def fail_local_close(monkeypatch):
    original = Connection.close

    async def close(connection):
        await original(connection)
        if isinstance(getattr(connection._handler, "__self__", None), CohostAcpSession):
            raise RuntimeError("secret-local-close-detail")

    monkeypatch.setattr(Connection, "close", close)


async def test_local_close_cannot_overwrite_correlated_load_rejection(monkeypatch):
    fail_local_close(monkeypatch)
    peer = Peer()
    peer.load_error = RequestError(-32002, "secret-first-error", {"sessionId": "durable-original"})
    async with wire(peer) as (*_, attach):
        with pytest.raises(HostUnavailable) as exc:
            await attach()
        assert exc.value.receipt.rejection.code == -32002
        assert exc.value.receipt.rejection.message == "secret-first-error"
        assert isinstance(exc.value.receipt.local_close_error, RuntimeError)
        assert "secret" not in str(exc.value)


async def test_load_disconnect_retains_unknown_original_binding():
    peer = Peer()

    async def disconnect():
        peer.writer.close()
        await asyncio.Future()

    peer.after_load = disconnect
    async with wire(peer) as (*_, attach):
        with pytest.raises(AcpOutcomeUnknown) as exc:
            await attach()
        assert exc.value.receipt.binding == BINDING
        assert exc.value.receipt.phase == "load"
        assert exc.value.receipt.outcome == "unknown"
        assert exc.value.receipt.request_id == 1
        assert [method for method, _ in peer.calls] == ["initialize", "session/load"]


@pytest.mark.parametrize("broken_close", [False, True])
async def test_load_task_cancellation_keeps_cancellation_and_unknown(monkeypatch, broken_close):
    if broken_close:
        fail_local_close(monkeypatch)
    entered = asyncio.Event()
    peer = Peer()

    async def block():
        entered.set()
        await asyncio.Future()

    peer.after_load = block
    async with wire(peer) as (*_, attach):
        task = asyncio.create_task(attach())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as exc:
            await task
        assert task.cancelled()
        receipt = getattr(exc.value, "receipt", None)
        assert receipt is not None
        assert receipt.binding == BINDING and receipt.phase == "load"
        assert receipt.outcome == "unknown" and receipt.request_id == 1
        assert isinstance(receipt.local_close_error, RuntimeError) == broken_close
        assert [method for method, _ in peer.calls] == ["initialize", "session/load"]


async def test_pre_dispatch_refusal_does_not_claim_load_was_sent():
    async with wire() as (peer, authority, _host, _context, _updates, attach):
        authority.denied = True
        with pytest.raises(HostUnavailable) as exc:
            await attach()
        receipt = getattr(exc.value, "receipt", None)
        assert receipt is not None
        assert receipt.phase == "admission" and receipt.outcome == "refused"
        assert receipt.request_id is None and receipt.rejection is None
        assert peer.calls == []


async def test_revocation_after_load_reply_preserves_original_custody():
    peer, authority = Peer(), Authority()

    async def revoke():
        authority.denied = True

    peer.after_load = revoke
    async with wire(peer, authority) as (*_, attach):
        with pytest.raises(AcpOutcomeUnknown) as exc:
            await attach()
        assert exc.value.receipt.phase == "post_load"
        assert exc.value.receipt.binding == BINDING
        assert exc.value.receipt.rejection is None


async def test_prompt_rejection_retains_private_original_details():
    async with wire() as (peer, _authority, _host, _context, _updates, attach):

        async def reject(_params):
            raise RequestError(-32603, "secret-native-detail", {"partial": "secret-effect-detail"})

        peer.prompt_handler = reject
        session = await attach()
        with pytest.raises(HostUnavailable) as exc:
            await session.prompt("one request")
        receipt = getattr(exc.value, "receipt", None)
        assert receipt is not None
        assert receipt.phase == "prompt" and receipt.request_id == 2
        assert receipt.rejection.code == -32603
        assert receipt.rejection.data == {"partial": "secret-effect-detail"}
        assert "secret" not in str(exc.value)
        with pytest.raises(HostUnavailable):
            await session.prompt("no replay")
        assert sum(method == "session/prompt" for method, _ in peer.calls) == 1


async def test_prompt_task_cancellation_is_not_converted_to_ordinary_failure():
    entered = asyncio.Event()
    async with wire() as (peer, _authority, _host, _context, _updates, attach):

        async def block(_params):
            entered.set()
            await asyncio.Future()

        peer.prompt_handler = block
        session = await attach()
        task = asyncio.create_task(session.prompt("one request"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as exc:
            await task
        assert task.cancelled()
        assert exc.value.receipt.phase == "prompt"
        assert exc.value.receipt.outcome == "unknown"
        assert exc.value.receipt.request_id == 2
        with pytest.raises(HostUnavailable):
            await session.prompt("no replay")
        assert sum(method == "session/prompt" for method, _ in peer.calls) == 1
