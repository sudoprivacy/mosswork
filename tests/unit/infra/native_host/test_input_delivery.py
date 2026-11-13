"""Existing inputDelivery wire over official ACP sockets; native/grant replies are fixtures."""

import asyncio
import copy
import json
import pickle

import pytest
from tests.unit.infra.native_host.test_acp import BINDING, PRINCIPAL, Authority, Peer, wire
from tests.unit.infra.native_host.test_mailbox_acp import MailboxPeer
from tests.unit.native_host.test_mailbox import ENDPOINT

from octop.infra.native_host.acp import AcpOutcomeUnknown, CohostAcpSession
from octop.infra.native_host.context import HostUnavailable, resolve_trusted_host
from octop.infra.native_host.mailbox import ControllerMailboxStreams

INPUT = "_sudocode/session/input"


def item(native_id=71, client_id="original-client", delivery="steer"):
    return {
        "id": native_id,
        "clientId": client_id,
        "source": "human",
        "delivery": delivery,
        "display": "original display",
    }


def state(revision, *, active=None, running=False, pending=()):
    return {
        "revision": revision,
        "activeTurnId": active,
        "running": running,
        "paused": False,
        "pending": list(pending),
    }


class InputPeer(Peer):
    def __init__(self, *, baseline=True, bad_state=None, bad_result=None, lose_reply=False):
        super().__init__()
        self.baseline, self.bad_state, self.bad_result = baseline, bad_state, bad_result
        self.lose_reply = lose_reply
        self.committed = False
        self.context_replies = []
        self.during_context = None

    async def update(self, update):
        await self.connection.send_notification(
            "_sudocode/session/input_update",
            {"sessionId": BINDING.native.durable_session_id, "update": update},
        )

    async def receive(self, method, params, notification):
        if method != INPUT:
            return await super().receive(method, params, notification)
        self.calls.append((method, copy.deepcopy(params)))
        assert params == {
            "sessionId": BINDING.native.durable_session_id,
            "action": "submit",
            "clientId": "original-client",
            "content": [{"type": "text", "text": "原始输入 🧩"}],
            "delivery": "queue",
        }
        if self.baseline:
            await self.update(
                {"kind": "state", "state": state(1, pending=[item(delivery="queue")])}
            )
        active = state(
            2, active=71, running=True, pending=[item(), item(72, "queued-other", "queue")]
        )
        if self.bad_state is not None:
            active.update(self.bad_state)
        await self.update({"kind": "state", "state": active})
        if self.during_context is not None:
            await self.during_context()
        self.context_replies.append(
            await self.connection.send_request(
                "_sudocode/session/host_context", {"sessionId": BINDING.native.durable_session_id}
            )
        )
        self.committed = True
        await self.update(
            {
                "kind": "committed",
                "clientId": "original-client",
                "source": "human",
                "display": "original display",
            }
        )
        await self.update({"kind": "turn_complete", "turnId": 71, "stopReason": "end_turn"})
        if self.lose_reply:
            self.writer.close()
            await self.writer.wait_closed()
            await asyncio.Event().wait()
        return (
            self.bad_result
            if self.bad_result is not None
            else {"id": 71, "state": state(1, pending=[item(delivery="queue")])}
        )


async def test_context_recorder_reads_actual_pending_batch_before_commit_and_submit_ack():
    peer = InputPeer()
    async with wire(peer) as (_peer, _authority, _host, _context, _updates, attach):
        seen = []
        session = None

        async def context(scope):
            evidence = session.context_input()
            assert evidence.binding == BINDING
            assert evidence.turn_id == 71 and evidence.state_revision == 2
            assert [(x.native_id, x.client_id) for x in evidence.inputs] == [
                (71, "original-client")
            ]
            assert evidence.stage == "pending_context" and not peer.committed
            with pytest.raises(TypeError):
                pickle.dumps(evidence)
            seen.append(evidence)
            await asyncio.sleep(0)
            assert session.context_input() is evidence
            return "exact original revision/digest text"

        session = await attach(resolve_context=context)
        accepted = await session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        assert accepted.native_id == 71 and accepted.client_id == "original-client"
        assert peer.committed and len(seen) == 1
        assert peer.context_replies == [
            {"appendSystemPrompt": "exact original revision/digest text"}
        ]
        with pytest.raises(HostUnavailable):
            session.context_input()
        assert [name for name, _ in peer.calls] == ["initialize", "session/load", INPUT]


