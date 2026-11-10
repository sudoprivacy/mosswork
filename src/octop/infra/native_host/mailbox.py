"""Public asyncio streams over the original official SDK session mailbox.

The socket pair is private controller framing only, with no listener or native
process/identity. Nexus's official SessionCodec and the official ACP parser
retain their respective sequencing, correlation and error responsibilities.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from dataclasses import asdict
from typing import Any

from .cohost import CohostSessionEndpoint
from .context import HostUnavailable, _require_completion
from .sdk import ConnectAssertion, ControllerSdkConnection, ControllerSdkError

_MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class MailboxOutcomeUnknown(HostUnavailable):
    """Original transport withdrawn; no conclusion about native input/cleanup."""


def _message(raw: bytes | str) -> bytes:
    def reject_constant(_value: str) -> None:
        raise ValueError("Invalid controller message")

    value: Any = json.loads(raw, parse_constant=reject_constant)
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise ValueError("Require an original JSON-RPC message")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > _MAX_MESSAGE_BYTES:
        raise ValueError("Controller message exceeds the original payload limit")
    return encoded


class ControllerMailboxStreams:
    """Borrow one fixed original SDK connection for PC3's public ACP streams.

    The caller retains the actual owner/placement, original native allocation,
    grant/admission and cleanup authority. check_attachment must assert that
    current preparation before and after open, without requiring this pending
    ACP initialize/load to have completed. SDK before_dispatch rechecks each
    operation after local waits. No default assertion/provider is supplied.

    Local socket drain does not acknowledge a native append/input. Only the
    original ACP/native receipt can do that; a failed bridge withdraws streams
    and retains Unknown. This class never reconnects, creates or cancels an Agent.
    """

    def __init__(
        self,
        connection: ControllerSdkConnection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        bridge_reader: asyncio.StreamReader,
        bridge_writer: asyncio.StreamWriter,
    ) -> None:
        self.reader, self.writer = reader, writer
        self._connection = connection
        self._bridge_reader, self._bridge_writer = bridge_reader, bridge_writer
        self._tasks: list[asyncio.Task[None]] = []
        self._closing = False
        self._first_failure: MailboxOutcomeUnknown | None = None
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    async def attach(
        cls,
        connection: ControllerSdkConnection,
        endpoint: CohostSessionEndpoint,
        *,
        check_attachment: ConnectAssertion,
    ) -> ControllerMailboxStreams:
        """Open the published endpoint through the same original controller.

        Endpoint comes from trusted native selection. The shared SDK carrier
        additionally checks its controller against the original delegated
        subject. Minting a new subject cannot adopt another live attachment.
        """
        if type(endpoint) is not CohostSessionEndpoint:
            raise ValueError("Require the original selected session endpoint")
        selected = json.dumps({"protocol": "acp-mailbox/1", **asdict(endpoint)})
        await _require_completion(check_attachment())
        await connection.session_operation("open", selected)
        pair: tuple[socket.socket, socket.socket] | None = None
        writers: list[asyncio.StreamWriter] = []
        try:
            pair = socket.socketpair()
            reader, writer = await asyncio.open_connection(sock=pair[0], limit=_MAX_MESSAGE_BYTES)
            writers.append(writer)
            incoming, outgoing = await asyncio.open_connection(
                sock=pair[1], limit=_MAX_MESSAGE_BYTES
            )
            writers.append(outgoing)
            await _require_completion(check_attachment())
            self = cls(connection, reader, writer, incoming, outgoing)
            self._tasks = [
                asyncio.create_task(self._send(), name="mosswork.mailbox.send"),
                asyncio.create_task(self._receive(), name="mosswork.mailbox.receive"),
            ]
            return self
        except BaseException:
            # Preserve the original failure/cancellation. Mailbox close is a
            # transport cleanup attempt, never a joined native writer receipt.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await connection.session_operation("close")
            for writer in writers:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    writer.close()
                    await writer.wait_closed()
            if pair is not None:
                for local in pair:
                    with contextlib.suppress(Exception):
                        local.close()
            raise

    @property
    def failure(self) -> MailboxOutcomeUnknown | None:
        """Public-safe first failure; no native secrets/payloads are exposed."""
        return self._first_failure

    def _fail(self) -> None:
        if self._closing:
            return
        if self._first_failure is None:
            self._first_failure = MailboxOutcomeUnknown("Original mailbox outcome is unknown")
        self.reader.set_exception(self._first_failure)
        self.writer.close()
        self._bridge_writer.close()
        for task in self._tasks:
            if task is not asyncio.current_task():
                task.cancel()

    async def _send(self) -> None:
        try:
            while not self._closing:
                line = await self._bridge_reader.readline()
                if not line:
                    raise ConnectionError("Local ACP stream closed")
                if len(line) > _MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                    raise ValueError("Invalid local ACP frame")
                payload = _message(line).decode("utf-8")
                await self._connection.session_operation("send", payload)
        except asyncio.CancelledError:
            if not self._closing:
                self._fail()
            raise
        except Exception:
            self._fail()

    async def _receive(self) -> None:
        try:
            while not self._closing:
                payload = await self._connection.session_operation("receive")
                if not payload:
                    continue  # Empty poll is not EOF or a native receipt.
                self._bridge_writer.write(_message(payload) + b"\n")
                await self._bridge_writer.drain()
        except asyncio.CancelledError:
            if not self._closing:
                self._fail()
            raise
        except Exception:
            self._fail()

    async def close(self) -> None:
        """Close this mailbox framing only, preserving first uncertain failure.

        The manager still owns the borrowed SDK connection, native lifecycle and
        authoritative writer/receiver reconciliation. No stopped token results.
        """
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        self._closing = True
        failed = False
        try:
            await self._connection.session_operation("close")
        except (ControllerSdkError, PermissionError):
            failed = True
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            for writer in (self.writer, self._bridge_writer):
                writer.close()
                with contextlib.suppress(ConnectionError, OSError):
                    await writer.wait_closed()
        if failed:
            if self._first_failure is None:
                self._first_failure = MailboxOutcomeUnknown("Original mailbox close is unconfirmed")
            raise self._first_failure
