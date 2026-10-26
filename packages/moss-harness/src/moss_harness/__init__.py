"""moss-harness: production-grade Harness Agent on top of LangChain Deep Agents."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from moss_harness._version import __version__
from moss_harness.config import (
    HarnessAgentConfig,
    MediaGenerationConfig,
    MediaProviderConfig,
    ModelConfig,
    ProviderConfig,
)
from moss_harness.request import ChatRequest

if TYPE_CHECKING:
    from moss_harness.agent import HarnessAgent
    from moss_harness.init import InitResult, init_workspace
    from moss_harness.manager import AgentEntry, HarnessAgentManager
    from moss_harness.observability.logging import (
        current_log_file,
        default_log_dir,
        setup_logging,
        teardown_logging,
    )
    from moss_harness.protocols.langgraph import AgentEventType
    from moss_harness.providers import ModelPreset, ProviderPreset

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "AgentEntry": ("moss_harness.manager", "AgentEntry"),
    "HarnessAgentManager": ("moss_harness.manager", "HarnessAgentManager"),
    "ModelPreset": ("moss_harness.providers", "ModelPreset"),
    "ProviderPreset": ("moss_harness.providers", "ProviderPreset"),
    "HarnessAgent": ("moss_harness.agent", "HarnessAgent"),
    "InitResult": ("moss_harness.init", "InitResult"),
    "init_workspace": ("moss_harness.init", "init_workspace"),
    "current_log_file": ("moss_harness.observability.logging", "current_log_file"),
    "default_log_dir": ("moss_harness.observability.logging", "default_log_dir"),
    "setup_logging": ("moss_harness.observability.logging", "setup_logging"),
    "teardown_logging": ("moss_harness.observability.logging", "teardown_logging"),
    "ChatProtocol": ("moss_harness.protocols", "ChatProtocol"),
    "register_protocol": ("moss_harness.protocols", "register_protocol"),
    "resolve_protocol": ("moss_harness.protocols", "resolve_protocol"),
    "AgentEventType": ("moss_harness.protocols.langgraph", "AgentEventType"),
    "SecurityPolicy": ("moss_harness.security.models", "SecurityPolicy"),
}


def __getattr__(name: str) -> object:  # pragma: no cover - thin re-export shim
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    return getattr(import_module(module_name), attr)


__all__ = [
    "AgentEntry",
    "AgentEventType",
    "ChatProtocol",
    "ChatRequest",
    "HarnessAgent",
    "HarnessAgentConfig",
    "HarnessAgentManager",
    "InitResult",
    "MediaGenerationConfig",
    "MediaProviderConfig",
    "ModelConfig",
    "ModelPreset",
    "ProviderConfig",
    "ProviderPreset",
    "SecurityPolicy",
    "__version__",
    "current_log_file",
    "default_log_dir",
    "init_workspace",
    "register_protocol",
    "resolve_protocol",
    "setup_logging",
    "teardown_logging",
]
