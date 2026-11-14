"""Official ACP sockets; lifecycle persistence/custody are explicit fixtures."""

import asyncio
import copy

import pytest
from tests.unit.infra.native_host.test_acp import BINDING, wire
from tests.unit.infra.native_host.test_input_context_recording import AcceptedPeer
from tests.unit.infra.native_host.test_input_delivery import INPUT

from octop.infra.native_host.acp import AcpRequestCancelled
from octop.infra.native_host.context import HostUnavailable

COMMITTED = {"kind": "committed", "clientId": "original-client"}
COMPLETED = {"kind": "turn_complete", "turnId": 71, "stopReason": "end_turn"}


async def test_original_terminal_recording_after_human_revoke_keeps_effects_withdrawn():
    peer = AcceptedPeer()
    async with wire(peer) as (_peer, authority, _host, _context, _updates, attach):
        recorded, human_visible = [], []
        completed = asyncio.Event()

        async def record(binding, update):
            # Actual provider must check native custody and writer. Here both
            # are explicit fixtures independent of revoked human authorization.
            assert binding == BINDING
            assert authority.denied
            recorded.append(copy.deepcopy(update))
            if update["kind"] == "turn_complete":
                completed.set()

        async def visible(method, update):
            if update.get("kind") in {"committed", "turn_complete"}:
                human_visible.append(update)

        session = await attach(on_input_lifecycle=record, on_update=visible)
        await session.submit_input("original-client", "Original", delivery="queue")
        authority.denied = True
        await peer.update(COMMITTED)
        await peer.update(COMPLETED)
        await completed.wait()
        assert recorded == [COMMITTED, COMPLETED] and human_visible == []
        with pytest.raises(HostUnavailable):
            session._check_ready()
        with pytest.raises(HostUnavailable):
            await session.submit_input("replacement", "Must refuse", delivery="queue")
        assert sum(method == INPUT for method, _ in peer.calls) == 1
        assert session.input_lifecycle_failure_receipt is None


async def test_missing_terminal_recorder_refuses_before_native_submit():
    async with wire(AcceptedPeer()) as (peer, _authority, _host, _context, _updates, attach):
        session = await attach(on_input_lifecycle=None)
        with pytest.raises(HostUnavailable, match="lifecycle recorder is required"):
            await session.submit_input("original-client", "Original", delivery="queue")
        assert sum(method == INPUT for method, _ in peer.calls) == 0


async def test_existing_terminal_recovery_never_requires_or_sends_a_new_input():
    peer = AcceptedPeer()
    async with wire(peer) as (_peer, authority, _host, _context, _updates, attach):
        recorded = []
        completed = asyncio.Event()

        async def record(binding, update):
            assert binding == BINDING
            # Real provider correlates the original persisted association and
            # native incarnation/custody; current human permission is revoked.
            assert authority.denied
            recorded.append(copy.deepcopy(update))
            if update["kind"] == "turn_complete":
                completed.set()

        session = await attach(on_input_lifecycle=record)
        authority.denied = True
        await peer.update(COMMITTED)
        await peer.update(COMPLETED)
        await completed.wait()
        assert recorded == [COMMITTED, COMPLETED]
        assert not session._native_delivery
        assert [method for method, _ in peer.calls] == ["initialize", "session/load"]
        with pytest.raises(HostUnavailable):
            session.context_input()
        with pytest.raises(HostUnavailable):
            await session.submit_input("replacement", "Must refuse", delivery="queue")


@pytest.mark.parametrize("failure", ["error", "malformed", "cancelled", "custody"])
async def test_recording_failure_retains_first_original_private_unknown(failure):
    peer = AcceptedPeer()
    async with wire(peer) as (_peer, _authority, _host, _context, _updates, attach):
        entered = asyncio.Event()

        async def record(binding, update):
            assert binding == BINDING and update == COMMITTED
            update["clientId"] = "mutated-private-callback-copy"
            entered.set()
            if failure == "cancelled":
                raise asyncio.CancelledError()
            if failure == "malformed":
                return False
            if failure == "custody":
                raise PermissionError("private wrong original native owner")
            raise RuntimeError("private possible original-row write")

        session = await attach(on_input_lifecycle=record)
        await session.submit_input("original-client", "Original", delivery="queue")
        # Directly await this owned handler to assert its precise failure type;
        # the SDK intentionally suppresses notification exceptions on the wire.
        expected = AcpRequestCancelled if failure == "cancelled" else HostUnavailable
        with pytest.raises(expected) as result:
            await session._receive(
                "_sudocode/session/input_update",
                {"sessionId": session.session_id, "update": COMMITTED},
                True,
            )
        assert entered.is_set()
        receipt = session.input_lifecycle_failure_receipt
        assert result.value.receipt is receipt
        assert receipt.outcome == "unknown" and receipt.phase == "input_lifecycle"
        assert receipt.binding == BINDING and receipt.request_id is None
        assert receipt.input_lifecycle_update == COMMITTED
        assert "private wrong original native owner" not in str(result.value)
        assert "private possible original-row write" not in str(result.value)
        assert "mutated-private-callback-copy" not in str(result.value)
        with pytest.raises(HostUnavailable):
            await session.submit_input("replacement", "Must refuse", delivery="queue")
        assert sum(method == INPUT for method, _ in peer.calls) == 1
        await session.close()
        assert session.input_lifecycle_failure_receipt is receipt


