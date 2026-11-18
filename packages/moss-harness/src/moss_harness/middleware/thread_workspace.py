"""Use the current conversation directory for routine file and shell tools."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from moss_harness.backends.thread_directory import active_thread_directory, ensure_thread_directory, thread_directory
from moss_harness.backends.workspace import BackendWorkspace, is_system_logical_entry

_FILE_KEYS = {
    "ls": "path",
    "glob": "path",
    "grep": "path",
    "read_file": "file_path",
    "write_file": "file_path",
    "edit_file": "file_path",
    "delete": "file_path",
    "send_file_to_user": "file_path",
    "virtual_to_native_path": "path",
}


class ThreadWorkspaceMiddleware(AgentMiddleware[Any, Any]):
    def __init__(self, workspace: BackendWorkspace) -> None:
        self.workspace = workspace

    def before_agent(self, state: Any, runtime: Any) -> None:
        from langgraph.config import get_config

        tid = (get_config().get("configurable") or {}).get("thread_id")
        if isinstance(tid, str) and tid:
            ensure_thread_directory(self.workspace, thread_directory(tid))

    async def abefore_agent(self, state: Any, runtime: Any) -> None:
        await asyncio.to_thread(self.before_agent, state, runtime)

    def _directory(self, request: ToolCallRequest) -> str:
        config = request.runtime.config or {}
        tid = (config.get("configurable") or {}).get("thread_id")
        return thread_directory(tid) if isinstance(tid, str) and tid else ""

    def _scope_path(self, request: ToolCallRequest, directory: str) -> ToolCallRequest | ToolMessage:
        call = request.tool_call
        name = call["name"]
        key = _FILE_KEYS.get(name)
        if not directory or not key:
            return request
        args = call["args"]
        raw = args.get(key, "." if name in {"ls", "glob", "grep"} else None)
        if not isinstance(raw, str):
            return request
        text = raw.replace("\\", "/")
        # Models may repeat a previously returned absolute workspace path.
        for base in (self.workspace.tool_path("."), self.workspace.workspace_dir.as_posix()):
            root = base.rstrip("/")
            if root and (text == root or text.startswith(root + "/")):
                text = text[len(root) :]
                break
        rel = PurePosixPath(text.lstrip("/"))

        first = next(iter(rel.parts), "")
        # Agent instructions, registered skills and uploaded inputs retain their
        # existing addresses. Generated files, including plans, belong to the thread.
        shared = is_system_logical_entry(first) or first in {
            ".mosswork",
            "AGENTS.md",
            "SOUL.md",
            "USER.md",
            "MEMORY.md",
            "inbound",
        }
        if ".." in rel.parts or (first == "threads" and not rel.is_relative_to(directory)):
            return ToolMessage(
                content="Path is outside the conversation directory", status="error", tool_call_id=call.get("id") or ""
            )
        if not shared and not rel.is_relative_to(directory):
            rel = PurePosixPath(directory) / rel
        if rel.is_relative_to(directory):
            try:
                self.workspace.check_subdirectory(directory, rel.relative_to(directory).as_posix())
            except PermissionError as exc:
                return ToolMessage(content=str(exc), status="error", tool_call_id=call.get("id") or "")
        path = rel.as_posix() if name == "send_file_to_user" else self.workspace.tool_path(rel.as_posix())
        return request.override(tool_call={**call, "args": {**args, key: path}})

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]]
    ) -> ToolMessage | Command[Any]:
        directory = self._directory(request)
        token = active_thread_directory.set(directory)
        try:
            scoped = self._scope_path(request, directory)
            if isinstance(scoped, ToolMessage):
                return scoped
            return handler(scoped)
        finally:
            active_thread_directory.reset(token)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]
    ) -> ToolMessage | Command[Any]:
        directory = self._directory(request)
        token = active_thread_directory.set(directory)
        try:
            scoped = await asyncio.to_thread(self._scope_path, request, directory)
            if isinstance(scoped, ToolMessage):
                return scoped
            return await handler(scoped)
        finally:
            active_thread_directory.reset(token)
