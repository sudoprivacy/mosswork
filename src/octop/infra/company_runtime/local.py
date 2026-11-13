"""Local operation receipts and supervised processes. CWD is not an OS sandbox."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import ExecuteResponse
from langchain.agents.middleware import AgentMiddleware
from moss_harness.backends.local_shell import HarnessLocalShellBackend

from octop.infra.company_runtime.processes import (
    _TOOL_CALL_ID,
    ProjectProcesses,
)
from octop.infra.company_runtime.processes import (
    atomic_json as atomic_json,
)
from octop.infra.company_runtime.processes import (
    windows_python_command as windows_python_command,
)
from octop.infra.utils.asyncio import offload


def local_attachment_metadata(project: Path, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep verified imported originals in user history without uploading their bytes."""
    if len(files) > 50:
        raise ValueError("too_many_local_attachments")
    metadata = []
    for file in files:
        path = Path(file["path"]).resolve(strict=True)
        if not path.is_relative_to(project.resolve()) or not path.is_file():
            raise ValueError("local_attachment_outside_project")
        size = path.stat().st_size
        if not 0 < size <= 50 * 1024 * 1024 or size != file["size"]:
            raise ValueError("local_attachment_size_changed")
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != file["sha256"]:
            raise ValueError("local_attachment_checksum_mismatch")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        kind = mime.split("/", 1)[0]
        metadata.append(
            {
                "workspace_path": str(path),
                "filename": path.name,
                "media_type": mime,
                "kind": kind if kind in {"image", "audio", "video"} else "file",
                "execution_location": "desktop",
                "size": size,
                "sha256": digest,
            }
        )
    return metadata


def image_reference_inputs(project: Path, references: list[str]) -> list[str]:
    """Upload only explicitly selected project images to the image-editing tool."""
    if len(references) > 4:
        raise ValueError("At most four reference images are supported")
    encoded = []
    total = 0
    for reference in references:
        path = (project / reference).resolve(strict=True)
        if not path.is_relative_to(project.resolve()) or not path.is_file():
            raise ValueError("Reference image must be inside this project")
        mime = mimetypes.guess_type(path.name)[0]
        if mime not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("Reference images must be PNG, JPEG or WebP")
        with path.open("rb") as source:
            data = source.read(10 * 1024 * 1024 + 1)
        total += len(data)
        if not data or total > 10 * 1024 * 1024:
            raise ValueError("Reference images must total at most 10 MB")
        encoded.append(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}")
    return encoded


class OperationJournal:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, operation: str) -> Path:
        return self.root / (hashlib.sha256(operation.encode()).hexdigest() + ".json")

    def get(self, operation: str) -> dict[str, Any] | None:
        path = self.path(operation)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def save(self, operation: str, **data: Any) -> None:
        atomic_json(
            self.path(operation), {"operation_id": operation, "updated_at": time.time(), **data}
        )


class ReceiptMiddleware(AgentMiddleware):
    ALLOWED = frozenset(
        {
            "execute",
            "ls",
            "read_file",
            "write_file",
            "edit_file",
            "glob",
            "grep",
            "write_todos",
            "current_time",
            "memory_search",
            "memory_get",
            "remember",
            "search_knowledge",
            "company_generate_image",
            "company_generate_video",
            "company_synthesize_speech",
            "company_transcribe_audio",
            "company_web_search",
            "send_local_file",
            "load_company_skill",
        }
    )

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        tools = [
            tool
            for tool in request.tools
            if (tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", ""))
            in self.ALLOWED
        ]
        return await handler(request.override(tools=tools))

    def __init__(
        self, journal: OperationJournal, saver: Any, authorize: Callable[..., Any]
    ) -> None:
        self.journal, self.saver, self.authorize = journal, saver, authorize
        self.locks: dict[str, asyncio.Lock] = {}

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        call = request.tool_call
        if call["name"] in {"write_file", "edit_file"} and str(
            call.get("args", {}).get("file_path", "")
        ).startswith("/company-skills/"):
            raise RuntimeError("company_skill_cache_read_only")
        if call["name"] not in self.ALLOWED:
            raise RuntimeError("company_tool_not_granted")
        key = str(call["id"])
        digest = hashlib.sha256(
            json.dumps(call, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        async with self.locks.setdefault(key, asyncio.Lock()):
            old = await offload(self.journal.get, key)
            if old:
                if old["request_hash"] != digest:
                    raise RuntimeError("operation_identity_conflict")
                if old["status"] == "completed":
                    return self.saver.unpack(old["result"])
                raise RuntimeError(
                    "operation_result_unknown: inspect the existing output before retry"
                )
            await self.authorize()
            await offload(
                self.journal.save, key, request_hash=digest, status="running", tool=call["name"]
            )
            token = _TOOL_CALL_ID.set(key)
            try:
                result = await handler(request)
            except BaseException:
                await offload(
                    self.journal.save, key, request_hash=digest, status="unknown", tool=call["name"]
                )
                raise
            finally:
                _TOOL_CALL_ID.reset(token)
            await offload(
                self.journal.save,
                key,
                request_hash=digest,
                status="completed",
                tool=call["name"],
                result=self.saver.pack(result),
            )
            return result


class ProjectBackend(ProjectProcesses, HarnessLocalShellBackend):
    def __init__(
        self, project: Path, state: Path, emit: Callable[..., None], *, tool_path: str
    ) -> None:
        HarnessLocalShellBackend.__init__(
            self,
            root_dir=project,
            workspace_dir=project,
            virtual_mode=True,
            inherit_env=False,
            timeout=900,
        )
        ProjectProcesses.__init__(self, project, state, emit, tool_path=tool_path)

    def _resolve_path(self, key: str) -> Path:
        # File tools accept both project-relative paths and the native absolute
        # paths shown in shell output; preserve virtual-root escape checks.
        native = Path(key)
        if native.is_absolute() and native.is_relative_to(self.project):
            key = native.relative_to(self.project).as_posix()
        return super()._resolve_path(key)

    def artifact_metadata(self, file_path: str) -> dict[str, Any]:
        """Describe an existing project file for the native preview/reveal bridge."""
        target = self._resolve_path(file_path)
        if not target.is_file():
            raise ValueError("local_file_not_found")
        with target.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            size = os.fstat(stream.fileno()).st_size
        return {"path": str(target), "size": size, "sha256": digest}

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        raise RuntimeError("Synchronous execution is disabled; use asynchronous runtime")

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        result = await ProjectProcesses.aexecute(self, command, timeout=timeout)
        return ExecuteResponse(
            output=result.output, exit_code=result.exit_code, truncated=result.truncated
        )


def artifact_index(
    project: Path,
    *,
    previous: list[dict[str, Any]] | None = None,
    limit: int | None = 1000,
) -> list[dict[str, Any]]:
    """Index only new/changed bytes when a pre-turn snapshot is supplied."""
    known = {item["path"]: item["sha256"] for item in previous or []}
    outputs = []
    for path in sorted(project.rglob("*")):
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(project):
            continue
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if known.get(str(path)) == digest:
            continue
        outputs.append({"path": str(path), "size": path.stat().st_size, "sha256": digest})
        if limit is not None and len(outputs) >= limit:
            break
    return outputs
