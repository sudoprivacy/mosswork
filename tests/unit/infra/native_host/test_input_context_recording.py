"""Original input context over official ACP; native state and persistence are fixtures."""

import asyncio
import hashlib
import json
from dataclasses import replace

import pytest
from acp.exceptions import RequestError
from tests.unit.infra.native_host.test_acp import BINDING, Peer, wire
from tests.unit.infra.native_host.test_input_delivery import INPUT, item, state
from tests.unit.native_host.test_sudograph import compiled_context, rows
from tests.unit.native_host.test_sudograph import prepared as prepared
from tests.unit.native_host.test_sudograph import registered as registered
from tests.unit.native_host.test_sudograph import retained as retained

from octop.infra.native_host.context import HostUnavailable, _require_completion
from octop.infra.sudograph.protocol import ToolResult


class AcceptedPeer(Peer):
    def __init__(self, session_id=BINDING.native.durable_session_id):
        super().__init__()
        self.session_id = session_id

    async def update(self, update):
        await self.connection.send_notification(
            "_sudocode/session/input_update", {"sessionId": self.session_id, "update": update}
        )

    async def receive(self, method, params, notification):
        if method != INPUT:
            return await super().receive(method, params, notification)
        self.calls.append((method, dict(params)))
        await self.update({"kind": "state", "state": state(0)})
        queued = state(1, pending=[item(client_id=params["clientId"], delivery="queue")])
        await self.update({"kind": "state", "state": queued})
        return {"id": 71, "state": queued}


@pytest.mark.parametrize("failure", ["error", "malformed", "cancelled"])
async def test_recorder_failure_after_acceptance_withdraws_and_retains_original_unknown(failure):
    peer = AcceptedPeer()
    async with wire(peer) as (_peer, _authority, _host, _context, _updates, attach):
        entered = asyncio.Event()
        observed, request_ids = [], []
        session = None

        def capture(event):
            if event.message.get("method") == "_sudocode/session/host_context":
                request_ids.append(event.message["id"])

        async def context(scope):
            observed.append(session.context_input())
            entered.set()
            if failure == "error":
                raise RuntimeError("private-possible-write-diagnostic")
            if failure == "cancelled":
                raise asyncio.CancelledError()
            await _require_completion(asyncio.sleep(0, result=False))
            raise AssertionError("Malformed completion must not release text")

        session = await attach(resolve_context=context, observers=[capture])
        accepted = await session.submit_input("original-client", "Original", delivery="queue")
        assert accepted.native_id == 71
        await peer.update(
            {"kind": "state", "state": state(2, active=71, running=True, pending=[item()])}
        )
        requested = asyncio.create_task(
            peer.connection.send_request(
                "_sudocode/session/host_context", {"sessionId": session.session_id}
            )
        )
        try:
            await entered.wait()
            await asyncio.sleep(0)
            with pytest.raises(HostUnavailable):
                session._check_ready()
            receipt = session.context_failure_receipt
            assert receipt.outcome == "unknown" and receipt.phase == "host_context"
            assert receipt.binding == BINDING
            assert receipt.request_id == request_ids[0]
            assert receipt.input_context is observed[0]
            assert receipt.input_context.turn_id == 71
            assert receipt.input_context.inputs[0].client_id == "original-client"
            if failure != "cancelled":
                with pytest.raises(RequestError) as error:
                    await requested
                assert "private-possible-write" not in str(error.value)
            with pytest.raises(HostUnavailable):
                await session.submit_input("replacement-client", "Must refuse", delivery="queue")
            assert sum(method == INPUT for method, _ in peer.calls) == 1
            await session.close()
            assert session.context_failure_receipt is receipt
        finally:
            if not requested.done():
                requested.cancel()
            await asyncio.gather(requested, return_exceptions=True)


async def context_request(peer, session):
    return await peer.connection.send_request(
        "_sudocode/session/host_context", {"sessionId": session.session_id}
    )


async def activate(peer, native_id=71, client_id="original-client", revision=2):
    await peer.update(
        {
            "kind": "state",
            "state": state(
                revision, active=native_id, running=True, pending=[item(native_id, client_id)]
            ),
        }
    )


def admit_input_transport(prepared):
    resource = prepared.ports.binding.resource_ref
    prepared.authority.grants.update({("session_load", resource), ("session_prompt", resource)})
    return resource


