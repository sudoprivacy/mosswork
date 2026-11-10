"""Compose registered Sudograph domain ports with original host authorities.

The injected grant and transport services remain authoritative. These ports
recheck the retained bindings at service I/O and append to the existing audit;
they do not register models, issue grants or admit review comments.
"""

from __future__ import annotations

import asyncio
import copy
from typing import TYPE_CHECKING, Any, Literal, Protocol

from octop.infra.sudograph.client import Operation, SudographClient
from octop.infra.sudograph.protocol import (
    ModelBinding,
    ModelPointer,
    ReviewDocument,
    ToolResult,
    envelope,
    review_request,
    tool_request,
)

from .context import (
    BackendHostContextResolver,
    CohostSessionBinding,
    HostBinding,
    HostUnavailable,
    TrustedHostContext,
    _require_completion,
    _required_strings,
)

if TYPE_CHECKING:
    from octop.infra.db.repos.retained_cohost import (
        RetainedCohostBinding,
        RetainedCohostBindingRepo,
    )
    from octop.infra.db.repos.retained_model_audit import RetainedModelAuditRepo
    from octop.infra.db.repos.retained_ontology import (
        RetainedOntologyBinding,
        RetainedOntologyBindingRepo,
    )
    from octop.infra.db.repos.retained_review import RetainedReviewDocumentRepo

_OPERATIONS = frozenset(
    {
        "sudograph_context",
        "sudograph_context_read",
        "sudograph_model_contract",
        "sudograph_model_apply",
        "review",
    }
)


class ModelAccess(Protocol):
    """Current full-model/action/projection grant, separate from resource access.

    Trusted composition closes over the same sealed actor as the host. actor_id
    is its original member, not the human account, selected target or UI integer.
    Human review requires its independent full-model read permission.
    """

    async def __call__(
        self,
        host: HostBinding,
        actor_id: str,
        registered: RetainedOntologyBinding,
        model: ModelBinding,
        operation: Operation,
    ) -> None: ...