@pytest.mark.parametrize("bad_state", [None, {"activeTurnId": True}, {"pending": [item(72)]}])
async def test_ambiguous_or_foreign_native_active_evidence_refuses_context(bad_state):
    peer = InputPeer(baseline=bad_state is not None, bad_state=bad_state)
    async with wire(peer) as (_peer, _authority, _host, _context, _updates, attach):
        session = None
        recorded = []

        async def context(scope):
            recorded.append(session.context_input())
            return "must not return unbound context"

        session = await attach(resolve_context=context)
        with pytest.raises((AcpOutcomeUnknown, HostUnavailable)):
            await session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        assert recorded == []
        assert sum(name == INPUT for name, _ in peer.calls) == 1
        assert all(name not in {"session/new", "session/prompt"} for name, _ in peer.calls)


async def test_lost_submission_reply_retains_original_input_request_without_replay():
    peer = InputPeer(lose_reply=True)
    async with wire(peer) as (_peer, _authority, _host, _context, _updates, attach):
        session = None

        async def context(scope):
            assert session.context_input().inputs[0].client_id == "original-client"
            return "same original pointer"

        session = await attach(resolve_context=context)
        with pytest.raises(AcpOutcomeUnknown) as error:
            await session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        assert error.value.receipt.binding == BINDING
        assert error.value.receipt.phase == "input_submit"
        assert error.value.receipt.request_id is not None
        assert error.value.receipt.client_id == "original-client"
        assert error.value.receipt.input_acceptance is None
        with pytest.raises(HostUnavailable):
            await session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        assert sum(name == INPUT for name, _ in peer.calls) == 1


async def test_native_context_evidence_is_withdrawn_during_context_read():
    peer = InputPeer()
    async with wire(peer) as (_peer, _authority, _host, _context, _updates, attach):
        session = None
        received = asyncio.Event()

        def observer(event):
            params = event.message.get("params", {})
            if params.get("update", {}).get("state", {}).get("revision") == 3:
                received.set()

        async def context(scope):
            assert session.context_input().turn_id == 71
            await peer.update({"kind": "state", "state": state(3)})
            await received.wait()
            return "stale pointer text"

        session = await attach(resolve_context=context, observers=[observer])
        with pytest.raises((AcpOutcomeUnknown, HostUnavailable)):
            await session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        assert peer.context_replies == []


async def test_delivery_path_never_falls_back_to_legacy_prompt():
    peer = InputPeer()
    async with wire(peer) as (_peer, _authority, _host, _context, _updates, attach):
        session = None

        async def context(scope):
            session.context_input()
            return "same pointer"

        session = await attach(resolve_context=context)
        await session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        with pytest.raises(HostUnavailable):
            await session.prompt("cannot bypass native input association")
        assert all(name != "session/prompt" for name, _ in peer.calls)


async def test_later_active_turn_cannot_replace_context_request_identity_during_authority_wait(
    monkeypatch,
):
    peer = InputPeer()
    async with wire(peer) as (_peer, _authority, host, _context, _updates, attach):
        arrived, entered, changed, release = (asyncio.Event() for _ in range(4))
        observed = []
        original = host.check_current
        paused = False

        def observer(event):
            if event.message.get("method") == "_sudocode/session/host_context":
                arrived.set()
            params = event.message.get("params", {})
            if params.get("update", {}).get("state", {}).get("revision") == 4:
                changed.set()

        async def check_current():
            nonlocal paused
            if arrived.is_set() and not paused:
                paused = True
                entered.set()
                await release.wait()
            await original()

        monkeypatch.setattr(host, "check_current", check_current)

        async def context(scope):
            observed.append(session.context_input())
            return "must not bind the later turn"

        session = await attach(resolve_context=context, observers=[observer])
        submitted = asyncio.create_task(
            session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        )
        await entered.wait()
        await peer.update({"kind": "state", "state": state(3)})
        await peer.update(
            {
                "kind": "state",
                "state": state(4, active=81, running=True, pending=[item(81, "later-client")]),
            }
        )
        await changed.wait()
        release.set()
        with pytest.raises(HostUnavailable):
            await submitted
        assert observed == [] and peer.context_replies == []