async def test_terminal_recordings_are_serialized_before_public_updates():
    peer = AcceptedPeer()
    async with wire(peer) as (_peer, _authority, _host, _context, _updates, attach):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        entries, writes, public = [], [], []

        async def record(binding, update):
            assert binding == BINDING
            entries.append(update["kind"])
            if update["kind"] == "committed":
                entered.set()
                await release.wait()
            writes.append(update["kind"])

        async def visible(method, update):
            if update.get("kind") in {"committed", "turn_complete"}:
                assert update["kind"] in writes
                public.append(update["kind"])
                if update["kind"] == "turn_complete":
                    finished.set()

        session = await attach(on_input_lifecycle=record, on_update=visible)
        await session.submit_input("original-client", "Original", delivery="queue")
        await peer.update(COMMITTED)
        await entered.wait()
        await peer.update(COMPLETED)
        assert writes == [] and entries == ["committed"] and public == []
        release.set()
        await finished.wait()
        assert entries == writes == ["committed", "turn_complete"]


async def test_actual_task_cancel_preserves_original_recording_uncertainty():
    async with wire(AcceptedPeer()) as (_peer, _authority, _host, _context, _updates, attach):
        entered = asyncio.Event()
        writes = []

        async def record(binding, update):
            assert binding == BINDING
            writes.append(copy.deepcopy(update))
            entered.set()
            await asyncio.Event().wait()

        session = await attach(on_input_lifecycle=record)
        await session.submit_input("original-client", "Original", delivery="queue")
        task = asyncio.create_task(
            session._receive(
                "_sudocode/session/input_update",
                {"sessionId": session.session_id, "update": COMMITTED},
                True,
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(AcpRequestCancelled) as cancelled:
            await task
        assert task.cancelled() and writes == [COMMITTED]
        receipt = session.input_lifecycle_failure_receipt
        assert cancelled.value.receipt is receipt and receipt.outcome == "unknown"
        assert receipt.input_lifecycle_update == COMMITTED
        await session.close()
        assert session.input_lifecycle_failure_receipt is receipt


async def test_foreign_terminal_notification_never_reaches_original_writer():
    peer = AcceptedPeer()
    async with wire(peer) as (_peer, _authority, _host, _context, _updates, attach):
        observed = []

        async def record(binding, update):
            observed.append((binding, update))

        session = await attach(on_input_lifecycle=record)
        await session.submit_input("original-client", "Original", delivery="queue")
        with pytest.raises(HostUnavailable):
            await session._receive(
                "_sudocode/session/input_update",
                {"sessionId": "foreign-durable", "update": COMMITTED},
                True,
            )
        assert observed == []
        with pytest.raises(HostUnavailable):
            session._check_ready()


async def test_terminal_lock_waiter_cancel_retains_original_unknown_and_withdraws():
    async with wire(AcceptedPeer()) as (peer, _authority, _host, _context, _updates, attach):
        entered, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
        writes = []

        async def record(binding, update):
            assert binding == BINDING
            writes.append(copy.deepcopy(update))
            entered.set()
            await release.wait()

        session = await attach(on_input_lifecycle=record)
        await session.submit_input("original-client", "Original", delivery="queue")
        original = asyncio.create_task(session._record_input_lifecycle(COMMITTED))
        await entered.wait()

        async def terminal():
            waiting.set()
            await session._record_input_lifecycle(COMPLETED)

        pending = asyncio.create_task(terminal())
        await waiting.wait()
        try:
            assert not pending.done() and writes == [COMMITTED]
            pending.cancel()
            with pytest.raises(AcpRequestCancelled) as cancelled:
                await pending
            assert pending.cancelled() and writes == [COMMITTED]
            receipt = session.input_lifecycle_failure_receipt
            assert cancelled.value.receipt is receipt and receipt.outcome == "unknown"
            assert receipt.phase == "input_lifecycle" and receipt.binding == BINDING
            assert receipt.request_id is None and receipt.input_lifecycle_update == COMPLETED
            assert not session._ready
            with pytest.raises(HostUnavailable):
                session._check_ready()
            with pytest.raises(HostUnavailable):
                await session.submit_input("replacement", "Must refuse", delivery="queue")
            assert sum(method == INPUT for method, _ in peer.calls) == 1
        finally:
            release.set()
            await asyncio.gather(original, return_exceptions=True)
        assert session.input_lifecycle_failure_receipt is receipt
        assert writes == [COMMITTED]
        await session.close()
        assert session.input_lifecycle_failure_receipt is receipt
