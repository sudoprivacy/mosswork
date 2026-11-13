"""Private controller IPC to the shared official Nexus SDK implementation.

This starts a trusted controller component, never a native Agent or ACP engine.
The native service remains the identity/session/effect authority. Closing this
transport cannot establish that a native request or writer stopped.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import copy
import json
import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .context import HostUnavailable, _require_completion, _required_strings

_MAX_FRAME_BYTES = 8 * 1024 * 1024
_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
_MAX_CALLS = 16
_CONTROLLER_OPERATIONS = frozenset(
    {"call", "session.open", "session.send", "session.receive", "session.close", "file.read"}
)

ConnectAssertion = Callable[[], Awaitable[None]]
DispatchAssertion = Callable[[str, str], Awaitable[None]]


class ControllerSdkError(HostUnavailable):
    """Public-safe diagnostic with a controller-only original native verdict."""

    def __init__(
        self,
        code: str,
        *,
        is_outcome_uncertain: bool,
        native_failure: dict[str, str | int] | None = None,
    ) -> None:
        super().__init__(code)
        self.code, self.is_outcome_uncertain = code, is_outcome_uncertain
        self._native_failure = copy.deepcopy(native_failure)

    @property
    def native_failure(self) -> dict[str, str | int] | None:
        """Original structured native error for trusted exact recovery only.

        Never serialize this into public errors or logs: native messages can
        contain private paths. Exception text/repr exposes only the stable code.
        """
        return copy.deepcopy(self._native_failure)


@dataclass(frozen=True, slots=True)
class ControllerSdkCommand:
    """Trusted deployment's verified Node executable and shared package entry."""

    node_executable: Path
    controller_entry: Path

    def __post_init__(self) -> None:
        for path in (self.node_executable, self.controller_entry):
            if not path.is_absolute() or any(char in str(path) for char in "\x00\r\n"):
                raise ValueError("Require trusted absolute controller artifact paths")


@dataclass(frozen=True, slots=True, repr=False)
class ControllerSdkConfiguration:
    """Existing authenticated owner and issuer/placement, never browser metadata."""

    owner_id: str
    issuer_endpoint: str
    ca_file: Path
    cert_file: Path
    key_file: Path
    runtime_endpoint: str | None = None
    server_name: str | None = None
    connect_timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        values = [self.owner_id, self.issuer_endpoint]
        values.extend(
            value for value in (self.runtime_endpoint, self.server_name) if value is not None
        )
        _required_strings(*values)
        if any(any(char in value for char in "\r\n") for value in values):
            raise ValueError("Invalid controller binding")
        for path in (self.ca_file, self.cert_file, self.key_file):
            if not path.is_absolute() or any(char in str(path) for char in "\x00\r\n"):
                raise ValueError("Require trusted existing issuer PEM paths")
        if type(self.connect_timeout_ms) is not int or not 1 <= self.connect_timeout_ms <= 120_000:
            raise ValueError("Invalid controller connection timeout")

    def bootstrap(self) -> dict[str, object]:
        tls = {"ca": str(self.ca_file), "cert": str(self.cert_file), "key": str(self.key_file)}
        if self.server_name is not None:
            tls["serverName"] = self.server_name
        config: dict[str, object] = {"endpoint": self.issuer_endpoint, "tls": tls}
        if self.runtime_endpoint is not None:
            config["runtimeEndpoint"] = self.runtime_endpoint
        return {
            "type": "connect",
            "version": 1,
            "ownerId": self.owner_id,
            "config": config,
            "connectTimeoutMs": self.connect_timeout_ms,
        }