class RegisteredModelTransport(Protocol):
    """Existing reviewed worker service, bound by its original registration.

    It resolves the actual private connector/credentials and enforces endpoint
    restrictions and grant/effect fences. References here are not permissions.
    There is no URL, token, connector lookup or transport fallback in these ports.
    """

    async def call_tool(
        self, registered: RetainedOntologyBinding, name: Operation, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def read_review(
        self, registered: RetainedOntologyBinding, request: dict[str, Any]
    ) -> dict[str, Any]: ...


class ContextRecorder(Protocol):
    """Record the exact pointer against this original input's existing lifecycle.

    The selected factory supplies the current input/run custody in its closure.
    This must complete before text is returned. It is not a comment ACK, native
    delivery acknowledgment or authority inferred from a digest.
    """

    async def __call__(
        self, host: HostBinding, registered: RetainedOntologyBinding, pointer: ModelPointer
    ) -> None: ...


class RegisteredSudographPorts:
    """Actual HostPorts consumer for one original session and registered model.

    Construction grants nothing. The registration is re-read before and after
    every grant/transport wait. Grant and reviewed transport implementations are
    mandatory production dependencies; no allow-all or local worker is supplied.
    """

    def __init__(
        self,
        host: TrustedHostContext,
        original: RetainedCohostBinding,
        registered: RetainedOntologyBinding,
        *,
        bindings: RetainedCohostBindingRepo,
        registry: RetainedOntologyBindingRepo,
        audit: RetainedModelAuditRepo,
        actor_id: str,
        require_model_access: ModelAccess,
        transport: RegisteredModelTransport,
    ) -> None:
        from octop.infra.db.repos.retained_cohost import RetainedCohostBinding
        from octop.infra.db.repos.retained_ontology import RetainedOntologyBinding

        _required_strings(actor_id)
        if (
            type(original) is not RetainedCohostBinding
            or type(registered) is not RetainedOntologyBinding
            or original.organization_id != host.binding.organization_id
            or registered.application_session_id != original.application_session_id
            or registered.organization_id != original.organization_id
            or registered.owner_id != original.owner_id
            or host.binding.native
            != CohostSessionBinding(
                original.owner_id,
                original.native_agent_id,
                original.durable_session_id,
                original.repository_path,
            )
        ):
            raise PermissionError("Model does not belong to this original host session")
        # Preserve the domain's exact reference validation. Never normalize an
        # incompatible original reference into a different model identity.
        self._binding = ModelBinding(
            registered.authority_ref,
            registered.model_ref,
            registered.resource_ref,
            registered.source_refs,
        )
        self._host, self._original, self._registered = host, original, registered
        self._bindings, self._registry, self._audit = bindings, registry, audit
        self._actor_id, self._require_model_access = actor_id, require_model_access
        self._transport = transport

    @property
    def binding(self) -> ModelBinding:
        return self._binding

    def _match(self, binding: ModelBinding, operation: Operation) -> None:
        if type(binding) is not ModelBinding or binding != self.binding:
            raise PermissionError("Foreign registered model binding")
        if operation not in _OPERATIONS:
            raise ValueError("Unknown registered model operation")

    async def _current(self) -> None:
        await self._host.check_current()
        await asyncio.to_thread(self._bindings.assert_current, self._original)
        await asyncio.to_thread(self._registry.assert_current, self._registered)
        await self._host.check_current()

    async def authorize(self, binding: ModelBinding, operation: Operation) -> None:
        self._match(binding, operation)
        await self._current()
        await self._host.authorize(operation, binding.resource_ref)
        await _require_completion(
            self._require_model_access(
                self._host.binding, self._actor_id, self._registered, binding, operation
            )
        )
        await self._current()
        await self._host.authorize(operation, binding.resource_ref)

    async def _before_io(self, binding: ModelBinding, operation: Operation) -> None:
        await self.authorize(binding, operation)
        await self._host.check_ready(operation, binding.resource_ref)
        await self.authorize(binding, operation)

    async def call_tool(
        self, binding: ModelBinding, name: Operation, arguments: dict[str, Any]
    ) -> ToolResult:
        self._match(binding, name)
        # Validate and snapshot before the first await, preserving operation_id.
        request = tool_request(name, arguments)
        await self._before_io(binding, name)
        response = await self._transport.call_tool(self._registered, name, request)
        if type(response) is not ToolResult or type(response.is_error) is not bool:
            raise HostUnavailable("Invalid registered worker result")
        result = copy.deepcopy(response)
        await self.authorize(binding, name)
        return result

    async def read_review(self, binding: ModelBinding, request: dict[str, Any]) -> dict[str, Any]:
        self._match(binding, "review")
        if not isinstance(request, dict) or request.get("resource") != binding.resource_ref:
            raise PermissionError("Review does not target the registered resource")
        arguments = review_request(
            {key: value for key, value in request.items() if key != "resource"}
        )
        await self._before_io(binding, "review")
        response = await self._transport.read_review(
            self._registered, {**arguments, "resource": binding.resource_ref}
        )
        result = copy.deepcopy(envelope(response, binding, review=True))
        await self.authorize(binding, "review")
        return result

    async def audit(
        self, binding: ModelBinding, operation: Operation, status: Literal["success", "error"]
    ) -> None:
        self._match(binding, operation)
        # Denied/stale attempts must remain auditable without another grant.
        # The canonical writer role is installed separately by deployment.
        await asyncio.to_thread(
            self._audit.record_access,
            self._registered,
            actor_id=self._actor_id,
            operation=operation,
            status=status,
        )

    async def capture_review(
        self, endpoint: str, store: RetainedReviewDocumentRepo
    ) -> ReviewDocument:
        """Capture one compiler document, commit it, then admit frame serving.

        The endpoint comes from the existing trusted page grant. This method
        accepts no browser-supplied HTML/pointer. PC3 serves the returned HTML
        only after this wait completes; a cancelled/failed wait cannot publish
        a frame or acknowledge a comment, even if the SQL commit later finishes.
        """
        document = await SudographClient(self.binding, self).review_document(endpoint)
        await self._before_io(self.binding, "review")
        await _require_completion(
            asyncio.to_thread(
                store.save,
                self._original,
                self._registered,
                endpoint,
                record=document.record(),
                html=document.html,
                account_id=self._host.binding.account_id,
                actor_id=self._actor_id,
            )
        )
        await self.authorize(self.binding, "review")
        return document

    async def restore_review(
        self, endpoint: str, store: RetainedReviewDocumentRepo
    ) -> ReviewDocument:
        """Recover the original frame under fresh grants, without reading head.

        A checksum authenticates no caller. The registered original session is
        revalidated around the actual canonical read and domain restoration.
        A legacy missing digest or altered binding/HTML refuses recovery.
        """
        await self._before_io(self.binding, "review")
        stored = await asyncio.to_thread(store.load, self._original, self._registered, endpoint)
        document = ReviewDocument.restore(self.binding, stored.record, stored.html)
        await self.audit(self.binding, "review", "success")
        await self.authorize(self.binding, "review")
        return document

    def host_context_resolver(self, record_context: ContextRecorder) -> BackendHostContextResolver:
        """Return text from one validated context, retaining its semantic pointer.

        No last-context cache or digest computed from rendered text is used.
        The existing host wrapper checks original logical/native IDs and current
        authority around this reader; PC3 retains ACP input-epoch enforcement.
        """
        client = SudographClient(self.binding, self)

        async def read_text() -> str:
            result = await client.read_context()
            pointer = ModelPointer(result["revision"], result["context_digest"])
            text: str = result["text"]
            await self.authorize(self.binding, "sudograph_context")
            await _require_completion(record_context(self._host.binding, self._registered, pointer))
            await self.authorize(self.binding, "sudograph_context")
            return text

        return self._host.host_context_resolver(self.binding.resource_ref, read_text)
