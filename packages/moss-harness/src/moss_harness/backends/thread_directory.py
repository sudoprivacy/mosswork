"""Directory shared by one conversation's tool calls, including executor threads."""

from __future__ import annotations

import hashlib
import re
from contextlib import suppress
from contextvars import ContextVar
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from moss_harness.backends.utils import BackendOperationNotSupportedError

if TYPE_CHECKING:
    from moss_harness.backends.workspace import BackendWorkspace

# The agent and its backend can serve multiple conversations concurrently.
active_thread_directory: ContextVar[str] = ContextVar("active_thread_directory", default="")


def thread_directory(thread_id: str) -> str:
    if re.fullmatch(r"thr_[A-Za-z0-9_-]{1,124}", thread_id):
        return f"threads/{thread_id}"
    # External channel identifiers can contain separators, drive letters or reserved names.
    return "threads/id-" + hashlib.sha256(thread_id.encode()).hexdigest()


def ensure_thread_directory(workspace: BackendWorkspace, directory: str) -> None:
    """Materialize the session root on backends that support empty directories."""
    workspace.check_subdirectory(directory, "")
    # Some backends represent only files and infer directories from their keys.
    with suppress(BackendOperationNotSupportedError):
        workspace.mkdir(directory)


def thread_output_path(path: str) -> str:
    return str(PurePosixPath(active_thread_directory.get(), path))