async def test_two_queued_turns_record_current_input_and_one_exact_domain_reply_each(prepared):
    resource = admit_input_transport(prepared)
    compiled_context(prepared)
    first_written, finish_write = asyncio.Event(), asyncio.Event()
    recorded = []

    class TwoTurns(AcceptedPeer):
        async def receive(self, method, params, notification):
            if method == INPUT and params["clientId"] == "second-client":
                self.calls.append((method, dict(params)))
                queued = state(
                    4, active=71, running=True, pending=[item(81, "second-client", "queue")]
                )
                await self.update({"kind": "state", "state": queued})
                return {"id": 81, "state": queued}
            return await super().receive(method, params, notification)

    peer = TwoTurns(prepared.ports._host.binding.native.durable_session_id)
    async with wire(peer, bound_host=prepared.ports._host) as (_p, _a, _h, _c, updates, attach):
        session = None

        async def record_pending(host, registered, pointer, evidence):
            recorded.append((host, registered, pointer, evidence))
            assert evidence.stage == "pending_context"
            if len(recorded) == 1:
                first_written.set()
                await finish_write.wait()

        async def record_context(host, registered, pointer):
            # Resolve the session's CURRENT task evidence on every invocation.
            # No first-input identity is captured at attachment time.
            await session.input_context_recorder(record_pending)(host, registered, pointer)

        session = await attach(
            resource_ref=resource,
            resolve_context=prepared.ports.host_context_resolver(record_context),
        )
        first = await session.submit_input("original-client", "First", delivery="queue")
        await activate(peer)
        reply = asyncio.create_task(context_request(peer, session))
        try:
            await first_written.wait()
            assert not reply.done()
            assert len(prepared.transport.calls) == 1
            assert not any(update.get("kind") == "committed" for _, update in updates)
        finally:
            finish_write.set()
        assert await reply == {"appendSystemPrompt": "Exact compiled Unicode context"}
        await peer.update({"kind": "committed", "clientId": "original-client"})
        second = await session.submit_input("second-client", "Second", delivery="queue")
        assert (first.native_id, second.native_id) == (71, 81)
        await peer.update({"kind": "turn_complete", "turnId": 71, "stopReason": "end_turn"})
        await peer.update(
            {"kind": "state", "state": state(5, pending=[item(81, "second-client", "queue")])}
        )
        await activate(peer, 81, "second-client", revision=6)
        value = prepared.transport.result.decode()
        value["context"].update(
            revision="c" * 64, context_digest="d" * 64, text="Second exact context"
        )
        prepared.transport.result = ToolResult(json.dumps(value))
        assert await context_request(peer, session) == {
            "appendSystemPrompt": "Second exact context"
        }
        assert len(prepared.transport.calls) == 2 and len(rows(prepared)) == 2
        assert [entry[3].turn_id for entry in recorded] == [71, 81]
        assert [entry[3].inputs[0].client_id for entry in recorded] == [
            "original-client",
            "second-client",
        ]
        assert [entry[2].revision for entry in recorded] == ["a" * 64, "c" * 64]
        assert [entry[2].context_digest for entry in recorded] == ["b" * 64, "d" * 64]
        assert hashlib.sha256(b"Second exact context").hexdigest() != recorded[1][2].context_digest
        assert all(entry[0] == prepared.ports._host.binding for entry in recorded)
        assert all(entry[1] == prepared.original for entry in recorded)
        assert session.context_failure_receipt is None


@pytest.mark.parametrize("change", ["grant", "input"])
async def test_possible_pointer_write_then_changed_grant_or_input_retains_original(
    prepared, change
):
    resource = admit_input_transport(prepared)
    compiled_context(prepared)
    peer = AcceptedPeer(prepared.ports._host.binding.native.durable_session_id)
    async with wire(peer, bound_host=prepared.ports._host) as (_p, _a, _h, _c, _u, attach):
        session = None
        recorded = []
        changed = asyncio.Event()

        def observe(event):
            if (
                event.message.get("params", {}).get("update", {}).get("state", {}).get("revision")
                == 4
            ):
                changed.set()

        async def record_pending(host, registered, pointer, evidence):
            recorded.append((pointer, evidence))  # persistence itself is a fixture
            if change == "grant":
                prepared.grants.allowed.clear()
            else:
                await peer.update({"kind": "state", "state": state(3)})
                await activate(peer, 81, "later-client", revision=4)
                await changed.wait()

        async def record_context(host, registered, pointer):
            await session.input_context_recorder(record_pending)(host, registered, pointer)

        session = await attach(
            resource_ref=resource,
            resolve_context=prepared.ports.host_context_resolver(record_context),
            observers=[observe],
        )
        await session.submit_input("original-client", "First", delivery="queue")
        await activate(peer)
        with pytest.raises(RequestError):
            await context_request(peer, session)
        assert len(recorded) == 1 and len(prepared.transport.calls) == 1
        receipt = session.context_failure_receipt
        assert receipt.outcome == "unknown" and receipt.input_context is recorded[0][1]
        assert receipt.input_context.turn_id == 71
        assert receipt.input_context.inputs[0].client_id == "original-client"
        with pytest.raises(HostUnavailable):
            await session.submit_input("later-client", "Do not replay", delivery="queue")
        assert sum(method == INPUT for method, _ in peer.calls) == 1


