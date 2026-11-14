"""Resolve the original reviewed MCP worker for the registered transport.

The existing sealed-actor/full-model/native/DLP assertion is mandatory. This
ports original ReviewedMcpAccess and systemMcpAuthSecrets; it grants no task or
native effect and supplies no replacement credential or secret service.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from octop.infra.connectors.fixed_json import DispatchAssertion
from octop.infra.connectors.tool_protocol import (
    ToolConnection,
    ToolDescriptor,
    validate_host_read_arguments,
)
from octop.infra.sudograph.client import Operation
from octop.infra.sudograph.protocol import REVIEW_PROFILE, ModelBinding, review_request

if TYPE_CHECKING:
    from octop.infra.db.repos.retained_mcp import RetainedReviewedMcp, RetainedReviewedMcpRepo
    from octop.infra.db.repos.retained_ontology import (
        RetainedOntologyBinding,
        RetainedOntologyBindingRepo,
    )

type RegisteredAccess = Callable[[RetainedOntologyBinding, Operation], Awaitable[None]]
type SecretList = Callable[[str, str], Awaitable[list[dict[str, Any]]]]


def _deny() -> PermissionError:
    return PermissionError("Registered reviewed worker access is unavailable")


async def _complete(pending: Awaitable[object]) -> None:
    if await pending is not None:
        raise _deny()


def _inline_headers(row: RetainedReviewedMcp) -> tuple[tuple[str, str], ...]:
    if row.auth_type == "none":
        return ()
    try:
        config = json.loads(row.auth_config_json or "null")
    except (ValueError, TypeError):
        raise _deny() from None
    if type(config) is not dict:
        raise _deny()
    if row.auth_type == "custom_header" or "header_name" not in config:
        result = {key: value for key, value in config.items() if type(value) is str}
    else:
        header = config.get("header_name")
        if type(header) is not str or not header:
            raise _deny()
        if row.auth_type == "bearer":
            prefix, token = config.get("prefix"), config.get("token")
            if type(token) is not str or type(prefix) is not str:
                raise _deny()
            result = {header: f"{prefix} {token}" if prefix else token}
        elif row.auth_type == "basic":
            username, password = config.get("username"), config.get("password")
            if type(username) is not str or type(password) is not str:
                raise _deny()
            result = {
                header: "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
            }
        elif row.auth_type == "api_key" and type(config.get("api_key")) is str:
            result = {header: config["api_key"]}
        else:
            raise _deny()
    if not result or any(not value for value in result.values()):
        raise _deny()
    return tuple(result.items())


class _ReviewedConnectionResolver:
    """Share reviewed connection/secret fences, without supplying access grants."""

    _reviews: RetainedReviewedMcpRepo
    _actor: str
    _list_secrets: SecretList | None
    _addresses: tuple[str, ...] | None

    async def _headers(
        self, row: RetainedReviewedMcp, current: DispatchAssertion
    ) -> tuple[tuple[str, str], ...]:
        if row.auth_type != "secret_ref":
            return _inline_headers(row)
        if self._list_secrets is None or not row.secret_ref:
            raise _deny()
        await current()
        definition = await asyncio.to_thread(
            self._reviews.secret_definition, row.organization_id, row.secret_ref
        )
        await current()
        # These are the original org-scoped namespace and system subject.
        values = await self._list_secrets(
            f"org:{row.organization_id}:system:{definition.pinyin}",
            f"system:moss:{row.organization_id}",
        )
        await current()
        if (
            await asyncio.to_thread(
                self._reviews.secret_definition, row.organization_id, row.secret_ref
            )
            != definition
        ):
            raise _deny()
        if type(values) is not list:
            raise _deny()
        by_key: dict[str, str] = {}
        seen: set[str] = set()
        for value in values:
            if type(value) is not dict or type(value.get("key")) is not str:
                raise _deny()
            key, secret = value["key"], value.get("value")
            if key in seen or secret is not None and type(secret) is not str:
                raise _deny()
            seen.add(key)
            if secret is not None:
                by_key[key] = secret
        if definition.scheme == "header":
            headers = tuple((key, by_key[key]) for key in definition.entries if key in by_key)
        else:
            secret = by_key.get(definition.entries[0])
            if not secret:
                raise _deny()
            prefix = definition.bearer_prefix if definition.bearer_prefix is not None else "Bearer"
            formatted = (
                f"{prefix} {secret}"
                if definition.scheme == "bearer"
                else "Basic " + base64.b64encode(secret.encode()).decode()
            )
            headers = (("Authorization", formatted),)
        if not headers:
            raise _deny()
        return headers

    async def _resolve_connection(
        self,
        *,
        organization_id: str,
        worker_ref: str,
        operation: Operation,
        require_access: DispatchAssertion,
        review_arguments: dict[str, Any] | None = None,
    ) -> tuple[ToolConnection, ToolDescriptor, DispatchAssertion]:
        if operation not in (
            "sudograph_context",
            "sudograph_context_read",
            "sudograph_model_contract",
            "sudograph_model_apply",
            "review",
        ):
            raise _deny()
        await require_access()
        name = "sudograph_context_read" if operation == "review" else operation
        row = await asyncio.to_thread(
            self._reviews.resolve,
            organization_id,
            self._actor,
            worker_ref,
            name,
        )
        document = json.loads(row.review_document)
        if document["action"] != ("write" if operation == "sudograph_model_apply" else "read"):
            raise _deny()
        if operation == "review":
            metadata = document["tool"].get("_meta")
            if (
                type(metadata) is not dict
                or metadata.get("moss/host-review-profile") != REVIEW_PROFILE
            ):
                raise _deny()
            # Refuse unapproved page fields/record scopes and unsupported read
            # schemas before using the authorized secret-list capability.
            validate_host_read_arguments(document["tool"], review_arguments)

        async def current_rows() -> None:
            await require_access()
            await asyncio.to_thread(self._reviews.assert_current, row)
            await require_access()

        await current_rows()
        headers = await self._headers(row, current_rows)
        connection = ToolConnection(row.endpoint, headers, row.timeout_ms, self._addresses)

        async def current() -> None:
            await current_rows()
            # Original withHostRead fences current credential values as part
            # of connection identity, including rotation during DNS/TLS waits.
            # Normalize header names with the same public constructor.
            refreshed = ToolConnection(
                row.endpoint,
                await self._headers(row, current_rows),
                row.timeout_ms,
                self._addresses,
            )
            if refreshed != connection:
                raise _deny()
            await current_rows()

        await current()
        descriptor = ToolDescriptor(document["protocol"], document["tool"], document["digest"])
        return connection, descriptor, current


class ReviewedModelResolvers(_ReviewedConnectionResolver):
    """Actual tuple producers for PC2's one registered transport adapter.

    check_access must revalidate the held original sealed actor plus the full
    registered graph/action and applicable native/DLP authority. Execution
    composition also checks its host Ready; a separate human-only composition
    supplies its real human/document/source grant without requiring native Ready.
    list_secrets is the original authorized Nexus service capability, not the
    session SDK, a worker token or an operator credential exposed to a child.
    """

    def __init__(
        self,
        *,
        reviews: RetainedReviewedMcpRepo,
        registry: RetainedOntologyBindingRepo,
        actor_member_id: str,
        check_access: RegisteredAccess,
        list_secrets: SecretList | None = None,
        allowed_addresses: tuple[str, ...] | None = None,
    ) -> None:
        if not actor_member_id or not callable(check_access):
            raise ValueError("Require the original actor and actual current access assertion")
        self._reviews, self._registry = reviews, registry
        self._actor, self._check_access = actor_member_id, check_access
        self._list_secrets, self._addresses = list_secrets, allowed_addresses

    async def _access(self, registered: RetainedOntologyBinding, operation: Operation) -> None:
        from octop.infra.db.repos.retained_ontology import RetainedOntologyBinding

        if type(registered) is not RetainedOntologyBinding:
            raise _deny()
        # Keep the domain's exact codec: refuse incompatible original model or
        # resource refs rather than normalizing them into another identity.
        ModelBinding(
            registered.authority_ref,
            registered.model_ref,
            registered.project_id,
            registered.source_refs,
        )
        await _complete(self._check_access(registered, operation))
        await asyncio.to_thread(self._registry.assert_current, registered)
        await _complete(self._check_access(registered, operation))

    async def _resolve(
        self,
        registered: RetainedOntologyBinding,
        operation: Operation,
        *,
        review_arguments: dict[str, Any] | None = None,
    ) -> tuple[ToolConnection, ToolDescriptor, DispatchAssertion]:
        if operation not in (
            "sudograph_context",
            "sudograph_context_read",
            "sudograph_model_contract",
            "sudograph_model_apply",
            "review",
        ):
            raise _deny()
        await self._access(registered, operation)

        async def access() -> None:
            await self._access(registered, operation)

        return await self._resolve_connection(
            organization_id=registered.organization_id,
            worker_ref=registered.worker_ref,
            operation=operation,
            require_access=access,
            review_arguments=review_arguments,
        )

    async def resolve_tool(
        self, registered: RetainedOntologyBinding, operation: Operation
    ) -> tuple[ToolConnection, ToolDescriptor, DispatchAssertion]:
        if operation == "review":
            raise _deny()
        return await self._resolve(registered, operation)

    async def resolve_review(
        self, registered: RetainedOntologyBinding, request: dict[str, Any]
    ) -> tuple[ToolConnection, DispatchAssertion]:
        # PC2 snapshots/validates the full review-v2 request and derives the
        # fixed /review endpoint from this approved original MCP endpoint.
        if type(request) is not dict or request.get("resource") != registered.project_id:
            raise _deny()
        value = request.copy()
        value.pop("resource")
        selected = review_request(value)
        # Original withHostRead validates page params as actual record reads.
        # Other review operations validate the schema without invoking the MCP
        # tool or fabricating its required argument values. Capture before waits.
        arguments = (
            {**selected.get("params", {}), "records": True}
            if selected["operation"] == "page"
            else None
        )
        connection, _, current = await self._resolve(
            registered, "review", review_arguments=arguments
        )
        return connection, current
