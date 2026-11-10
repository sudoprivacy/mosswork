"""Connect host-reviewed registration tuples to the maintained worker transports.

Current model grants, credentials and registration resolution belong to the host.
This adapter creates no authority, session, retry or replacement operation ID.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from octop.infra.sudograph.client import Operation
from octop.infra.sudograph.protocol import (
    REVIEW_PROFILE,
    SudographError,
    ToolResult,
    review_request,
    tool_request,
    write_unconfirmed,
)

from .fixed_json import (
    DispatchAssertion,
    FixedJsonError,
    FixedJsonTransport,
    assert_dispatch_current,
)
from .tool_protocol import (
    ToolConnection,
    ToolDescriptor,
    ToolProtocol,
    ToolProtocolError,
    open_tool_protocol,
)


def _failure(error: Exception) -> Exception:
    # Official MCP task groups can wrap transport failures. Keep their fixed
    # diagnostics without exposing provider text or losing possible effects.
    pending, leaves = [error], []
    while pending:
        item = pending.pop()
        if isinstance(item, ExceptionGroup):
            pending.extend(item.exceptions)
        else:
            leaves.append(item)
    if len(leaves) == 1:
        item = leaves[0]
        if isinstance(item, ToolProtocolError):
            return ToolProtocolError(str(item))
        if isinstance(item, FixedJsonError):
            return FixedJsonError(item.code)
        if isinstance(item, SudographError):
            return item
        if isinstance(item, PermissionError):
            return PermissionError("Registered reviewed worker access denied")
    return SudographError(
        "MODEL_OPERATION_UNAVAILABLE",
        "Registered model operation unavailable",
        "Check the bound worker and current authorization.",
    )


class RegisteredModelTransport[Registration]:
    """Consume Owner6's current reviewed tuples, including their I/O assertions.

    Registration is the host's immutable original binding, passed unchanged.
    Both resolvers must derive configuration from that binding and current
    independent grants. Neither a registration reference nor this object grants
    access. Human-only reads can use independent human grants without an Agent.
    """

    def __init__(
        self,
        *,
        resolve_tool: Callable[
            [Registration, Operation],
            Awaitable[tuple[ToolConnection, ToolDescriptor, DispatchAssertion]],
        ],
        resolve_review: Callable[
            [Registration, dict[str, Any]], Awaitable[tuple[ToolConnection, DispatchAssertion]]
        ],
    ) -> None:
        if not callable(resolve_tool) or not callable(resolve_review):
            raise ValueError("Require both current reviewed registration resolvers")
        self._tool, self._review = resolve_tool, resolve_review

    async def call_tool(
        self, registered: Registration, name: Operation, arguments: dict[str, Any]
    ) -> ToolResult:
        args = tool_request(name, arguments)
        connection, descriptor, current = await self._tool(registered, name)
        reviewed = copy.deepcopy(descriptor)
        if reviewed.tool.get("name") != name:
            raise ToolProtocolError("TOOL_DESCRIPTOR_CHANGED")
        protocol: ToolProtocol | None = None
        compiler_refused = False
        try:
            async with open_tool_protocol(connection, check_current=current) as protocol:
                result = await protocol.call_reviewed(reviewed, args)
                compiler_refused = result.get("isError") is True
                reply = ToolResult(result["content"], result.get("isError", False))
            await assert_dispatch_current(current)
            return reply
        except Exception as error:
            if (
                name == "sudograph_model_apply"
                and protocol is not None
                and protocol.call_started
                and not compiler_refused
            ):
                raise write_unconfirmed() from None
            raise _failure(error) from None

    async def read_review(
        self, registered: Registration, request: dict[str, Any]
    ) -> dict[str, Any]:
        value = copy.deepcopy(request)
        resource = value.pop("resource", None)
        if not isinstance(resource, str) or not resource:
            raise ValueError("Require the original registered review resource")
        args = {**review_request(value), "resource": resource}
        connection, current = await self._review(registered, copy.deepcopy(args))
        base = httpx.URL(connection.endpoint)
        if (
            str(base) != connection.endpoint
            or base.userinfo
            or base.query
            or base.fragment
            or connection.endpoint.endswith("/")
        ):
            raise FixedJsonError("INVALID_ENDPOINT")
        # Exact original new URL('review', config.endpoint), not /mcp/review.
        endpoint = str(base.join("review"))
        transport = FixedJsonTransport(
            endpoint,
            profile="review",
            allowed_addresses=connection.allowed_addresses,
            ca_file=connection.ca_file,
            before_dispatch=current,
        )
        try:
            async with httpx.AsyncClient(
                transport=transport,
                headers=dict(connection.headers),
                timeout=connection.timeout_ms / 1000,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(endpoint, json=args)
                try:
                    result = response.json()
                except ValueError:
                    result = None
                await assert_dispatch_current(current)
                if not response.is_success:
                    remote = result.get("error") if isinstance(result, dict) else None
                    code = (
                        "ACCESS_DENIED"
                        if response.status_code in (401, 403)
                        else remote
                        if remote
                        in (
                            "ACCESS_DENIED",
                            "CONTEXT_STALE",
                            "REVISION_UNKNOWN",
                            "SOURCE_SCHEMA_CHANGED",
                            "READ_CONTRACT",
                            "TARGET_UNKNOWN",
                            "CONTEXT_TARGET_UNKNOWN",
                            "VIEW_UNAVAILABLE",
                        )
                        else "VIEW_UNAVAILABLE"
                    )
                    raise SudographError(
                        code,
                        "Registered review request unavailable",
                        "Use the original model pointer under current authorization; ask the host operator if unavailable.",
                    )
            if not isinstance(result, dict) or result.get("format") != REVIEW_PROFILE:
                raise SudographError(
                    "WORKER_CONTRACT", "Incompatible registered review response", "Check review-v2."
                )
            return result
        except Exception as error:
            raise _failure(error) from None
