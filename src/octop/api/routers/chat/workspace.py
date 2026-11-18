"""Browse the directory used by the current conversation."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from fastapi import APIRouter, Depends
from moss_harness.backends.thread_directory import ensure_thread_directory, thread_directory
from moss_harness.backends.workspace import BackendWorkspace
from pydantic import BaseModel, Field

from octop.api.common.workspace import (
    assert_workspace_resource_access,
    file_info_to_dict,
    require_agent_workspace,
)
from octop.api.deps import current_user, get_server
from octop.api.routers.chat.history import _agent_facing_workspace_dir, _require_thread
from octop.infra.agents.threads.artifact import artifacts_outside_thread_directory
from octop.infra.backend.tree_listing import dedupe_tree_rows
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.gateway.threads import thread_row_has_messages
from octop.infra.utils.asyncio import offload
from octop.infra.utils.thread_artifact import ThreadArtifact

router = APIRouter()


class DirectoryEntry(BaseModel):
    name: str
    is_dir: bool


class ThreadWorkspace(BaseModel):
    location: Literal["server", "desktop"]
    root: str = Field(
        description="Server workspace-relative directory or bound desktop project name."
    )
    display_root: str | None = None
    entries: list[DirectoryEntry] = Field(default_factory=list)
    legacy_files: list[ThreadArtifact] = Field(default_factory=list)
    task_id: str | None = None
    has_messages: bool


def _directory_entries(
    ws: BackendWorkspace, root: str, path: str, rows: list[dict[str, Any]]
) -> list[DirectoryEntry]:
    entries = []
    for row in rows:
        entry_path = PurePosixPath(row["path"])
        # BackendWorkspace presents local symlink targets as resolved paths.
        if len(entry_path.parts) > 1 and entry_path.parent != PurePosixPath(root, path):
            continue
        try:
            ws.check_subdirectory(root, str(PurePosixPath(path, entry_path.name)))
        except PermissionError:
            continue
        entries.append(DirectoryEntry(name=entry_path.name, is_dir=row["is_dir"]))
    return entries


@router.get(
    "/agents/{agent_id}/threads/{thread_id}/workspace",
    summary="Browse a conversation workspace",
    description=(
        "List one directory relative to the owned conversation. Opening an existing server conversation "
        "initializes its directory if needed. Desktop files are listed by the originating client."
    ),
    response_model=ThreadWorkspace,
)
async def thread_workspace(
    agent_id: str,
    thread_id: str,
    path: str = "",
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> ThreadWorkspace:
    row = await offload(_require_thread, server, agent_id, thread_id, user, None)
    if (
        PureWindowsPath(path).anchor
        or "\\" in path
        or ".." in PurePosixPath(path).parts
        or "\x00" in path
    ):
        raise MossworkError(ErrorCode.SLASH_BAD_ARGS, "Invalid workspace-relative directory")
    repo = server.services.company_runtime_repo
    binding = await offload(repo.thread_state, thread_id)
    has_messages = thread_row_has_messages(row)
    if binding["execution_location"] == "desktop":
        return ThreadWorkspace(
            location="desktop",
            root=await offload(repo.project_name, thread_id),
            task_id=binding.get("local_task_id"),
            has_messages=has_messages,
        )
    ws = await require_agent_workspace(agent_id, user=user, server=server, owner_only=True)
    root = thread_directory(thread_id)
    directory = str(PurePosixPath(root, path))
    await assert_workspace_resource_access(server, user, agent_id, ws, directory)
    try:
        await offload(ws.check_subdirectory, root, path)
        listing = await ws.als(directory)
        if listing is None and has_messages and not path:
            # Sessions predating per-thread directories acquire their root on first open.
            await offload(ensure_thread_directory, ws, root)
            listing = await ws.als(directory)
    except (ValueError, PermissionError) as exc:
        raise MossworkError(ErrorCode.FORBIDDEN, "Directory is outside the conversation") from exc
    if listing is None and (path or await ws.aexists(directory)):
        raise MossworkError(ErrorCode.NOT_FOUND, "Cannot list conversation directory")
    rows = (
        dedupe_tree_rows([file_info_to_dict(i) for i in listing.entries or []], parent=directory)
        if listing
        else []
    )
    entries = await offload(_directory_entries, ws, root, path, rows)
    facing = await offload(_agent_facing_workspace_dir, server, agent_id)
    legacy = []
    if not path:
        legacy = artifacts_outside_thread_directory(
            row.artifacts,
            facing,
            agent_id=agent_id,
            directory=root,
            tool_root=await offload(ws.tool_path, root),
        )
    return ThreadWorkspace(
        location="server",
        root=root,
        display_root=(facing / root).as_posix(),
        entries=entries,
        legacy_files=legacy,
        has_messages=has_messages,
    )