async def test_actual_adapter_refuses_false_completion_after_possible_pointer_write(prepared):
    resource = admit_input_transport(prepared)
    compiled_context(prepared)
    peer = AcceptedPeer(prepared.ports._host.binding.native.durable_session_id)
    async with wire(peer, bound_host=prepared.ports._host) as (_p, _a, _h, _c, _u, attach):
        session = None
        recorded = []

        async def record_pending(host, registered, pointer, evidence):
            recorded.append((pointer, evidence))
            return False

        async def record_context(host, registered, pointer):
            await session.input_context_recorder(record_pending)(host, registered, pointer)

        session = await attach(
            resource_ref=resource,
            resolve_context=prepared.ports.host_context_resolver(record_context),
        )
        await session.submit_input("original-client", "Original", delivery="queue")
        await activate(peer)
        with pytest.raises(RequestError):
            await context_request(peer, session)
        assert len(recorded) == 1 and len(prepared.transport.calls) == 1
        assert session.context_failure_receipt.input_context is recorded[0][1]
        assert session.context_failure_receipt.outcome == "unknown"


@pytest.mark.parametrize("pause_at", ["initial_active", "recording_wait"])
async def test_pause_then_resume_cannot_release_old_context_through_actual_recorder(
    prepared, pause_at
):
    resource = admit_input_transport(prepared)
    compiled_context(prepared)
    peer = AcceptedPeer(prepared.ports._host.binding.native.durable_session_id)
    async with wire(peer, bound_host=prepared.ports._host) as (_p, _a, _h, _c, _u, attach):
        session = None
        recorded = []
        resumed = asyncio.Event()
        resume_revision = 3 if pause_at == "initial_active" else 4

        def observe(event):
            revision = (
                event.message.get("params", {}).get("update", {}).get("state", {}).get("revision")
            )
            if revision == resume_revision:
                resumed.set()

        async def pause_and_resume(revision):
            active = state(revision, active=71, running=True, pending=[item()])
            await peer.update({"kind": "state", "state": {**active, "paused": True}})
            await peer.update(
                {
                    "kind": "state",
                    "state": state(
                        revision + 1,
                        active=71,
                        running=True,
                        pending=[item(), item(72, "queued", "queue")],
                    ),
                }
            )
            await resumed.wait()

        async def record_pending(host, registered, pointer, evidence):
            recorded.append((pointer, evidence))  # actual persistence remains a fixture
            await pause_and_resume(3)

        async def record_context(host, registered, pointer):
            await session.input_context_recorder(record_pending)(host, registered, pointer)

        session = await attach(
            resource_ref=resource,
            resolve_context=prepared.ports.host_context_resolver(record_context),
            observers=[observe],
        )
        accepted = await session.submit_input("original-client", "Original", delivery="queue")
        assert accepted.native_id == 71
        if pause_at == "initial_active":
            await pause_and_resume(2)
        else:
            await activate(peer)
        with pytest.raises(RequestError):
            await context_request(peer, session)
        if pause_at == "initial_active":
            assert recorded == [] and prepared.transport.calls == []
        else:
            assert len(recorded) == 1 and len(prepared.transport.calls) == 1
            receipt = session.context_failure_receipt
            assert receipt.outcome == "unknown" and receipt.input_context is recorded[0][1]
            assert receipt.input_context.turn_id == 71
            assert receipt.input_context.inputs[0].client_id == "original-client"
            await session.close()
            assert session.context_failure_receipt is receipt
        with pytest.raises(HostUnavailable):
            await session.submit_input("queued", "Do not replay", delivery="queue")
        assert [method for method, _ in peer.calls].count(INPUT) == 1
        assert not peer.lifecycle_updates  # no fabricated committed/completed event


async def test_foreign_host_cannot_be_recorded_against_current_input():
    peer = AcceptedPeer()
    async with wire(peer) as (_p, _a, _h, _c, _u, attach):
        session = None
        calls = []

        async def persist(*args):
            calls.append(args)

        async def context(scope):
            record = session.input_context_recorder(persist)
            await record(replace(BINDING, account_id="foreign"), None, None)
            return "Must not be released"

        session = await attach(resolve_context=context)
        await session.submit_input("original-client", "First", delivery="queue")
        await activate(peer)
        with pytest.raises(RequestError):
            await context_request(peer, session)
        assert calls == [] and session.context_failure_receipt.input_context.binding == BINDING


async def test_closed_attachment_recorder_cannot_be_reused_for_later_input():
    calls = []

    async def persist(*args):
        calls.append(args)

    async with wire() as (_p, _a, _h, _c, _u, attach):
        original = await attach()
        original_record = original.input_context_recorder(persist)
        await original.close()
    peer = AcceptedPeer()
    async with wire(peer) as (_p, _a, _h, _c, _u, attach):

        async def context(scope):
            await original_record(BINDING, None, None)
            return "Must not bind the old attachment"

        session = await attach(resolve_context=context)
        await session.submit_input("original-client", "After reconnect", delivery="queue")
        await activate(peer)
        with pytest.raises(RequestError):
            await context_request(peer, session)
        assert calls == []
        assert (
            session.context_failure_receipt.input_context.inputs[0].client_id == "original-client"
        )
