"""Custom middleware shipped with moss-harness."""

from __future__ import annotations

from moss_harness.middleware.client_tool_search import ClientToolSearchMiddleware
from moss_harness.middleware.conversation_mode import ConversationModeMiddleware
from moss_harness.middleware.filesystem_guard import FilesystemGuardMiddleware
from moss_harness.middleware.media_offload import MediaOffloadMiddleware
from moss_harness.middleware.memory import MemoryMiddleware
from moss_harness.middleware.model_settings import (
    CONFIGURABLE_MAX_INPUT_TOKENS,
    CONFIGURABLE_MODEL_SETTINGS,
    ModelSettingsMiddleware,
)
from moss_harness.middleware.native_tool_search import NativeToolSearchMiddleware
from moss_harness.middleware.peer import PeerAgentMiddleware
from moss_harness.middleware.pii import detect_pii
from moss_harness.middleware.tool_search import ToolSearchMiddleware

__all__ = [
    "CONFIGURABLE_MAX_INPUT_TOKENS",
    "CONFIGURABLE_MODEL_SETTINGS",
    "ClientToolSearchMiddleware",
    "ConversationModeMiddleware",
    "FilesystemGuardMiddleware",
    "MediaOffloadMiddleware",
    "MemoryMiddleware",
    "ModelSettingsMiddleware",
    "NativeToolSearchMiddleware",
    "PeerAgentMiddleware",
    "ToolSearchMiddleware",
    "detect_pii",
]