@pytest.mark.parametrize(
    "reply",
    [
        {},
        [],
        {"id": True, "state": state(1, pending=[item(delivery="queue")])},
        {"id": 71, "state": state(1, pending=[item(client_id="foreign-client")])},
    ],
)
async def test_uncorrelated_submission_acceptance_is_unknown_without_retry(reply):
    peer = InputPeer(bad_result=reply)
    async with wire(peer) as (_peer, _authority, _host, _context, _updates, attach):
        session = None

        async def context(scope):
            session.context_input()
            return "original pointer"

        session = await attach(resolve_context=context)
        with pytest.raises(AcpOutcomeUnknown) as error:
            await session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        assert error.value.receipt.phase == "input_result"
        assert error.value.receipt.binding == BINDING
        with pytest.raises(HostUnavailable):
            await session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        assert sum(method == INPUT for method, _ in peer.calls) == 1


async def test_real_task_cancellation_preserves_original_input_dispatch_receipt():
    peer = InputPeer()
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_before_context():
        entered.set()
        await release.wait()

    peer.during_context = wait_before_context
    async with wire(peer) as (_peer, _authority, _host, _context, _updates, attach):
        session = await attach()
        task = asyncio.create_task(
            session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        )
        await entered.wait()
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError) as error:
                await task
            assert task.cancelled()
            assert error.value.receipt.phase == "input_submit"
            assert error.value.receipt.outcome == "unknown"
            assert error.value.receipt.request_id is not None
            assert error.value.receipt.client_id == "original-client"
            with pytest.raises(HostUnavailable):
                await session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        finally:
            release.set()
        assert sum(method == INPUT for method, _ in peer.calls) == 1


async def test_stop_during_input_admission_refuses_unsent_original_request(monkeypatch):
    peer = InputPeer()
    async with wire(peer) as (_peer, _authority, host, _context, _updates, attach):
        session = await attach()
        entered, release = asyncio.Event(), asyncio.Event()
        original = host.check_ready

        async def wait_admission(operation, resource):
            if operation == "session_prompt":
                entered.set()
                await release.wait()
            await original(operation, resource)

        monkeypatch.setattr(host, "check_ready", wait_admission)
        task = asyncio.create_task(
            session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        )
        await entered.wait()
        await session.cancel()
        await peer.cancelled.wait()
        release.set()
        with pytest.raises(HostUnavailable) as error:
            await task
        assert error.value.receipt.phase == "input_admission"
        assert error.value.receipt.outcome == "refused"
        assert error.value.receipt.request_id is None
        assert all(method != INPUT for method, _ in peer.calls)


@pytest.mark.parametrize("kind", ["committed", "paused"])
async def test_committed_or_paused_state_cannot_be_recorded_as_pending_context(kind):
    peer = InputPeer()

    async def change_before_context():
        if kind == "committed":
            await peer.update(
                {
                    "kind": "committed",
                    "clientId": "original-client",
                    "source": "human",
                    "display": "same original",
                }
            )
        else:
            await peer.update(
                {
                    "kind": "state",
                    "state": {
                        **state(3, active=71, running=True, pending=[item()]),
                        "paused": True,
                    },
                }
            )

    peer.during_context = change_before_context
    async with wire(peer) as (_peer, _authority, _host, _context, _updates, attach):
        seen = []
        session = None

        async def context(scope):
            seen.append(session.context_input())
            return "must not label as pending"

        session = await attach(resolve_context=context)
        with pytest.raises(HostUnavailable):
            await session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        assert seen == [] and peer.context_replies == []


async def test_legacy_prompt_connection_cannot_be_reclassified_as_native_input_delivery():
    async with wire() as (peer, _authority, _host, _context, _updates, attach):
        session = await attach()
        await session.prompt("legacy fixture only")
        with pytest.raises(HostUnavailable):
            await session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        assert all(method != INPUT for method, _ in peer.calls)