class _Frame(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class _Ready(_Frame):
    type: Literal["ready"]
    version: Literal[1]

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported controller protocol")
        return value


class _Result(_Frame):
    type: Literal["result"]
    id: int = Field(ge=1, le=2**53 - 1)
    payload: str


class _Capabilities(_Frame):
    type: Literal["capabilities"]
    version: Literal[1]
    operations: list[str] = Field(max_length=len(_CONTROLLER_OPERATIONS))

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        return _Ready.exact_version(value)

    @field_validator("operations")
    @classmethod
    def exact_operations(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or not set(value) <= _CONTROLLER_OPERATIONS:
            raise ValueError("Invalid controller operations")
        return value


class _NativeFailure(_Frame):
    code: int | str
    message: str = Field(max_length=8192)


class _Error(_Frame):
    type: Literal["error"]
    id: int = Field(ge=1, le=2**53 - 1)
    code: Literal[
        "SDK_CALL_FAILED",
        "SDK_SESSION_FAILED",
        "SDK_READ_FAILED",
        "LIMIT_REACHED",
        "RESULT_UNAVAILABLE",
    ]
    isOutcomeUncertain: bool
    nativeFailure: _NativeFailure | None = None

    @model_validator(mode="after")
    def conservative_outcome(self) -> _Error:
        if self.code != "LIMIT_REACHED" and not self.isOutcomeUncertain:
            raise ValueError("Native call failure cannot establish noncommit")
        return self


class _Failed(_Frame):
    type: Literal["failed"]
    code: Literal["CONNECT_FAILED", "INVALID_PROTOCOL"]


_RESPONSE: TypeAdapter[_Ready | _Capabilities | _Result | _Error | _Failed] = TypeAdapter(
    Annotated[_Ready | _Capabilities | _Result | _Error | _Failed, Field(discriminator="type")]
)


def _decode(line: bytes) -> _Ready | _Capabilities | _Result | _Error | _Failed:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate controller response field")
            result[key] = value
        return result

    def invalid_constant(_value: str) -> None:
        raise ValueError("Invalid JSON constant")

    return _RESPONSE.validate_python(
        json.loads(line.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)
    )


def _encode(value: dict[str, object]) -> bytes:
    frame = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    if len(frame) > _MAX_FRAME_BYTES:
        raise ValueError("Controller frame exceeds limit")
    return frame


class ControllerSdkConnection:
    """One fixed owner's official SDK, with bounded private call correlation.

    Correlation IDs never replace native operation/session/delivery identities.
    Required connect/dispatch assertions consume current trusted app authority;
    native services still enforce original ownership and atomic effect fences.
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        before_dispatch: DispatchAssertion,
        rpc_timeout_seconds: float,
        requires_probe: bool = False,
    ) -> None:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise ValueError("Require private controller pipes")
        self._process, self._stdin, self._stdout = process, process.stdin, process.stdout
        self._before_dispatch, self._rpc_timeout_seconds = before_dispatch, rpc_timeout_seconds
        self._pending: dict[int, asyncio.Future[str]] = {}
        self._slots = asyncio.Semaphore(_MAX_CALLS)
        self._write_lock, self._close_lock = asyncio.Lock(), asyncio.Lock()
        self._session_write_lock = asyncio.Lock()
        self._session_write_unknown = False
        self._next_id = 0
        self._is_closed = False
        self._bootstrap_sent = False
        self._supported_operations: frozenset[str] = frozenset()
        self._capabilities: asyncio.Future[frozenset[str]] | None = (
            asyncio.get_running_loop().create_future() if requires_probe else None
        )
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._reader_task = asyncio.create_task(self._read_responses())
        self._stderr_task = asyncio.create_task(self._discard_stderr(process.stderr))
        self._cleanup_task: asyncio.Task[None] | None = None

    @classmethod
    async def connect(
        cls,
        command: ControllerSdkCommand,
        configuration: ControllerSdkConfiguration,
        *,
        before_connect: ConnectAssertion,
        before_dispatch: DispatchAssertion,
        rpc_timeout_seconds: float | None = None,
        required_operations: frozenset[str] = frozenset(),
    ) -> ControllerSdkConnection:
        """Authorize before process/credential access and recheck after minting.

        The caller verifies the complete Node/package artifact, resolves the
        original owner/issuer/placement and injects current assertion providers.
        No default authority, alternate issuer or direct-model path exists.
        Requiring transport operations probes the selected component before
        sending configuration/PEM paths, then rechecks current connection access.
        This metadata is neither native readiness nor an operation grant.
        """
        if (
            type(required_operations) is not frozenset
            or not required_operations <= _CONTROLLER_OPERATIONS
        ):
            raise ValueError("Invalid required controller operations")
        timeout = (
            rpc_timeout_seconds
            if rpc_timeout_seconds is not None
            else configuration.connect_timeout_ms / 1000 + 5
        )
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Require a bounded controller RPC timeout")
        try:
            await _require_completion(before_connect())
        except PermissionError:
            raise PermissionError("Controller SDK connection access denied") from None
        except Exception:
            raise ControllerSdkError(
                "CONNECT_AUTHORIZATION_UNAVAILABLE", is_outcome_uncertain=False
            ) from None
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL"}
        }
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                str(command.node_executable),
                str(command.controller_entry),
                cwd=command.controller_entry.parent,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                limit=_MAX_FRAME_BYTES + 1,
            )
        )
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            abandoned = cls(process, before_dispatch=before_dispatch, rpc_timeout_seconds=timeout)
            await abandoned.close()
            raise
        except Exception:
            raise ControllerSdkError("CONNECT_UNAVAILABLE", is_outcome_uncertain=False) from None
        connection = cls(
            process,
            before_dispatch=before_dispatch,
            rpc_timeout_seconds=timeout,
            requires_probe=bool(required_operations),
        )
        try:
            if connection._capabilities is not None:
                connection._stdin.write(_encode({"type": "capabilities"}))
                await connection._stdin.drain()
                supported = await asyncio.wait_for(
                    asyncio.shield(connection._capabilities), timeout
                )
                if not required_operations <= supported:
                    raise ValueError("Selected controller lacks required operations")
                await _require_completion(before_connect())
                connection._check_open()
            connection._bootstrap_sent = True
            connection._stdin.write(_encode(configuration.bootstrap()))
            await connection._stdin.drain()
            await asyncio.wait_for(
                asyncio.shield(connection._ready), configuration.connect_timeout_ms / 1000 + 5
            )
            await _require_completion(before_connect())
            connection._check_open()
            return connection
        except asyncio.CancelledError:
            await connection.close()
            raise
        except PermissionError:
            await connection.close()
            raise PermissionError("Controller SDK connection access denied") from None
        except Exception:
            await connection.close()
            raise ControllerSdkError(
                "CONNECT_UNAVAILABLE", is_outcome_uncertain=connection._bootstrap_sent
            ) from None

    def _check_open(self) -> None:
        if self._is_closed or self._process.returncode is not None:
            raise ControllerSdkError("CONNECTION_UNAVAILABLE", is_outcome_uncertain=False)

    def _invalidate(self) -> None:
        self._is_closed = True
        if self._capabilities is not None:
            if not self._capabilities.done():
                self._capabilities.set_exception(
                    ControllerSdkError("CONNECT_UNAVAILABLE", is_outcome_uncertain=False)
                )
            if not self._capabilities.cancelled():
                self._capabilities.exception()
        if not self._ready.done():
            self._ready.set_exception(
                ControllerSdkError("CONNECT_UNAVAILABLE", is_outcome_uncertain=True)
            )
        # Observe orphaned/cancelled callers' futures without logging payloads.
        if not self._ready.cancelled():
            self._ready.exception()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(
                    ControllerSdkError("OUTCOME_UNKNOWN", is_outcome_uncertain=True)
                )
        self._pending.clear()

    async def _read_responses(self) -> None:
        try:
            while line := await self._stdout.readline():
                if self._is_closed:
                    continue
                if len(line) > _MAX_FRAME_BYTES or not line.endswith(b"\n"):
                    raise ValueError("Invalid controller response framing")
                response = _decode(line)
                if isinstance(response, _Capabilities):
                    if (
                        self._capabilities is None
                        or self._capabilities.done()
                        or self._bootstrap_sent
                    ):
                        raise ValueError("Unexpected controller capabilities")
                    self._supported_operations = frozenset(response.operations)
                    self._capabilities.set_result(self._supported_operations)
                    continue
                if isinstance(response, _Ready):
                    if not self._bootstrap_sent or self._ready.done():
                        raise ValueError("Controller cannot rebind")
                    self._ready.set_result(None)
                    continue
                if isinstance(response, _Failed) or not self._ready.done():
                    raise ValueError("Controller unavailable")
                future = self._pending.pop(response.id, None)
                if future is None:
                    raise ValueError("Unknown controller correlation")
                if isinstance(response, _Result):
                    future.set_result(response.payload)
                else:
                    native_failure = (
                        {
                            "code": response.nativeFailure.code,
                            "message": response.nativeFailure.message,
                        }
                        if response.nativeFailure is not None
                        else None
                    )
                    future.set_exception(
                        ControllerSdkError(
                            response.code,
                            is_outcome_uncertain=response.isOutcomeUncertain,
                            native_failure=native_failure,
                        )
                    )
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            self._invalidate()
            if self._cleanup_task is None:
                self._cleanup_task = asyncio.create_task(self._close_resources())

    @staticmethod
    async def _discard_stderr(reader: asyncio.StreamReader) -> None:
        # Drain continuously so diagnostics cannot block IPC. Child/provider
        # diagnostics may include private details and are never forwarded.
        while await reader.read(4096):
            pass

    def _completed(self, future: asyncio.Future[str]) -> None:
        self._slots.release()
        if not future.cancelled():
            future.exception()

    async def call(self, method: str, payload: str, auth_token: str) -> str:
        """Call once with empty bearer; retain uncertain submitted operations.

        Current dispatch authorization runs after local capacity/lock waits.
        Native atomic fences remain required. Cancellation or timeout after
        submission does not cancel a native effect and cannot authorize replay.
        """
        if auth_token != "":
            raise ValueError("Controller runtime cannot override delegated mTLS")
        _required_strings(method)
        if len(method) > 512 or any(char in method for char in "\r\n"):
            raise ValueError("Invalid native method")
        return await self._request("call", "method", method, payload, method)

    async def read_file(self, path: str) -> bytes:
        """Read one trusted original managed file using the same official SDK.

        The required file.read probe must have succeeded before bootstrap.
        Current authorization receives (controller.file.read, unchanged path)
        after capacity/lock waits and again before exposing returned bytes.
        Empty bytes and read errors never establish native session absence;
        observing a file cannot clear an uncertain append or authorize replay.
        """
        _required_strings(path)
        if len(path.encode("utf-16-le")) > 8192 or any(char in path for char in "\r\n"):
            raise ValueError("Invalid managed file path")
        if "file.read" not in self._supported_operations:
            raise ControllerSdkError("READ_UNAVAILABLE", is_outcome_uncertain=False)
        payload = await self._request("read", "path", path, path, "controller.file.read")
        try:
            if len(payload) > 4 * ((_MAX_PAYLOAD_BYTES + 2) // 3):
                raise ValueError("Read result exceeds limit")
            result = base64.b64decode(payload, validate=True)
            if (
                len(result) > _MAX_PAYLOAD_BYTES
                or base64.b64encode(result).decode("ascii") != payload
            ):
                raise ValueError("Invalid read result")
        except (ValueError, binascii.Error):
            self._invalidate()
            if self._cleanup_task is None:
                self._cleanup_task = asyncio.create_task(self._close_resources())
            raise ControllerSdkError("RESULT_UNAVAILABLE", is_outcome_uncertain=True) from None
        self._check_open()
        await self._assert_dispatch("controller.file.read", path)
        self._check_open()
        return result

    async def session_operation(
        self, operation: Literal["open", "send", "receive", "close"], payload: str = ""
    ) -> str:
        """Use the same controller's official SDK mailbox, never generic Call.

        DispatchAssertion receives the private label controller.session.<op>
        and the unchanged JSON payload. It must recheck actual original host
        authority; this label is neither a native RPC nor an operation grant.
        A send serializes through its acknowledgment so the assertion runs
        after prior write waits. Uncertain/cancelled sends latch withdrawal;
        receiving original replies or closing does not authorize another send.
        """
        if operation not in {"open", "send", "receive", "close"}:
            raise ValueError("Invalid controller session operation")
        if operation in {"receive", "close"} and payload != "":
            raise ValueError("Controller session poll/close require empty payload")

        async def dispatch() -> str:
            if operation in {"open", "send"} and self._session_write_unknown:
                raise ControllerSdkError("OUTCOME_UNKNOWN", is_outcome_uncertain=True)
            try:
                return await self._request(
                    "session", "operation", operation, payload, "controller.session." + operation
                )
            except ControllerSdkError as error:
                if operation in {"open", "send"} and error.is_outcome_uncertain:
                    self._session_write_unknown = True
                raise
            except asyncio.CancelledError:
                if operation in {"open", "send"}:
                    self._session_write_unknown = True
                raise

        if operation in {"open", "send"}:
            async with self._session_write_lock:
                return await dispatch()
        return await dispatch()

    async def _request(
        self,
        frame_type: Literal["call", "session", "read"],
        operation_key: Literal["method", "operation", "path"],
        operation: str,
        payload: str,
        assertion_method: str,
    ) -> str:
        if not isinstance(payload, str) or len(payload.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise ValueError("Invalid native payload")
        self._check_open()
        await self._slots.acquire()
        is_owned_by_future = False
        try:
            async with self._write_lock:
                self._check_open()
                await self._assert_dispatch(assertion_method, payload)
                self._check_open()
                self._next_id += 1
                if self._next_id > 2**53 - 1:
                    raise ControllerSdkError("CORRELATION_EXHAUSTED", is_outcome_uncertain=False)
                frame = _encode(
                    {
                        "type": frame_type,
                        "id": self._next_id,
                        operation_key: operation,
                        **({"payload": payload} if frame_type != "read" else {}),
                    }
                )
                future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
                future.add_done_callback(self._completed)
                self._pending[self._next_id] = future
                is_owned_by_future = True
                try:
                    self._stdin.write(frame)
                    await self._stdin.drain()
                except (ConnectionError, OSError):
                    self._invalidate()
                    await self.close()
                    raise ControllerSdkError("OUTCOME_UNKNOWN", is_outcome_uncertain=True) from None
            try:
                return await asyncio.wait_for(asyncio.shield(future), self._rpc_timeout_seconds)
            except TimeoutError:
                raise ControllerSdkError("OUTCOME_UNKNOWN", is_outcome_uncertain=True) from None
        finally:
            if not is_owned_by_future:
                self._slots.release()

    async def _assert_dispatch(self, operation: str, payload: str) -> None:
        try:
            await _require_completion(self._before_dispatch(operation, payload))
        except PermissionError:
            raise PermissionError("Current controller operation denied") from None
        except Exception:
            raise ControllerSdkError("DISPATCH_UNAVAILABLE", is_outcome_uncertain=False) from None

    async def close(self) -> None:
        """Close this controller transport only; native cleanup remains Unknown."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._close_resources())
        await asyncio.shield(self._cleanup_task)

    async def _close_resources(self) -> None:
        async with self._close_lock:
            self._invalidate()
            self._stdin.close()
            is_pipe_blocked = False
            try:
                await asyncio.wait_for(self._stdin.wait_closed(), 5)
            except (ConnectionError, OSError):
                pass
            except TimeoutError:
                is_pipe_blocked = True
            try:
                if is_pipe_blocked and self._process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        self._process.terminate()
                await asyncio.wait_for(self._process.wait(), 5)
            except TimeoutError:
                # Only this owned SDK component is stopped, never an Agent or
                # another session. Native effects retain their original custody.
                with contextlib.suppress(ProcessLookupError):
                    self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), 2)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        self._process.kill()
                    await self._process.wait()
            for task in (self._reader_task, self._stderr_task):
                if task is not asyncio.current_task():
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
