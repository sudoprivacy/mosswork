"""Independent human review through the existing registered worker transport.

The held canonical principal and mandatory full-model/projection grant remain
authoritative. No Agent, native session, page grant or comment queue is created.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any, Literal, Protocol

from octop.infra.connectors.fixed_json import DispatchAssertion
from octop.infra.connectors.registered_model import RegisteredModelTransport
from octop.infra.connectors.tool_protocol import ToolConnection, ToolDescriptor
from octop.infra.db.repos.retained_mcp import RetainedReviewedMcpRepo
from octop.infra.db.repos.retained_ontology_project import (
    RetainedOntologyProject,
    RetainedOntologyProjectRepo,
)
from octop.infra.db.repos.retained_review import RetainedHumanReviewDocumentRepo
from octop.infra.identity.request_principal import CanonicalAuthenticatedRequest
from octop.infra.identity.write_authority import CanonicalWriterAdmission, assert_canonical_writer
from octop.infra.sudograph.client import Operation, SudographClient
from octop.infra.sudograph.protocol import (
    ModelBinding,
    ReviewDocument,
    ToolResult,
    envelope,
    review_request,
)

from .reviewed_mcp import SecretList, _complete, _deny, _ReviewedConnectionResolver


class HumanReviewAccess(Protocol):
    """Owner6 supplies current full-model/document/source/projection/DLP grants."""

    async def __call__(
        self,
        principal: CanonicalAuthenticatedRequest,
        project: RetainedOntologyProject,
        model: ModelBinding,
        *,
        operation: Literal["review"],
        request: dict[str, Any],
    ) -> None: ...


class HumanReviewAudit(Protocol):
    """Actual canonical writer, including denied attempts after grant withdrawal.

    The original sealed principal attributes the event; error audit grants no
    new human effect. Append sanitized metadata with NULL native/session scope.
    """

    async def __call__(
        self,
        principal: CanonicalAuthenticatedRequest,
        project: RetainedOntologyProject,
        model: ModelBinding,
        status: Literal["success", "error"],
    ) -> None: ...


def _binding(project: RetainedOntologyProject) -> ModelBinding:
    if type(project) is not RetainedOntologyProject:
        raise _deny()
    return ModelBinding(
        project.authority_ref, project.model_ref, project.project_id, project.source_refs
    )


def _request(project: RetainedOntologyProject, value: dict[str, Any]) -> dict[str, Any]:
    _binding(project)
    if type(value) is not dict or value.get("resource") != project.project_id:
        raise _deny()
    selected = review_request({key: val for key, val in value.items() if key != "resource"})
    return {**selected, "resource": project.project_id}


class _HumanAccess:
    def __init__(
        self,
        principal: CanonicalAuthenticatedRequest,
        projects: RetainedOntologyProjectRepo,
        require_review_access: HumanReviewAccess,
    ) -> None:
        if type(principal) is not CanonicalAuthenticatedRequest or not callable(
            require_review_access
        ):
            raise _deny()
        # Accessing identity checks the real factory seal, including raw object
        # allocation. Revalidation uses this same private credential afterward.
        self.principal, self.original = principal, principal.identity
        self.projects, self.require_review_access = projects, require_review_access

    async def check(self, project: RetainedOntologyProject, request: dict[str, Any]) -> None:
        model = _binding(project)
        selected = _request(project, request)
        if (
            project.organization_id != self.original.snapshot.organization_id
            or await self.principal.revalidate() != self.original
        ):
            raise _deny()
        await asyncio.to_thread(self.projects.assert_current, project)
        await _complete(
            self.require_review_access(
                self.principal, project, model, operation="review", request=copy.deepcopy(selected)
            )
        )
        await asyncio.to_thread(self.projects.assert_current, project)
        if await self.principal.revalidate() != self.original:
            raise _deny()


class HumanReviewResolvers(_ReviewedConnectionResolver):
    """Project-only review tuples; every tool/context/write entry refuses.

    The same original reviewed MCP policy, visibility, secret definition/value
    rotation and post-DNS/TLS dispatch checks serve both human and session paths.
    """

    def __init__(
        self,
        *,
        principal: CanonicalAuthenticatedRequest,
        projects: RetainedOntologyProjectRepo,
        reviews: RetainedReviewedMcpRepo,
        require_review_access: HumanReviewAccess,
        list_secrets: SecretList | None = None,
        allowed_addresses: tuple[str, ...] | None = None,
    ) -> None:
        self._human = _HumanAccess(principal, projects, require_review_access)
        self._reviews = reviews
        self._actor = self._human.original.snapshot.member.owner_id
        self._list_secrets, self._addresses = list_secrets, allowed_addresses

    async def resolve_tool(
        self, project: RetainedOntologyProject, operation: Operation
    ) -> tuple[ToolConnection, ToolDescriptor, DispatchAssertion]:
        raise _deny()

    async def resolve_review(
        self, project: RetainedOntologyProject, request: dict[str, Any]
    ) -> tuple[ToolConnection, DispatchAssertion]:
        selected = _request(project, request)
        arguments = (
            {**selected.get("params", {}), "records": True}
            if selected["operation"] == "page"
            else None
        )

        async def current() -> None:
            await self._human.check(project, selected)

        connection, _, assertion = await self._resolve_connection(
            organization_id=project.organization_id,
            worker_ref=project.worker_ref,
            operation="review",
            require_access=current,
            review_arguments=arguments,
        )
        return connection, assertion


class _ReviewPorts:
    """One immutable request per invocation, so concurrent readers never mix grants."""

    def __init__(
        self,
        human: _HumanAccess,
        project: RetainedOntologyProject,
        request: dict[str, Any],
        transport: RegisteredModelTransport[RetainedOntologyProject],
        audit_review: HumanReviewAudit,
    ) -> None:
        self.human, self.project, self.model = human, project, _binding(project)
        self.request = _request(project, request)
        self.transport, self.audit_review = transport, audit_review

    def _match(self, binding: ModelBinding, operation: Operation) -> None:
        if type(binding) is not ModelBinding or binding != self.model or operation != "review":
            raise _deny()

    async def authorize(self, binding: ModelBinding, operation: Operation) -> None:
        self._match(binding, operation)
        await self.human.check(self.project, self.request)

    async def call_tool(
        self, binding: ModelBinding, name: Operation, arguments: dict[str, Any]
    ) -> ToolResult:
        raise _deny()

    async def read_review(self, binding: ModelBinding, request: dict[str, Any]) -> dict[str, Any]:
        self._match(binding, "review")
        if _request(self.project, request) != self.request:
            raise _deny()
        await self.authorize(binding, "review")
        response = await self.transport.read_review(self.project, copy.deepcopy(self.request))
        result = copy.deepcopy(envelope(response, binding, review=True))
        await self.authorize(binding, "review")
        return result

    async def audit(
        self, binding: ModelBinding, operation: Operation, status: Literal["success", "error"]
    ) -> None:
        self._match(binding, operation)
        await _complete(self.audit_review(self.human.principal, self.project, binding, status))
        if status == "success":
            # Audit SQL also awaits I/O; never release data using the earlier
            # admission if it was revoked while the canonical writer completed.
            await self.authorize(binding, "review")


class HumanReviewReader:
    """Validated replies and immutable human document capture before page release."""

    def __init__(
        self,
        *,
        principal: CanonicalAuthenticatedRequest,
        project: RetainedOntologyProject,
        projects: RetainedOntologyProjectRepo,
        require_review_access: HumanReviewAccess,
        transport: RegisteredModelTransport[RetainedOntologyProject],
        audit_review: HumanReviewAudit,
    ) -> None:
        self.binding = _binding(project)
        if not callable(audit_review):
            raise _deny()
        self._human = _HumanAccess(principal, projects, require_review_access)
        if project.organization_id != self._human.original.snapshot.organization_id:
            # A foreign project must never enter this principal's audit scope,
            # even when the first grant check would later deny its read.
            raise _deny()
        self._project, self._transport, self._audit = project, transport, audit_review

    def _client(self, request: dict[str, Any]) -> SudographClient:
        selected = {**review_request(request), "resource": self._project.project_id}
        ports = _ReviewPorts(self._human, self._project, selected, self._transport, self._audit)
        return SudographClient(self.binding, ports)

    async def review(self, request: dict[str, Any]) -> dict[str, Any]:
        selected = review_request(request)
        return await self._client(selected).review(selected)

    async def review_document(self, endpoint: str) -> ReviewDocument:
        return await self._client({"operation": "document", "endpoint": endpoint}).review_document(
            endpoint
        )

    def _document_store(self, store: RetainedHumanReviewDocumentRepo) -> None:
        if (
            type(store) is not RetainedHumanReviewDocumentRepo
            or store._uri + "?mode=ro" != self._human.projects._uri
        ):
            raise _deny()

    async def capture_review(
        self,
        endpoint: str,
        store: RetainedHumanReviewDocumentRepo,
        *,
        writer: CanonicalWriterAdmission,
    ) -> ReviewDocument:
        """Return unchanged HTML only after original project/actor bytes commit.

        A cancelled/failed wait releases no page even if SQL commits afterward.
        Explicit restore uses the same original endpoint, never a worker retry,
        new session, document ID or latest-head substitute.
        """
        self._document_store(store)
        request = {
            "operation": "document",
            "endpoint": endpoint,
            "resource": self._project.project_id,
        }
        await assert_canonical_writer(writer)
        document = await self.review_document(endpoint)
        await assert_canonical_writer(writer)
        await self._human.check(self._project, request)
        snapshot = self._human.original.snapshot
        await _complete(
            asyncio.to_thread(
                store.save,
                self._project,
                endpoint,
                record=document.record(),
                html=document.html,
                account_id=snapshot.account_id,
                actor_id=snapshot.member.owner_id,
            )
        )
        await assert_canonical_writer(writer)
        await self._human.check(self._project, request)
        return document

    async def restore_review(
        self, endpoint: str, store: RetainedHumanReviewDocumentRepo
    ) -> ReviewDocument:
        """Authorize original persisted bytes before serving; never refetch HTML.

        The existing independent audit writer is required after read I/O.
        Restoring a page authenticates neither a comment nor native completion.
        """
        self._document_store(store)
        request = {
            "operation": "document",
            "endpoint": endpoint,
            "resource": self._project.project_id,
        }
        model, snapshot = self.binding, self._human.original.snapshot
        try:
            await self._human.check(self._project, request)
            stored = await asyncio.to_thread(
                store.load,
                self._project,
                endpoint,
                account_id=snapshot.account_id,
                actor_id=snapshot.member.owner_id,
            )
            document = ReviewDocument.restore(model, stored.record, stored.html)
            await _complete(self._audit(self._human.principal, self._project, model, "success"))
            await self._human.check(self._project, request)
            return document
        except asyncio.CancelledError:
            raise
        except Exception:
            await _complete(self._audit(self._human.principal, self._project, model, "error"))
            raise
