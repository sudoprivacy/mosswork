"""In-process MCP gateway for mosswork-hosted connector adapters."""

from mosswork.infra.connectors.gateway.langchain import build_gateway_langchain_tools
from mosswork.infra.connectors.gateway.protocol import handle_mcp_request
from mosswork.infra.connectors.gateway.registry import mcp_tools_for_kind

__all__ = [
    "build_gateway_langchain_tools",
    "handle_mcp_request",
    "mcp_tools_for_kind",
]