async def test_existing_typed_mailbox_streams_carry_input_context_before_commit():
    class TypedMailboxPeer(MailboxPeer):
        # SDK append acknowledgement returns first. Native processing occurs
        # separately so reverse context replies use the same original sender.
        submitted = None

        async def native_turn(self, message):
            for update in (
                {"kind": "state", "state": state(1, pending=[item(delivery="queue")])},
                {"kind": "state", "state": state(2, active=71, running=True, pending=[item()])},
            ):
                await self.incoming.put(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "_sudocode/session/input_update",
                            "params": {
                                "sessionId": BINDING.native.durable_session_id,
                                "update": update,
                            },
                        }
                    )
                )
            await self.incoming.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "original-context",
                        "method": "_sudocode/session/host_context",
                        "params": {"sessionId": BINDING.native.durable_session_id},
                    }
                )
            )
            context = await self.responses.get()
            assert context == {
                "jsonrpc": "2.0",
                "id": "original-context",
                "result": {"appendSystemPrompt": "same model pointer 🧩"},
            }
            await self.incoming.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "_sudocode/session/input_update",
                        "params": {
                            "sessionId": BINDING.native.durable_session_id,
                            "update": {
                                "kind": "committed",
                                "clientId": "original-client",
                                "source": "human",
                                "display": "same original",
                            },
                        },
                    }
                )
            )
            await self.incoming.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"id": 71, "state": state(1, pending=[item(delivery="queue")])},
                    }
                )
            )

        async def session_operation(self, operation, payload=""):
            if operation == "send" and (message := json.loads(payload)).get("method") == INPUT:
                self.operations.append((operation, payload))
                self.requests.append(message)
                self.submitted = asyncio.create_task(self.native_turn(message))
                return ""
            return await super().session_operation(operation, payload)

    peer = TypedMailboxPeer()
    host = await resolve_trusted_host(PRINCIPAL, BINDING.selection, Authority())

    async def attachment():
        await host.check_ready("session_load", "registered-execution")

    streams = await ControllerMailboxStreams.attach(
        peer.connection, ENDPOINT, check_attachment=attachment
    )
    session = None
    seen = []

    async def context(scope):
        seen.append(session.context_input())
        return "same model pointer 🧩"

    async def update(method, value):
        return None

    recorded = []
    recorded_terminal = asyncio.Event()

    async def record_lifecycle(binding, value):
        # Typed mailbox/native custody and persistence remain fixture evidence.
        assert binding == BINDING
        recorded.append(copy.deepcopy(value))
        recorded_terminal.set()

    try:
        async with asyncio.timeout(8):
            session = await CohostAcpSession.attach(
                host,
                reader=streams.reader,
                writer=streams.writer,
                execution_cwd="/proc/812/root/owner/workspace",
                mcp_servers=[],
                resource_ref="registered-execution",
                resolve_context=context,
                on_update=update,
                on_input_lifecycle=record_lifecycle,
            )
            result = await session.submit_input("original-client", "原始输入 🧩", delivery="queue")
            assert result.native_id == 71 and result.client_id == "original-client"
            assert len(seen) == 1 and seen[0].stage == "pending_context"
            assert seen[0].inputs[0].client_id == "original-client"
            await peer.submitted
            await recorded_terminal.wait()
            assert [(row["kind"], row["clientId"]) for row in recorded] == [
                ("committed", "original-client")
            ]
            assert [name for name, _ in peer.operations].count("open") == 1
            assert [request["method"] for request in peer.requests] == [
                "initialize",
                "session/load",
                INPUT,
            ]
    finally:
        if session is not None:
            await session.close()
        await streams.close()
        if peer.submitted is not None and not peer.submitted.done():
            peer.submitted.cancel()
            await asyncio.gather(peer.submitted, return_exceptions=True)


async def test_native_acceptance_stays_private_when_current_grants_change_after_reply():
    authority = Authority()

    class RevokedPeer(InputPeer):
        async def receive(self, method, params, notification):
            result = await super().receive(method, params, notification)
            if method == INPUT:
                authority.denied = True
            return result

    peer = RevokedPeer()
    async with wire(peer, authority) as (_peer, _authority, _host, _context, _updates, attach):
        session = None

        async def context(scope):
            session.context_input()
            return "original pointer"

        session = await attach(resolve_context=context)
        with pytest.raises(AcpOutcomeUnknown) as error:
            await session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        receipt = error.value.receipt
        assert receipt.phase == "input_result" and receipt.client_id == "original-client"
        assert receipt.input_acceptance.native_id == 71
        assert receipt.input_acceptance.client_id == "original-client"
        with pytest.raises(HostUnavailable):
            await session.submit_input("original-client", "原始输入 🧩", delivery="queue")
        assert sum(method == INPUT for method, _ in peer.calls) == 1
