"""Strict ACP consumer for one already authorized, original cohost connection.

The caller supplies the original transport and execution cwd. This module does
not spawn an engine, create a session, acquire credentials or own native cleanup.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, NoReturn, SupportsIndex

from acp.connection import Connection, StreamDirection, StreamEvent
from acp.exceptions import RequestError

from octop import __version__

from .context import (
    BackendHostContextResolver,
    HostBinding,
    HostUnavailable,
    TrustedHostContext,
    _require_completion,
)
from .inputs import NativeInputContext, NativeInputSubmission, OrderedInputProjection

if TYPE_CHECKING:
    from octop.infra.db.repos.retained_ontology import RetainedOntologyBinding
    from octop.infra.sudograph.protocol import ModelPointer

    from .sudograph import ContextRecorder

UpdateSink = Callable[[str, dict[str, Any]], Awaitable[None]]
InputLifecycleSink = Callable[[HostBinding, dict[str, Any]], Awaitable[None]]
_INPUT_METHOD = "_sudocode/session/input"


@dataclass(frozen=True, slots=True, repr=False)
class AcpRejection:
    """Original correlated RPC rejection, retained only inside the controller."""

    code: int
    message: str
    data: Any


@dataclass(frozen=True, slots=True, repr=False)
class AcpFailureReceipt:
    """Private failure evidence, never a native completion or absence proof.

    Rejected means an RPC error reply was received, not that no effects occurred.
    Unknown retains the original manager's custody. Even an exact missing-session
    error needs the authenticated provider's binding and correlation checks.
    """

    binding: HostBinding
    phase: str
    outcome: Literal["refused", "rejected", "unknown"]
    request_id: str | int | None
    rejection: AcpRejection | None
    error: BaseException
    local_close_error: BaseException | None = None
    client_id: str | None = None
    input_acceptance: NativeInputSubmission | None = None
    input_context: NativeInputContext | None = None
    input_lifecycle_update: dict[str, Any] | None = None

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("ACP failure receipts are controller-private")


class AcpRequestFailed(HostUnavailable):
    """Sanitized public error with explicit private evidence for its owner."""

    def __init__(self, message: str, receipt: AcpFailureReceipt) -> None:
        super().__init__(message)
        self.receipt = receipt


class AcpOutcomeUnknown(AcpRequestFailed):
    """The request may have reached the original engine; do not replay it."""


class AcpRequestCancelled(asyncio.CancelledError):
    """Keep task cancellation while retaining the original uncertain operation."""

    def __init__(self, receipt: AcpFailureReceipt) -> None:
        super().__init__("Original ACP operation cancelled; retain its private receipt")
        self.receipt = receipt


class CohostAcpSession:
    """Attach only by exact durable ID, with current grants on each operation.

    Stream ownership/authentication and atomic native fences belong to the
    injected shared runtime. A successful load is not evidence of those fences.
    """

    def __init__(
        self,
        host: TrustedHostContext,
        resource_ref: str,
        resolve_context: BackendHostContextResolver,
        on_update: UpdateSink,
        on_input_lifecycle: InputLifecycleSink | None = None,
    ) -> None:
        if not resource_ref:
            raise ValueError("Require a registered execution resource")
        self._host, self._resource_ref = host, resource_ref
        self._resolve_context, self._on_update = resolve_context, on_update
        self._on_input_lifecycle = on_input_lifecycle
        self._input_lifecycle_lock = asyncio.Lock()
        self._input_lifecycle_failure_receipt: AcpFailureReceipt | None = None
        self._connection: Connection | None = None
        self._reader: asyncio.StreamReader | None = None
        self._ready = False
        self._closed = False
        self._input_epoch = 0
        self._prompt_lock = asyncio.Lock()
        self._request_ids: dict[str, str | int] = {}
        self._native_delivery = False
        self._legacy_prompt_used = False
        self._inputs = OrderedInputProjection(host.binding)
        self._context_input: ContextVar[NativeInputContext | None] = ContextVar(
            "original_native_context_input", default=None
        )
        self._pending_context_input: NativeInputContext | None = None
        self._pending_context_request_id: str | int | None = None
        self._context_failure_receipt: AcpFailureReceipt | None = None
        self._context_active = False

    def _observe_request(self, event: StreamEvent) -> None:
        # Official ACP still owns parsing and response correlation. Its public
        # observer exposes the actual outgoing ID without retaining prompt data.
        if event.direction == StreamDirection.OUTGOING:
            method, request_id = event.message.get("method"), event.message.get("id")
            if (
                isinstance(method, str)
                and isinstance(request_id, (str, int))
                and not isinstance(request_id, bool)
            ):
                self._request_ids[method] = request_id
        elif (
            self._native_delivery
            and event.message.get("method") == "_sudocode/session/input_update"
        ):
            # Observe the public SDK's parsed frames synchronously in wire
            # order. Awaiting notification handlers can reorder completion.
            # This projection does not replace their current grant/audit checks.
            params = event.message.get("params")
            try:
                if not isinstance(params, dict) or params.get("sessionId") != self.session_id:
                    raise HostUnavailable("Foreign native input stream")
                self._inputs.observe(params.get("update"))
            except Exception:
                self._inputs.invalidate()
                self._ready = False
                self._host.close()
        elif (
            self._native_delivery
            and event.message.get("method") == "_sudocode/session/host_context"
            and "id" in event.message
        ):
            try:
                params = event.message.get("params")
                request_id = event.message.get("id")
                if (
                    not isinstance(params, dict)
                    or params.get("sessionId") != self.session_id
                    or not isinstance(request_id, (str, int))
                    or isinstance(request_id, bool)
                    or self._pending_context_input is not None
                    or self._context_active
                ):
                    raise HostUnavailable("Original context request is ambiguous")
                # Capture at arrival, BEFORE any host-authority await. A later
                # turn cannot become the identity of this session-only request.
                self._pending_context_input = self._inputs.context()
                self._pending_context_request_id = request_id
            except Exception:
                self._inputs.invalidate()
                self._ready = False
                self._host.close()

    def _failure(
        self,
        phase: str,
        method: str | None,
        error: BaseException,
        *,
        possibly_sent: bool,
        local_close_error: BaseException | None = None,
        client_id: str | None = None,
        input_acceptance: NativeInputSubmission | None = None,
    ) -> AcpFailureReceipt:
        request_id = self._request_ids.get(method) if method else None
        rejection = None
        if (
            phase in {"initialize", "load", "prompt", "input_submit"}
            and isinstance(error, RequestError)
            and request_id is not None
        ):
            rejection = AcpRejection(error.code, str(error), copy.deepcopy(error.data))
        return AcpFailureReceipt(
            self._host.binding,
            phase,
            "rejected" if rejection else "unknown" if possibly_sent else "refused",
            request_id,
            rejection,
            error,
            local_close_error,
            client_id,
            input_acceptance,
        )

    async def _close_after_failure(self) -> BaseException | None:
        try:
            await self.close()
        except (Exception, asyncio.CancelledError) as error:
            # Keep the first failure, including task cancellation, as primary.
            # Closing local RPC tasks says nothing about native writer joins.
            return error
        return None

    @classmethod
    async def attach(
        cls,
        host: TrustedHostContext,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        execution_cwd: str,
        mcp_servers: list[dict[str, Any]],
        resource_ref: str,
        resolve_context: BackendHostContextResolver,
        on_update: UpdateSink,
        on_input_lifecycle: InputLifecycleSink | None = None,
        observers: list[Callable[[StreamEvent], Awaitable[None] | None]] | None = None,
    ) -> CohostAcpSession:
        """Consume trusted streams; refuse missing capabilities or a failed load.

        execution_cwd is the published native workspace, not a path derived from
        the repository name. There is deliberately no session/new fallback.
        Native submit_input requires on_input_lifecycle. This controller-private
        provider must validate original connection/custody, correlate its durable
        pending record and assert current writer authority around recording.
        It grants no new effect and does not depend on a revoked human/UI grant.
        """
        if not execution_cwd or "\x00" in execution_cwd:
            raise ValueError("Require the published execution workspace")
        if on_input_lifecycle is not None and not callable(on_input_lifecycle):
            raise TypeError("The actual original input lifecycle recorder is required")
        self = cls(host, resource_ref, resolve_context, on_update, on_input_lifecycle)
        self._reader = reader
        servers = copy.deepcopy(mcp_servers)
        phase, load_attempted = "admission", False
        method: str | None = None
        try:
            await host.check_ready("session_load", resource_ref)
            self._connection = Connection(
                self._receive, writer, reader, observers=[self._observe_request, *(observers or [])]
            )
            phase, method = "initialize", "initialize"
            initialized = await self._connection.send_request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "mosswork", "version": __version__},
                },
            )
            if not self._supports_required_protocol(initialized):
                raise HostUnavailable("The original engine lacks required ACP capabilities")
            phase, method = "pre_load", None
            await host.check_ready("session_load", resource_ref)
            phase, method, load_attempted = "load", "session/load", True
            result = await self._connection.send_request(
                "session/load",
                {
                    "sessionId": self.session_id,
                    "cwd": execution_cwd,
                    "mcpServers": servers,
                    "_meta": {"sudocode": {"hostContext": {"version": 1}}},
                },
            )
            phase = "load_result"
            if not isinstance(result, dict) or any(
                result[key] != self.session_id
                for key in ("sessionId", "session_id", "id")
                if key in result
            ):
                raise HostUnavailable("ACP load returned a different or invalid session")
            phase = "post_load"
            await host.check_ready("session_load", resource_ref)
            if self._closed:
                raise HostUnavailable("ACP connection was withdrawn during load")
            self._ready = True
            return self
        except (Exception, asyncio.CancelledError) as error:
            close_error = await self._close_after_failure()
            receipt = self._failure(
                phase, method, error, possibly_sent=load_attempted, local_close_error=close_error
            )
            if isinstance(error, asyncio.CancelledError):
                raise AcpRequestCancelled(receipt) from None
            failure = AcpOutcomeUnknown if receipt.outcome == "unknown" else AcpRequestFailed
            raise failure("Original ACP session attachment failed", receipt) from None

    @property
    def session_id(self) -> str:
        return self._host.binding.native.durable_session_id

    @staticmethod
    def _supports_required_protocol(result: object) -> bool:
        if not isinstance(result, dict) or type(result.get("protocolVersion")) is not int:
            return False
        if result["protocolVersion"] != 1:
            return False
        capabilities = result.get("agentCapabilities")
        meta = result.get("_meta")
        namespace = meta.get("sudocode") if isinstance(meta, dict) else None
        if not isinstance(capabilities, dict) or capabilities.get("loadSession") is not True:
            return False
        if not isinstance(namespace, dict):
            return False
        return all(
            isinstance(value := namespace.get(name), dict)
            and type(value.get("version")) is int
            and value["version"] == 1
            for name in ("hostContext", "inputDelivery")
        )

    def _check_ready(self) -> Connection:
        if (
            self._closed
            or not self._ready
            or self._connection is None
            or (self._reader is not None and self._reader.at_eof())
        ):
            raise HostUnavailable("Original ACP session is unavailable")
        return self._connection

    async def _receive(self, method: str, params: Any, notification: bool) -> Any:
        if method == "_sudocode/session/host_context" and not notification:
            epoch = self._input_epoch
            token = None
            evidence = None
            request_id = None
            try:
                self._check_ready()
                if not isinstance(params, dict) or params.get("sessionId") != self.session_id:
                    raise PermissionError("Foreign ACP session")
                if self._native_delivery:
                    evidence = self._pending_context_input
                    self._pending_context_input = None
                    request_id = self._pending_context_request_id
                    self._pending_context_request_id = None
                    if evidence is None or self._context_active:
                        raise HostUnavailable("Original context request has no input evidence")
                    self._context_active = True
                    token = self._context_input.set(evidence)
                await self._host.check_current()
                if self._native_delivery:
                    self.context_input()
                text = await self._resolve_context(
                    {
                        "sessionId": self._host.binding.selection.thread_id,
                        "nativeSessionId": self.session_id,
                    }
                )
                await self._host.check_current()
                self._check_ready()
                if epoch != self._input_epoch or not isinstance(text, str) or not text.strip():
                    raise HostUnavailable("ACP context was withdrawn while reading")
                if self._native_delivery:
                    self.context_input()
                return {"appendSystemPrompt": text}
            except (Exception, asyncio.CancelledError) as error:
                if self._native_delivery:
                    # Acceptance may already have returned, and the recorder
                    # may have committed before failing. Withdraw new effects
                    # without discarding the original input or claiming cleanup.
                    if self._context_failure_receipt is None:
                        self._context_failure_receipt = replace(
                            self._failure("host_context", None, error, possibly_sent=True),
                            request_id=request_id,
                            input_context=evidence,
                        )
                    self._ready = False
                    self._inputs.invalidate()
                    if isinstance(error, asyncio.CancelledError):
                        raise AcpRequestCancelled(self._context_failure_receipt) from None
                elif isinstance(error, asyncio.CancelledError):
                    raise
                # Source/provider exceptions can contain credentials. Neither
                # forward them nor substitute an older context snapshot.
                raise RequestError(-32603, "Current host context is unavailable") from None
            finally:
                if token is not None:
                    self._context_input.reset(token)
                    self._context_active = False

        if method in {"session/update", "_sudocode/session/input_update"} and notification:
            try:
                if (
                    self._closed
                    or not isinstance(params, dict)
                    or params.get("sessionId") != self.session_id
                    or not isinstance(params.get("update"), dict)
                ):
                    raise HostUnavailable("ACP update does not belong to the original session")
                if (
                    self._on_input_lifecycle is not None
                    and method == "_sudocode/session/input_update"
                    and params["update"].get("kind") in {"committed", "turn_complete", "error"}
                ):
                    await self._record_input_lifecycle(params["update"])
                await self._host.check_current()
                if self._closed:
                    raise HostUnavailable("ACP update was withdrawn")
                await self._on_update(method, params["update"])
            except Exception:
                # The SDK suppresses notification-handler errors. Retain a
                # closed local gate so a broken projection cannot remain Ready.
                self._ready = False
                if not self._native_delivery and self._on_input_lifecycle is None:
                    self._closed = True
                self._host.close()
                raise
            return None

        if method == "session/request_permission" and not notification:
            # Native questions require the current, authenticated product HITL
            # adapter. Until installed, never grant an engine's permission.
            return {"outcome": {"outcome": "cancelled"}}
        if notification:
            return None
        raise RequestError.method_not_found(method)

    @property
    def context_failure_receipt(self) -> AcpFailureReceipt | None:
        """First private context failure; possible persistence is not input commit.

        Remains available after local close for the original provider's outcome
        reconciliation. Neither this receipt nor close authorizes replacement.
        """
        return self._context_failure_receipt

    @property
    def input_lifecycle_failure_receipt(self) -> AcpFailureReceipt | None:
        """First private recording uncertainty, retained after local close.

        An observed notification is not itself validated custody, a successful
        durable write, a native cleanup receipt or permission for another input.
        """
        return self._input_lifecycle_failure_receipt

    async def _record_input_lifecycle(self, update: dict[str, Any]) -> None:
        # Keep ordered recording independent of current human/UI permission.
        # The injected writer must authenticate the original native connection,
        # custody and persisted association, and assert its current writer before
        # and after I/O. It must not use a stale human grant or grant a new effect.
        original = copy.deepcopy(update)
        try:
            async with self._input_lifecycle_lock:
                if self._closed:
                    raise HostUnavailable("Original input lifecycle transport was closed")
                if self._on_input_lifecycle is None:
                    raise HostUnavailable("Original input lifecycle recorder is unavailable")
                await _require_completion(
                    self._on_input_lifecycle(self._host.binding, copy.deepcopy(original))
                )
        except (Exception, asyncio.CancelledError) as error:
            if self._input_lifecycle_failure_receipt is None:
                self._input_lifecycle_failure_receipt = replace(
                    self._failure("input_lifecycle", None, error, possibly_sent=True),
                    input_lifecycle_update=original,
                )
            self._ready = False
            self._inputs.invalidate()
            self._host.close()
            if isinstance(error, asyncio.CancelledError):
                raise AcpRequestCancelled(self._input_lifecycle_failure_receipt) from None
            raise AcpOutcomeUnknown(
                "Original input lifecycle recording is uncertain",
                self._input_lifecycle_failure_receipt,
            ) from None

    def context_input(self) -> NativeInputContext:
        """Read this actual context task's original pending batch, never a cached grant.

        The existing Owner6 ContextRecorder may close over this method to durably
        record its exact ModelPointer before returning text. It must not wait for
        a later committed transcript row. Caller/native authority stays external.
        """
        self._check_ready()
        evidence = self._context_input.get()
        if not self._context_active or evidence is None or self._inputs.context() is not evidence:
            raise HostUnavailable("No current original context input")
        return evidence

    def input_context_recorder(
        self,
        record_pending: Callable[
            [HostBinding, RetainedOntologyBinding, ModelPointer, NativeInputContext],
            Awaitable[None],
        ],
    ) -> ContextRecorder:
        """Bind each original pending input to the existing three-argument port.

        The trusted provider persists through its original input lifecycle. This
        adapter stores no pointer, allocates no identity and caches no first
        input. It checks the actual context task again after persistence waits.
        """
        if not callable(record_pending):
            raise TypeError("The actual original input recorder is required")

        async def record(
            host: HostBinding, registered: RetainedOntologyBinding, pointer: ModelPointer
        ) -> None:
            evidence = self.context_input()
            if host != evidence.binding:
                raise HostUnavailable("Context recorder has a foreign original host")
            await _require_completion(record_pending(host, registered, pointer, evidence))
            if self.context_input() is not evidence:
                raise HostUnavailable("Original context input changed during recording")

        return record

    async def submit_input(
        self, client_id: str, text: str, *, delivery: Literal["steer", "queue"]
    ) -> NativeInputSubmission:
        """Submit the existing inputDelivery action once with its original clientId.

        Numeric IDs come only from the native response/ordered state. Acceptance
        is separate from committed/completed. No diagnostic snapshot, automatic
        new ID, retry or legacy session/prompt fallback is performed.
        """
        if not isinstance(client_id, str) or not client_id or "\x00" in client_id:
            raise ValueError("Require the original native input clientId")
        if not isinstance(text, str) or not text.strip() or delivery not in {"steer", "queue"}:
            raise ValueError("Require explicit input text and native delivery mode")
        async with self._prompt_lock:
            connection = self._check_ready()
            if self._on_input_lifecycle is None:
                raise HostUnavailable("The original input lifecycle recorder is required")
            if self._legacy_prompt_used:
                raise HostUnavailable("Legacy prompt has no native input lifecycle")
            self._native_delivery = True
            epoch = self._input_epoch
            phase, attempted = "input_admission", False
            accepted = None
            self._request_ids.pop(_INPUT_METHOD, None)
            try:
                await self._host.check_ready("session_prompt", self._resource_ref)
                self._check_ready()
                if epoch != self._input_epoch:
                    raise HostUnavailable("Original input withdrawn before dispatch")
                phase, attempted = "input_submit", True
                result = await connection.send_request(
                    _INPUT_METHOD,
                    {
                        "sessionId": self.session_id,
                        "action": "submit",
                        "clientId": client_id,
                        "content": [{"type": "text", "text": text}],
                        "delivery": delivery,
                    },
                )
                phase = "input_result"
                accepted = NativeInputSubmission.parse(client_id, result)
                # The receipt may predate newer state notifications. Never feed
                # it back as the current wire projection or wait for commit here.
                await self._host.check_current()
                self._check_ready()
                return accepted
            except (Exception, asyncio.CancelledError) as error:
                if attempted:
                    self._ready = False
                    self._inputs.invalidate()
                receipt = self._failure(
                    phase,
                    _INPUT_METHOD,
                    error,
                    possibly_sent=attempted,
                    client_id=client_id,
                    input_acceptance=accepted,
                )
                if isinstance(error, asyncio.CancelledError):
                    raise AcpRequestCancelled(receipt) from None
                failure = AcpOutcomeUnknown if receipt.outcome == "unknown" else AcpRequestFailed
                raise failure("Original native input submission failed", receipt) from None

    async def prompt(self, text: str) -> dict[str, Any]:
        """Prompt the original session once; do not retry an uncertain outcome."""
        if not text.strip():
            raise ValueError("Require non-empty input")
        async with self._prompt_lock:
            connection = self._check_ready()
            if self._native_delivery:
                raise HostUnavailable("Native input delivery requires its original lifecycle")
            await self._host.check_ready("session_prompt", self._resource_ref)
            self._check_ready()
            self._legacy_prompt_used = True
            phase = "prompt"
            self._request_ids.pop("session/prompt", None)
            try:
                result = await connection.send_request(
                    "session/prompt",
                    {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]},
                )
                phase = "prompt_result"
                await self._host.check_current()
                self._check_ready()
                if not isinstance(result, dict) or not isinstance(result.get("stopReason"), str):
                    raise HostUnavailable("Original ACP prompt completion is unavailable")
                return result
            except (Exception, asyncio.CancelledError) as error:
                self._ready = False
                receipt = self._failure(phase, "session/prompt", error, possibly_sent=True)
                if isinstance(error, asyncio.CancelledError):
                    raise AcpRequestCancelled(receipt) from None
                failure = AcpOutcomeUnknown if receipt.outcome == "unknown" else AcpRequestFailed
                raise failure(
                    "Original ACP prompt failed; inspect its private receipt", receipt
                ) from None

    async def cancel(self) -> None:
        """Write standard Stop; this is not an engine-stopped acknowledgment."""
        self._input_epoch += 1
        self._inputs.invalidate()
        connection = self._check_ready()
        await self._host.authorize("session_cancel", self._resource_ref)
        self._check_ready()
        try:
            await connection.send_notification("session/cancel", {"sessionId": self.session_id})
        except (Exception, asyncio.CancelledError) as error:
            self._ready = False
            receipt = self._failure("cancel", None, error, possibly_sent=True)
            if isinstance(error, asyncio.CancelledError):
                raise AcpRequestCancelled(receipt) from None
            raise AcpOutcomeUnknown(
                "Original ACP cancellation outcome is unknown", receipt
            ) from None

    async def close(self) -> None:
        """Withdraw context and close RPC tasks; native cleanup stays with its owner."""
        self._input_epoch += 1
        self._inputs.invalidate()
        self._ready = False
        self._closed = True
        self._host.close()
        if self._connection is not None:
            await self._connection.close()
