"""Conservative side-effect annotations for company connector tools.

The broker uses these annotations for approval and audit decisions.  Gateway
adapters expose stable tool names in source; remote MCP servers are treated as
writes unless their metadata clearly identifies a read-only operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SideEffectLevel = Literal[
    "read_only", "external_query", "write", "message_send", "credential_change"
]


@dataclass(frozen=True)
class ToolSideEffect:
    level: SideEffectLevel
    reason: str


READ_ONLY = ToolSideEffect("read_only", "does not change external state")
EXTERNAL_QUERY = ToolSideEffect("external_query", "sends the query to an external service")
WRITE = ToolSideEffect("write", "may change external state")
MESSAGE_SEND = ToolSideEffect("message_send", "sends a message to another person or channel")
CREDENTIAL_CHANGE = ToolSideEffect("credential_change", "changes connector credentials")


def _method_name(arguments: dict[str, Any]) -> str:
    return str(arguments.get("method") or "").strip().lower()


_GATEWAY_LEVELS: dict[str, dict[str, ToolSideEffect]] = {
    "agently-cli": {
        "agently_me": READ_ONLY,
        "agently_list": READ_ONLY,
        "agently_read": READ_ONLY,
        "agently_search": EXTERNAL_QUERY,
        "agently_send": MESSAGE_SEND,
        "agently_reply": MESSAGE_SEND,
        "agently_forward": MESSAGE_SEND,
        "agently_trash": WRITE,
        "agently_delete": WRITE,
        "agently_upload": WRITE,
        "agently_download": READ_ONLY,
    },
    "qq-mail": {
        "search_emails": EXTERNAL_QUERY,
        "read_email": READ_ONLY,
        "send_email": MESSAGE_SEND,
    },
    "qq-music": dict.fromkeys(
        (
            "search_music",
            "list_charts",
            "get_chart_detail",
            "get_playlist_detail",
            "listening_report",
        ),
        EXTERNAL_QUERY,
    ),
    "fliggy": dict.fromkeys(("fliggy_ai_search", "fliggy_fast_search"), EXTERNAL_QUERY),
    "baidu-map": dict.fromkeys(("search_place", "plan_direction", "get_weather"), EXTERNAL_QUERY),
    "ctrip-wendao": {"ask_wendao": EXTERNAL_QUERY},
    "meituan-travel": {"travel_query": EXTERNAL_QUERY},
    "yuandian": dict.fromkeys(
        (
            "search_laws",
            "search_cases",
            "search_enterprises",
            "get_enterprise",
            "detect_hallucination",
        ),
        EXTERNAL_QUERY,
    ),
    "tencent-ima": {
        name: (
            WRITE
            if name in {"create_note", "append_note", "import_urls", "add_note_to_knowledge"}
            else READ_ONLY
        )
        for name in (
            "list_notebooks",
            "list_notes",
            "get_note",
            "search_notes",
            "create_note",
            "append_note",
            "list_knowledge_bases",
            "list_addable_knowledge_bases",
            "get_knowledge_base",
            "list_knowledge",
            "search_knowledge",
            "import_urls",
            "add_note_to_knowledge",
            "get_media",
        )
    },
    "tencent-news": {"search_news": EXTERNAL_QUERY},
    "wechat-reading": {"list_bookshelf": READ_ONLY, "get_book_notes": READ_ONLY},
    "feishu-cli": {
        "doc": WRITE,
        "base": WRITE,
        "calendar": WRITE,
        "im": MESSAGE_SEND,
        "help": READ_ONLY,
    },
    "wecom-cli": {
        "doc": WRITE,
        "schedule": WRITE,
        "msg": MESSAGE_SEND,
        "help": READ_ONLY,
    },
    "weknora": {
        "list_knowledge_bases": READ_ONLY,
        "search": EXTERNAL_QUERY,
        "read_document": READ_ONLY,
    },
}

_ADAPTER_MANAGED_CONFIRMATIONS = {
    "agently-cli": frozenset(
        {"agently_send", "agently_reply", "agently_forward", "agently_trash", "agently_delete"}
    )
}


def gateway_tool_side_effect(
    kind: str, tool_name: str, arguments: dict[str, Any] | None = None
) -> ToolSideEffect:
    """Return the strict annotation for one gateway tool invocation."""
    method = _method_name(arguments or {})
    if kind in {"feishu-cli", "wecom-cli"}:
        if tool_name == "help":
            return READ_ONLY
        if method.startswith(
            (
                "+search",
                "+fetch",
                "+get",
                "+list",
                "+read",
                "search",
                "get",
                "list",
                "read",
                "fetch",
            )
        ):
            return (
                EXTERNAL_QUERY
                if any(word in method for word in ("search", "query", "lookup"))
                else READ_ONLY
            )
        if tool_name == "im" or tool_name == "msg":
            return MESSAGE_SEND
        return WRITE
    if kind == "agently-cli" and tool_name in {
        "agently_send",
        "agently_reply",
        "agently_forward",
        "agently_trash",
        "agently_delete",
    }:
        return _GATEWAY_LEVELS[kind][tool_name]
    return _GATEWAY_LEVELS.get(kind, {}).get(tool_name, WRITE)


def remote_tool_side_effect(tool_name: str, tool: dict[str, Any] | None = None) -> ToolSideEffect:
    """Classify remote MCP metadata conservatively.

    A tool is read-only only when its name and description clearly express a
    read/query operation.  Unknown metadata remains a write and therefore
    requires approval.
    """
    raw = " ".join(
        part
        for part in (
            tool_name,
            str((tool or {}).get("description") or ""),
        )
        if part
    ).lower()
    if any(word in raw for word in ("credential", "change_password", "disconnect", "revoke_token")):
        return CREDENTIAL_CHANGE
    if any(
        word in raw
        for word in (
            "send",
            "create",
            "update",
            "delete",
            "remove",
            "write",
            "append",
            "publish",
            "move",
            "rename",
            "upload",
        )
    ):
        return (
            MESSAGE_SEND
            if any(word in raw for word in ("send", "message", "email", "im"))
            else WRITE
        )
    if any(
        word in raw
        for word in ("search", "query", "list", "get", "read", "fetch", "find", "lookup", "weather")
    ):
        return (
            EXTERNAL_QUERY
            if any(word in raw for word in ("search", "query", "lookup", "weather"))
            else READ_ONLY
        )
    return WRITE


def side_effect_for(
    kind: str,
    tool_name: str,
    tool: dict[str, Any] | None = None,
    arguments: dict[str, Any] | None = None,
) -> ToolSideEffect:
    if kind in _GATEWAY_LEVELS:
        return gateway_tool_side_effect(kind, tool_name, arguments)
    return remote_tool_side_effect(tool_name, tool)


def adapter_manages_confirmation(kind: str, tool_name: str) -> bool:
    """Return whether the adapter performs its own write confirmation flow."""
    return tool_name in _ADAPTER_MANAGED_CONFIRMATIONS.get(kind, frozenset())


__all__ = [
    "CREDENTIAL_CHANGE",
    "EXTERNAL_QUERY",
    "MESSAGE_SEND",
    "READ_ONLY",
    "WRITE",
    "SideEffectLevel",
    "ToolSideEffect",
    "adapter_manages_confirmation",
    "gateway_tool_side_effect",
    "remote_tool_side_effect",
    "side_effect_for",
]
