"""Force browser tool calls onto the current user's isolated profile."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ai_dev_browser.remote.control import ControlError
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.config import get_config
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from octop.infra.browser.control import profile_control
from octop.infra.utils.browser_media import parse_mosswork_user_id, user_browser_profile

logger = logging.getLogger(__name__)

_BROWSER_TOOL = "browser_use"
_MISSING_USER = (
    "browser_use blocked: the current turn has no mosswork user id, "
    "so a browser profile cannot be isolated."
)


def _bind_browser_profile(request: ToolCallRequest) -> ToolCallRequest | ToolMessage:
    tool_call = request.tool_call
    if str(tool_call.get("name") or "") != _BROWSER_TOOL:
        return request

    configurable = dict(get_config().get("configurable") or {})
    user_id = parse_mosswork_user_id(configurable.get("user"))
    if user_id is None:
        logger.warning("browser_use blocked: configurable.user is missing or invalid")
        return ToolMessage(
            content=_MISSING_USER,
            tool_call_id=str(tool_call.get("id") or ""),
            status="error",
        )

    raw_args = tool_call.get("args")
    args = dict(raw_args) if isinstance(raw_args, dict) else {}
    args["profile"] = user_browser_profile(user_id)
    return request.override(tool_call={**tool_call, "args": args})


class BrowserProfileMiddleware(AgentMiddleware[Any, Any]):
    """Prevent model-selected profiles from crossing mosswork user boundaries."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        self._root = root

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        bound = _bind_browser_profile(request)
        if isinstance(bound, ToolMessage):
            return bound
        if str(bound.tool_call.get("name") or "") == _BROWSER_TOOL:
            return ToolMessage(
                content="browser_use requires asynchronous execution to share the browser input barrier.",
                tool_call_id=str(bound.tool_call.get("id") or ""),
                status="error",
            )
        return handler(bound)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        bound = _bind_browser_profile(request)
        if isinstance(bound, ToolMessage):
            return bound
        if str(bound.tool_call.get("name") or "") != _BROWSER_TOOL:
            return await handler(bound)
        args = bound.tool_call["args"]
        config = get_config()
        configurable = config.get("configurable") or {}
        actor = json.dumps(
            [
                configurable.get("thread_id"),
                configurable.get("checkpoint_ns", ""),
            ]
        )
        controls = await profile_control(str(args["profile"]), self._root)
        try:
            return await controls.run_ai(
                actor,
                str(args.get("action") or ""),
                lambda: handler(bound),
                epoch=configurable.get("mosswork_browser_epoch", 0),
            )
        except ControlError as error:
            return ToolMessage(
                content=json.dumps({"error": error.code, **(error.details or {})}),
                tool_call_id=str(bound.tool_call.get("id") or ""),
                status="error",
            )


__all__ = ["BrowserProfileMiddleware"]
