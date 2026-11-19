"""ACP client callback for delegated external agents."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

from moss_harness.acp.attention import OrderedAttentionProjection
from moss_harness.acp.models import ACPRunnerConfig, SuspendedPermission
from moss_harness.acp.peer_arrival import PeerArrival
from moss_harness.acp.permissions import ACPPermissionAdapter
from moss_harness.acp.runtime_status import supports_engine_observation

MessageHandler = Callable[[dict[str, Any], bool], Awaitable[None]]


class ACPHostedClient:
    def __init__(
        self,
        *,
        runner_name: str,
        runner_config: ACPRunnerConfig,
        cwd: str,
    ) -> None:
        self.runner_name = runner_name
        self.tool_parse_mode = runner_config.tool_parse_mode
        self._permission_adapter = ACPPermissionAdapter(cwd=cwd)
        self._on_message: MessageHandler | None = None
        self._assistant_text = ""
        self._pending_permission: SuspendedPermission | None = None
        self._permission_future: asyncio.Future[Any] | None = None
        self._permission_request_id: int | None = None
        self._permission_requested = asyncio.Event()
        self._session_id: str | None = None
        self._is_invalidated = False
        self._attention_supported = False
        self.attention = OrderedAttentionProjection()
        self.last_peer_arrival: PeerArrival | None = None

    def bind_engine_session(self, session_id: str, metadata: dict[str, Any] | None) -> None:
        self._session_id = session_id
        self.last_peer_arrival = None
        self._is_invalidated = False
        self._attention_supported = supports_engine_observation(metadata, "attention")
        self.attention.invalidate()

    def invalidate_observations(self) -> None:
        self._is_invalidated = True
        self._session_id = None
        self.last_peer_arrival = None
        self.attention.invalidate()
        self._cancel_permission()

    @property
    def pending_permission(self) -> SuspendedPermission | None:
        return self._pending_permission

    def update_cwd(self, cwd: str) -> None:
        self._permission_adapter = ACPPermissionAdapter(cwd=cwd)

    def start_prompt(self, on_message: MessageHandler) -> None:
        self._on_message = on_message
        self._assistant_text = ""

    def resume_prompt(self, on_message: MessageHandler) -> None:
        self._on_message = on_message
        self._permission_requested.clear()

    async def wait_for_permission_request(self) -> None:
        await self._permission_requested.wait()

    def resolve_permission(self, option_id: str) -> None:
        if self._pending_permission is None or self._permission_future is None:
            raise ValueError("No pending ACP permission request.")
        selected = self._permission_adapter.resolve_option_by_id(
            self._pending_permission.options,
            option_id,
        )
        if selected is None:
            raise ValueError(
                "respond requires the exact selected permission option id from the provided options.",
            )
        if not self._permission_future.done():
            self._permission_future.set_result(
                self._permission_adapter.selected_response(selected),
            )
        self._clear_permission()

    def _clear_permission(self) -> None:
        self._pending_permission = None
        self._permission_future = None
        self._permission_request_id = None
        self._permission_requested.clear()

    def _cancel_permission(self) -> None:
        if self._permission_future is not None and not self._permission_future.done():
            self._permission_future.set_result(self._permission_adapter.cancelled_response())
        self._clear_permission()

    async def request_permission(
        self,
        options: list[Any],
        session_id: str,
        tool_call: Any,
        **_: Any,
    ) -> Any:
        if self._is_invalidated or (self._session_id is not None and session_id != self._session_id):
            return self._permission_adapter.cancelled_response()
        suspended = self._permission_adapter.build_suspended_permission(
            runner=self.runner_name,
            tool_call=tool_call,
            options=options,
        )
        if self._permission_adapter.is_hard_blocked(tool_call):
            return self._permission_adapter.cancelled_response()
        if self._permission_future is not None:
            # The engine owns serialization. Never replace another live reply.
            return self._permission_adapter.cancelled_response()
        request_id: Any = suspended.payload["toolCall"]
        for key in ("_meta", "sudocode", "attention", "requestId"):
            request_id = request_id.get(key) if isinstance(request_id, dict) else None
        self._permission_request_id = request_id if type(request_id) is int else None
        if self._attention_supported and self._permission_request_id is not None:
            snapshot = self.attention.snapshot
            if snapshot is not None and not any(request.id == request_id for request in snapshot.requests):
                self._clear_permission()
                return self._permission_adapter.cancelled_response()
        self._pending_permission = suspended
        self._permission_requested.set()
        future = asyncio.get_running_loop().create_future()
        self._permission_future = future
        try:
            await self._emit_message(
                {
                    "type": "permission_request",
                    "title": suspended.summary or suspended.tool_name,
                    "options": suspended.options,
                    "tool_kind": suspended.tool_kind,
                    "tool_name": suspended.tool_name,
                    "request_id": self._permission_request_id,
                },
                True,
            )
            return await future
        finally:
            if self._permission_future is future:
                self._clear_permission()

    async def session_update(self, session_id: str, update: Any, **metadata: Any) -> None:
        from acp.schema import (
            AgentMessageChunk,
            AgentPlanUpdate,
            AgentThoughtChunk,
            AvailableCommandsUpdate,
            CurrentModeUpdate,
            ToolCallProgress,
            ToolCallStart,
            UserMessageChunk,
        )

        if isinstance(update, UserMessageChunk):
            if session_id == self._session_id and not self._is_invalidated:
                # The ACP SDK expands notification-level _meta into kwargs.
                arrival = PeerArrival.from_notification(metadata)
                if arrival is not None:
                    self.last_peer_arrival = arrival
            return
        if isinstance(update, AgentMessageChunk):
            await self._accumulate_assistant_content(update.content)
            return

        if isinstance(update, AgentThoughtChunk):
            return

        if isinstance(update, ToolCallStart | ToolCallProgress):
            event = self._tool_event_from_state(update)
            if event is not None:
                await self._emit_message(event, True)
            return

        if isinstance(
            update,
            CurrentModeUpdate | AgentPlanUpdate | AvailableCommandsUpdate,
        ):
            return

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        _ = params
        self._unsupported_method(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        if method.lstrip("_") == "sudocode/session/attention_update":
            try:
                if (
                    not self._attention_supported
                    or self._session_id is None
                    or params.get("sessionId") != self._session_id
                ):
                    raise ValueError("Attention does not belong to the bound engine session")
                snapshot = self.attention.observe(params.get("update"))
            except ValueError:
                self.invalidate_observations()
                raise
            if self._permission_request_id is not None and not any(
                request.id == self._permission_request_id for request in snapshot.requests
            ):
                self._cancel_permission()
            await self.attention.deliver_current(
                snapshot.revision,
                lambda current: self._emit_message(
                    {"type": "agent_attention", "snapshot": current.model_dump(mode="json")}, True
                ),
            )
            return
        _ = params
        self._unsupported_method(method)

    def _unsupported_method(self, method: str) -> NoReturn:
        from acp import RequestError

        raise RequestError(code=-32601, message=f"Unsupported ACP extension method: {method}")

    async def finish_prompt(self) -> dict[str, Any] | None:
        if self._assistant_text:
            return {"type": "text", "text": self._assistant_text, "is_chunk": False}
        return None

    async def _emit_message(self, payload: dict[str, Any], is_last: bool) -> None:
        if self._on_message is None:
            return
        await self._on_message(payload, is_last)

    async def _accumulate_assistant_content(self, content: Any) -> None:
        text = self._extract_text_from_content(content)
        if text:
            self._merge_assistant_text(text)

    def _merge_assistant_text(self, text: str) -> None:
        if not text or text == self._assistant_text:
            return
        if not self._assistant_text:
            self._assistant_text = text
            return
        if text.startswith(self._assistant_text):
            self._assistant_text = text
            return
        self._assistant_text = self._assistant_text + text

    def _extract_text_from_content(self, content: Any) -> str:
        if hasattr(content, "text") and isinstance(getattr(content, "text", None), str):
            return str(content.text)
        if isinstance(content, list):
            return "".join(part for part in (self._extract_text_from_content(item) for item in content) if part)
        if isinstance(content, dict) and content.get("type") == "text":
            return str(content.get("text") or "")
        return ""

    def _tool_event_from_state(self, update: Any) -> dict[str, Any] | None:
        from acp.schema import ToolCallStart

        call_id = str(getattr(update, "tool_call_id", "") or "")
        if not call_id:
            return None
        title = str(getattr(update, "title", None) or "unknown")
        kind = str(getattr(update, "kind", None) or "other")
        return {
            "type": "tool_start" if isinstance(update, ToolCallStart) else "tool_update",
            "name": title,
            "call_id": call_id,
            "title": title,
            "kind": kind,
            "detail": title,
        }
