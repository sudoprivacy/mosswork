import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from moss_browser.cdp.client import CDPClient, CDPSessionError
from moss_browser.idle import BrowserIdleController, idle_controller_for
from moss_browser.settings import MossBrowserSettings


def _profile(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(name="test", data_dir=tmp_path / "test", cdp_port=9222)


def test_idle_controller_disabled_by_default(tmp_path):
    assert idle_controller_for(_profile(tmp_path), MossBrowserSettings()) is None


def test_idle_controller_disabled_for_remote_cdp(tmp_path):
    cfg = MossBrowserSettings(idle_timeout_minutes=30, cdp_host="browser.example")
    assert idle_controller_for(_profile(tmp_path), cfg) is None


async def test_idle_controller_reaps_only_after_completed_inactivity(tmp_path):
    profile = _profile(tmp_path)
    cfg = MossBrowserSettings(idle_timeout_minutes=30)
    key = (profile.data_dir.resolve(), cfg.cdp_host, profile.cdp_port)
    controller = BrowserIdleController(
        profile,
        cfg,
        timeout_seconds=60,
        key=key,
    )
    controller._last_activity -= 61

    controller.activity_started()
    with patch(
        "moss_browser.cdp.launcher.terminate_browser",
        new=AsyncMock(return_value=True),
    ) as terminate:
        assert await controller._check_once() is False
        controller.activity_finished()
        controller._last_activity -= 61
        assert await controller._check_once() is True

    terminate.assert_awaited_once_with(profile, cdp_host="localhost")


async def test_idle_reap_drops_registry_and_cancels_watchdog(tmp_path):
    from moss_browser import tool_interface as tool_interface_mod

    profile = _profile(tmp_path)
    cfg = MossBrowserSettings(idle_timeout_minutes=30)
    key = (profile.data_dir.resolve(), cfg.cdp_host, profile.cdp_port)
    controller = BrowserIdleController(
        profile,
        cfg,
        timeout_seconds=60,
        key=key,
    )
    sess = SimpleNamespace(close=AsyncMock())
    tool_interface_mod._registry[profile.name] = sess
    watchdog = asyncio.create_task(asyncio.sleep(3600))
    controller._task = watchdog

    try:
        with patch(
            "moss_browser.cdp.launcher.terminate_browser",
            new=AsyncMock(return_value=True),
        ):
            assert await controller.reap_now() is True
        sess.close.assert_awaited_once()
        assert profile.name not in tool_interface_mod._registry
        try:
            await asyncio.wait_for(watchdog, timeout=1)
        except asyncio.CancelledError:
            pass
        assert watchdog.cancelled()
    finally:
        tool_interface_mod._registry.pop(profile.name, None)
        if not watchdog.done():
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass


async def test_cdp_client_tracks_real_but_not_passive_commands():
    events: list[str] = []

    class Tracker:
        def activity_started(self) -> None:
            events.append("start")

        def activity_finished(self) -> None:
            events.append("finish")

    client = CDPClient(activity_tracker=Tracker())

    class FakeWebSocket:
        async def send(self, payload: str) -> None:
            msg_id = client._id
            client._pending[msg_id].set_result({"id": msg_id, "result": {}})

    client._ws = FakeWebSocket()  # type: ignore[assignment]
    await client.send("Runtime.evaluate")
    await client.send_passive("Runtime.evaluate")
    assert events == ["start", "finish"]


async def test_cdp_client_wraps_disconnected_send():
    client = CDPClient()

    class BrokenWebSocket:
        async def send(self, payload: str) -> None:
            raise RuntimeError("closed")

    client._ws = BrokenWebSocket()  # type: ignore[assignment]
    try:
        await client.send("Runtime.evaluate")
    except CDPSessionError as exc:
        assert "CDP send failed" in str(exc)
    else:
        raise AssertionError("expected CDPSessionError")


class _ClosingSocket:
    def __init__(self):
        self.sent = asyncio.Event()
        self.messages = asyncio.Queue()
        self.calls = 0

    async def send(self, payload):
        self.calls += 1
        self.sent.set()

    async def close(self):
        await self.messages.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        if isinstance(message, Exception):
            raise message
        return message


@pytest.mark.parametrize("ending", ["close", "eof", "receiver_cancel", "failure"])
async def test_cdp_receiver_end_refuses_original_pending_command_without_replay(ending):
    client = CDPClient(timeout=60)
    socket = _ClosingSocket()
    client._ws = socket
    receiver = asyncio.create_task(client._recv_loop())
    client._recv_task = receiver
    command = asyncio.create_task(client.send("Runtime.evaluate"))
    await socket.sent.wait()
    try:
        if ending == "close":
            await client.close()
        elif ending == "receiver_cancel":
            receiver.cancel()
            with pytest.raises(asyncio.CancelledError):
                await receiver
            assert receiver.cancelled()
        else:
            await socket.messages.put(
                RuntimeError("fixture transport failure")
                if ending == "failure"
                else None
            )
            await receiver
        for _ in range(3):
            await asyncio.sleep(0)
        assert command.done(), (
            "A finished receiver must not leave its original command blocked"
        )
        with pytest.raises(CDPSessionError):
            await command
        assert client._pending == {}
        assert socket.calls == 1
    finally:
        command.cancel()
        with contextlib.suppress(asyncio.CancelledError, CDPSessionError):
            await command
        await client.close()


async def test_cdp_close_preserves_true_sender_cancellation_and_releases_pending_entry():
    client = CDPClient(timeout=60)
    socket = _ClosingSocket()
    client._ws = socket
    client._recv_task = asyncio.create_task(client._recv_loop())
    command = asyncio.create_task(client.send("Runtime.evaluate"))
    await socket.sent.wait()
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    await client.close()
    assert command.cancelled()
    assert client._pending == {}
    assert socket.calls == 1


async def test_cdp_close_keeps_a_completed_original_reply():
    client = CDPClient(timeout=60)
    socket = _ClosingSocket()
    client._ws = socket
    client._recv_task = asyncio.create_task(client._recv_loop())
    command = asyncio.create_task(client.send("Runtime.evaluate"))
    await socket.sent.wait()
    await socket.messages.put(json.dumps({"id": 1, "result": {"original": True}}))
    assert await command == {"original": True}
    await client.close()
    assert client._pending == {}
    assert socket.calls == 1


async def test_cdp_close_before_receiver_starts_does_not_raise():
    class ClosingSocket(_ClosingSocket):
        async def close(self):
            await asyncio.sleep(0)
            await super().close()

    client = CDPClient()
    socket = ClosingSocket()
    client._ws = socket
    client._recv_task = asyncio.create_task(client._recv_loop())
    await client.close()
    assert client._recv_task.done()
    assert client._pending == {}
    assert socket.calls == 0
