"""Bounded official MCP client for existing reviewed registrations (MIT).

Ports Moss d0b84d4c agentFederation/toolProtocol.ts. This is not a review store,
credential resolver, model grant or native execution/session authority.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import rfc8785
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .fixed_json import (
    DispatchAssertion,
    FixedJsonTransport,
    assert_dispatch_current,
    validate_auth_headers,
)


class ToolProtocolError(RuntimeError):
    """A sanitized refusal. No operation is automatically replayed."""


def content_digest(value: Any) -> str:
    try:
        return "sha256:" + hashlib.sha256(rfc8785.dumps(value)).hexdigest()
    except (ValueError, TypeError):
        raise ToolProtocolError("TOOL_DESCRIPTOR_INVALID") from None


def _encoded(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    except (ValueError, TypeError, UnicodeError):
        raise ToolProtocolError("INVALID_TOOL_ARGUMENTS") from None


def _length(value: str) -> int:
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def _finite(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def snapshot_arguments(value: object) -> dict[str, Any]:
    """Keep the original bounded-record profile; copy before the first await."""
    nodes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 256 or depth > 6:
            raise ToolProtocolError("INVALID_TOOL_ARGUMENTS")
        if item is None or type(item) is bool:
            return
        if type(item) is str and _length(item) <= 8192:
            return
        if _finite(item):
            return
        if type(item) is list and len(item) <= 100:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict and len(item) <= 32:
            for key, child in item.items():
                if (
                    not isinstance(key, str)
                    or not 1 <= _length(key) <= 128
                    or re.search(r"[\x00-\x1f\x7f]", key)
                    or key in ("__proto__", "prototype", "constructor")
                ):
                    raise ToolProtocolError("INVALID_TOOL_ARGUMENTS")
                visit(child, depth + 1)
            return
        raise ToolProtocolError("INVALID_TOOL_ARGUMENTS")

    if type(value) is not dict:
        raise ToolProtocolError("INVALID_TOOL_ARGUMENTS")
    visit(value, 0)
    if len(_encoded(value)) > 32000:
        raise ToolProtocolError("INVALID_TOOL_ARGUMENTS")
    return copy.deepcopy(value)


def validate_arguments(tool: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Validate the original closed scalar/bounded-record descriptor profile."""
    schema, meta = tool.get("inputSchema", {}), tool.get("_meta", {})
    if type(schema) is not dict or type(meta) is not dict:
        raise ToolProtocolError("TOOL_SCHEMA_UNSUPPORTED")
    profile = meta.get("moss/input-profile")
    if profile not in (None, "bounded-record-v1"):
        raise ToolProtocolError("TOOL_INPUT_PROFILE_UNSUPPORTED")
    properties = schema.get("properties")
    required = schema.get("required", [])
    if (
        schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or type(properties) is not dict
        or len(properties) > 32
        or set(schema)
        - {
            "type",
            "properties",
            "required",
            "additionalProperties",
            "description",
            "title",
            "$schema",
        }
        or type(required) is not list
        or any(not isinstance(key, str) or key not in properties for key in required)
    ):
        raise ToolProtocolError("TOOL_SCHEMA_UNSUPPORTED")
    for name, spec in properties.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) or type(spec) is not dict:
            raise ToolProtocolError("TOOL_SCHEMA_UNSUPPORTED")
        kind = spec.get("type")
        if kind == "object" and profile == "bounded-record-v1":
            if (
                set(spec)
                - {"type", "description", "title", "additionalProperties", "propertyNames"}
                or spec.get("additionalProperties") != {}
                or ("propertyNames" in spec and spec["propertyNames"] != {"type": "string"})
            ):
                raise ToolProtocolError("TOOL_SCHEMA_UNSUPPORTED")
        else:
            if kind not in ("string", "boolean", "integer", "number") or set(spec) - {
                "type",
                "description",
                "title",
                "enum",
                "maxLength",
                "minLength",
                "minimum",
                "maximum",
            }:
                raise ToolProtocolError("TOOL_SCHEMA_UNSUPPORTED")
            for limit in ("maxLength", "minLength", "minimum", "maximum"):
                if limit in spec and (not _finite(spec[limit])):
                    raise ToolProtocolError("TOOL_SCHEMA_UNSUPPORTED")
            if "enum" in spec and (
                type(spec["enum"]) is not list
                or not 1 <= len(spec["enum"]) <= 64
                or any(type(v) not in (str, int, float, bool) for v in spec["enum"])
            ):
                raise ToolProtocolError("TOOL_SCHEMA_UNSUPPORTED")
    if set(arguments) - properties.keys() or any(key not in arguments for key in required):
        raise ToolProtocolError("INVALID_TOOL_ARGUMENTS")
    for key, value in arguments.items():
        spec = properties[key]
        kind = spec["type"]
        valid = (
            (kind == "object" and type(value) is dict)
            or (kind == "boolean" and type(value) is bool)
            or (
                kind == "string"
                and type(value) is str
                and spec.get("minLength", 0)
                <= _length(value)
                <= min(spec.get("maxLength", 8192), 8192)
            )
            or (
                kind in ("integer", "number")
                and type(value) in (int, float)
                and _finite(value)
                and spec.get("minimum", -math.inf) <= value <= spec.get("maximum", math.inf)
                and (kind != "integer" or (value == int(value) and abs(value) <= 9007199254740991))
            )
        )
        if not valid or (
            "enum" in spec
            and not any(
                (type(v) is type(value) or type(v) in (int, float) and type(value) in (int, float))
                and v == value
                for v in spec["enum"]
            )
        ):
            raise ToolProtocolError("INVALID_TOOL_ARGUMENTS")


@dataclass(frozen=True, slots=True)
class ToolConnection:
    endpoint: str
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    timeout_ms: int = 10000
    allowed_addresses: tuple[str, ...] | None = None
    ca_file: str | None = None

    def __post_init__(self) -> None:
        if type(self.timeout_ms) is not int or not 1000 <= self.timeout_ms <= 15000:
            raise ToolProtocolError("TOOL_TIMEOUT_INVALID")
        headers = httpx.Headers(self.headers)
        validate_auth_headers(headers)
        object.__setattr__(self, "headers", tuple(headers.multi_items()))
        if self.allowed_addresses is not None:
            object.__setattr__(self, "allowed_addresses", tuple(self.allowed_addresses))


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    protocol: str
    tool: dict[str, Any] = field(repr=False)
    digest: str


class ToolProtocol:
    def __init__(self, session: ClientSession, version: str, current: DispatchAssertion) -> None:
        self.session, self.version, self.current = session, version, current

    async def _current(self) -> None:
        await assert_dispatch_current(self.current)

    async def descriptors(self) -> list[ToolDescriptor]:
        found: list[ToolDescriptor] = []
        names, cursors = set(), set()
        cursor = None
        for _ in range(10):
            await self._current()
            page = await self.session.list_tools(cursor=cursor)
            await self._current()
            if len(found) + len(page.tools) > 200:
                raise ToolProtocolError("TOOL_DISCOVERY_LIMIT")
            for tool in page.tools:
                raw = tool.model_dump(mode="json", by_alias=True, exclude_unset=True)
                if (
                    not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", tool.name)
                    or tool.name in names
                    or len(_encoded(raw)) > 64000
                ):
                    raise ToolProtocolError("TOOL_DESCRIPTOR_INVALID")
                names.add(tool.name)
                found.append(
                    ToolDescriptor(
                        self.version, raw, content_digest({"protocol": self.version, "tool": raw})
                    )
                )
            cursor = page.nextCursor
            if not cursor:
                return found
            if len(cursor) > 1000 or cursor in cursors:
                raise ToolProtocolError("TOOL_DISCOVERY_LIMIT")
            cursors.add(cursor)
        raise ToolProtocolError("TOOL_DISCOVERY_LIMIT")

    async def call_reviewed(self, reviewed: ToolDescriptor, arguments: object) -> dict[str, Any]:
        # No caller-owned mapping survives an authorization/discovery await.
        args = snapshot_arguments(arguments)
        expected = copy.deepcopy(reviewed)
        validate_arguments(expected.tool, args)
        live = next(
            (d for d in await self.descriptors() if d.tool["name"] == expected.tool["name"]), None
        )
        if (
            live is None
            or live.protocol != expected.protocol
            or live.digest != expected.digest
            or content_digest({"protocol": expected.protocol, "tool": expected.tool})
            != expected.digest
        ):
            raise ToolProtocolError("TOOL_DESCRIPTOR_CHANGED")
        await self._current()
        try:
            result = await self.session.call_tool(expected.tool["name"], args)
            await self._current()
        except Exception:
            # The worker may have committed before a lost reply or revocation.
            # Keep cancellation untouched; never classify this as zero effect.
            raise ToolProtocolError("TOOL_EXECUTION_UNCONFIRMED") from None
        raw = result.model_dump(mode="json", by_alias=True, exclude_unset=True)
        if (
            len(result.content) > 128
            or any(p.type != "text" for p in result.content)
            or len(_encoded(raw)) > 128000
        ):
            raise ToolProtocolError("TOOL_OUTPUT_UNSUPPORTED")
        return {"content": raw["content"], **({"isError": True} if result.isError else {})}


@asynccontextmanager
async def open_tool_protocol(
    connection: ToolConnection, *, check_current: DispatchAssertion
) -> AsyncIterator[ToolProtocol]:
    """Use in one task/scope; current grants remain an injected host authority.

    The current assertion is mandatory at discovery/call boundaries AND after
    DNS/TLS waits before each authenticated HTTP write. False/JSON verdicts
    cannot supply a grant. Transport closure is never native cleanup evidence.
    """
    transport = FixedJsonTransport(
        connection.endpoint,
        profile="mcp",
        allowed_addresses=connection.allowed_addresses,
        ca_file=connection.ca_file,
        before_dispatch=check_current,
    )
    async with (
        httpx.AsyncClient(
            transport=transport,
            headers=dict(connection.headers),
            timeout=connection.timeout_ms / 1000,
            follow_redirects=False,
            trust_env=False,
        ) as http,
        streamable_http_client(connection.endpoint, http_client=http, terminate_on_close=False) as (
            read,
            write,
            get_session_id,
        ),
        ClientSession(
            read, write, read_timeout_seconds=timedelta(milliseconds=connection.timeout_ms)
        ) as session,
    ):
        await assert_dispatch_current(check_current)
        initialization = await session.initialize()
        if initialization.protocolVersion not in ("2025-06-18", "2025-11-25"):
            raise ToolProtocolError("TOOL_PROTOCOL_UNSUPPORTED")
        try:
            yield ToolProtocol(session, initialization.protocolVersion, check_current)
        finally:
            session_id = get_session_id()
            if session_id is not None:
                try:
                    async with asyncio.timeout(1):
                        await http.delete(
                            connection.endpoint,
                            headers={
                                "mcp-session-id": session_id,
                                "mcp-protocol-version": initialization.protocolVersion,
                            },
                        )
                except Exception:
                    # A failed termination cannot erase the original result or
                    # cancellation, or establish native writer cleanup.
                    pass
