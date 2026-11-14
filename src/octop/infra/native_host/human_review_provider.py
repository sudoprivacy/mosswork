"""Construct the existing human reader for one sealed caller and document.

The supplied grant, authorized secret capability and audit remain mandatory
trusted services. This resolver creates no authority, session or native runtime.
"""

from __future__ import annotations

import asyncio
import copy
import re
from collections.abc import Awaitable
from typing import Any, Literal

from octop.infra.connectors.registered_model import RegisteredModelTransport
from octop.infra.db.repos.retained_mcp import RetainedReviewedMcpRepo
from octop.infra.db.repos.retained_model_audit import RetainedModelAuditRepo
from octop.infra.db.repos.retained_ontology_project import (
    RetainedOntologyProject,
    RetainedOntologyProjectRepo,
)
from octop.infra.db.repos.retained_review import RetainedHumanReviewDocumentRepo
from octop.infra.errors import MossworkError
from octop.infra.identity.request_principal import CanonicalAuthenticatedRequest
from octop.infra.identity.write_authority import CanonicalWriterAdmission
from octop.infra.sudograph.protocol import (
    ModelBinding,
    ReviewDocument,
    SudographError,
    review_request,
)
from octop.infra.sudograph.review_read import review_page_request

from .human_review import (
    HumanReviewAccess,
    HumanReviewAudit,
    HumanReviewReader,
    HumanReviewResolvers,
)
from .human_review_audit import CanonicalHumanReviewAudit
from .reviewed_mcp import SecretList, _complete, _deny


class _ReviewAccessDenied(PermissionError):
    def __init__(self, denial: PermissionError) -> None:
        super().__init__(*denial.args)
        self.denial = denial


async def _review_access(pending: Awaitable[None]) -> None:
    """Keep an actual grant refusal distinct from an invalid provider result."""
    try:
        return await pending
    except PermissionError as denial:
        raise _ReviewAccessDenied(denial) from None


class CanonicalHumanReviewReaders:
    """The existing HTTP resolver seam, with no cached caller or latest project.

    Trusted composition supplies the same protected canonical store and actual
    current providers. URI equality prevents a mistaken cross-store assembly;
    deployment must independently preserve writer/filesystem exclusion. The
    returned reader remains subject to its grant and credential checks at I/O.
    """

    def __init__(
        self,
        *,
        projects: RetainedOntologyProjectRepo,
        reviews: RetainedReviewedMcpRepo,
        require_review_access: HumanReviewAccess,
        audit_review: HumanReviewAudit,
        list_secrets: SecretList | None = None,
        allowed_addresses: tuple[str, ...] | None = None,
    ) -> None:
        if (
            type(projects) is not RetainedOntologyProjectRepo
            or type(reviews) is not RetainedReviewedMcpRepo
            or projects._uri != reviews._uri
            or not callable(require_review_access)
            or not callable(audit_review)
            or list_secrets is not None
            and not callable(list_secrets)
        ):
            raise ValueError("Require the same canonical store and actual human review providers")
        self._projects, self._reviews = projects, reviews
        self._access, self._audit = require_review_access, audit_review
        self._secrets, self._addresses = list_secrets, allowed_addresses

    @classmethod
    def with_canonical_audit(
        cls,
        *,
        projects: RetainedOntologyProjectRepo,
        reviews: RetainedReviewedMcpRepo,
        audits: RetainedModelAuditRepo,
        writer: CanonicalWriterAdmission,
        require_review_access: HumanReviewAccess,
        list_secrets: SecretList | None = None,
        allowed_addresses: tuple[str, ...] | None = None,
    ) -> CanonicalHumanReviewReaders:
        """Compose the existing reader with the original audit repo and writer.

        Pass the same writer to the existing HTTP document mount. This checks
        store assembly only; trusted deployment must establish exclusivity.
        Each audit binds the reader's held actor/project without reauthorizing
        a denied human read or constructing another business/runtime service.
        """
        if (
            type(projects) is not RetainedOntologyProjectRepo
            or type(audits) is not RetainedModelAuditRepo
            or audits._uri.removesuffix("?mode=rw") + "?mode=ro" != projects._uri
            or not callable(getattr(writer, "assert_current_writer", None))
        ):
            raise ValueError("Require the same canonical audit store and actual writer")

        async def audit_review(
            principal: CanonicalAuthenticatedRequest,
            project: RetainedOntologyProject,
            model: ModelBinding,
            status: Literal["success", "error"],
        ) -> None:
            await CanonicalHumanReviewAudit(
                principal=principal, project=project, audits=audits, writer=writer
            )(principal, project, model, status)

        return cls(
            projects=projects,
            reviews=reviews,
            require_review_access=require_review_access,
            audit_review=audit_review,
            list_secrets=list_secrets,
            allowed_addresses=allowed_addresses,
        )

    async def __call__(
        self,
        principal: CanonicalAuthenticatedRequest,
        *,
        project_id: str,
        endpoint: str,
    ) -> HumanReviewReader:
        """Bind the exact original document without reading its worker or secrets.

        This does not acquire a grant or writer role. Missing/changed evidence
        refuses before a reader escapes; later reader calls revalidate the same
        principal/project/document through the original reviewed transport.
        """
        if type(principal) is not CanonicalAuthenticatedRequest:
            raise _deny()
        original = principal.identity
        selected = review_request({"operation": "document", "endpoint": endpoint})
        if await principal.revalidate() != original:
            raise _deny()
        project = await asyncio.to_thread(
            self._projects.get,
            organization_id=original.snapshot.organization_id,
            project_id=project_id,
        )
        if await principal.revalidate() != original:
            raise _deny()
        model = ModelBinding(
            project.authority_ref, project.model_ref, project.project_id, project.source_refs
        )
        request = {**selected, "resource": project.project_id}
        original_principal, original_project, original_model = principal, project, model
        require_access, audit_review = self._access, self._audit

        async def document_access(
            principal: CanonicalAuthenticatedRequest,
            project: RetainedOntologyProject,
            model: ModelBinding,
            *,
            operation: Literal["review"],
            request: dict[str, Any],
        ) -> None:
            if (
                principal is not original_principal
                or project != original_project
                or model != original_model
                or operation != "review"
                or request != {**selected, "resource": original_project.project_id}
            ):
                raise _deny()
            await _complete(
                _review_access(
                    require_access(
                        principal,
                        project,
                        model,
                        operation="review",
                        request=copy.deepcopy(request),
                    )
                )
            )

        resolvers = HumanReviewResolvers(
            principal=principal,
            projects=self._projects,
            reviews=self._reviews,
            require_review_access=document_access,
            list_secrets=self._secrets,
            allowed_addresses=self._addresses,
        )
        transport: RegisteredModelTransport[RetainedOntologyProject] = RegisteredModelTransport(
            resolve_tool=resolvers.resolve_tool, resolve_review=resolvers.resolve_review
        )
        reader = HumanReviewReader(
            principal=principal,
            project=project,
            projects=self._projects,
            require_review_access=document_access,
            transport=transport,
            audit_review=audit_review,
        )
        try:
            await document_access(principal, project, model, operation="review", request=request)
            await asyncio.to_thread(self._projects.assert_current, project)
            if await principal.revalidate() != original:
                raise _deny()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Attribute denial to the held original actor even after human
            # access withdrawal. The independent audit provider owns writer
            # admission; this callback grants no new read or native effect.
            await _complete(audit_review(principal, project, model, "error"))
            raise
        return reader

    async def read_page(
        self,
        principal: CanonicalAuthenticatedRequest,
        *,
        project_id: str,
        endpoint: str,
        record_sha256: str,
        request: object,
        documents: RetainedHumanReviewDocumentRepo,
    ) -> dict[str, Any]:
        """Read one page bound to the same actor's committed original document.

        The authenticated parent supplies the original renderer request after
        checking its frame. The stored document selects the model and pointer;
        the same mandatory access provider must separately authorize the page,
        including its source/projection/DLP scope. Document access alone never
        admits page I/O. The required parent record digest must match these
        stored bytes; it grants no access. No HTML is fetched or rewritten.
        """
        if type(record_sha256) is not str or re.fullmatch(r"[a-f0-9]{64}", record_sha256) is None:
            raise ValueError("Require the original captured document digest")
        original_request = copy.deepcopy(request)
        reader = await self(principal, project_id=project_id, endpoint=endpoint)
        document = await reader.restore_review(endpoint, documents)
        if document.record()["record_sha256"] != record_sha256:
            raise SudographError(
                "CONTEXT_STALE",
                "The original captured review document does not match",
                "Restore the original document under current authorization before reading records.",
            )
        selected = review_page_request(document, original_request)
        project, model = reader._project, reader.binding
        original_principal, original_project, original_model = principal, project, model
        snapshot = reader._human.original.snapshot
        expected = {**selected, "resource": project.project_id}
        document_request = {
            "operation": "document",
            "endpoint": endpoint,
            "resource": project.project_id,
        }

        async def original_document() -> None:
            await reader._human.check(project, document_request)
            stored = await asyncio.to_thread(
                documents.load,
                project,
                endpoint,
                account_id=snapshot.account_id,
                actor_id=snapshot.member.owner_id,
            )
            if ReviewDocument.restore(model, stored.record, stored.html) != document:
                raise _deny()
            await reader._human.check(project, document_request)

        async def page_access(
            principal: CanonicalAuthenticatedRequest,
            project: RetainedOntologyProject,
            model: ModelBinding,
            *,
            operation: Literal["review"],
            request: dict[str, Any],
        ) -> None:
            if (
                principal is not original_principal
                or project != original_project
                or model != original_model
                or operation != "review"
                or request != expected
            ):
                raise _deny()
            await original_document()
            await _complete(
                _review_access(
                    self._access(
                        principal,
                        project,
                        model,
                        operation="review",
                        request=copy.deepcopy(request),
                    )
                )
            )
            await original_document()

        resolvers = HumanReviewResolvers(
            principal=principal,
            projects=self._projects,
            reviews=self._reviews,
            require_review_access=page_access,
            list_secrets=self._secrets,
            allowed_addresses=self._addresses,
        )
        transport: RegisteredModelTransport[RetainedOntologyProject] = RegisteredModelTransport(
            resolve_tool=resolvers.resolve_tool, resolve_review=resolvers.resolve_review
        )
        page_reader = HumanReviewReader(
            principal=principal,
            project=project,
            projects=self._projects,
            require_review_access=page_access,
            transport=transport,
            audit_review=self._audit,
        )
        try:
            result = await page_reader.review(selected)
        except asyncio.CancelledError:
            raise
        except Exception as failure:
            # Preserve current canonical and actual resource 401/403 outside
            # the sanitizer. Invalid provider returns and other internal
            # failures retain the original transport/audit/domain outcome.
            try:
                await page_reader._human.check(project, expected)
            except _ReviewAccessDenied as current_denial:
                raise current_denial.denial from None
            except MossworkError as current:
                if current.status in {401, 403}:
                    raise
                raise failure from None
            except Exception:
                raise failure from None
            raise
        try:
            await page_reader._human.check(project, expected)
        except _ReviewAccessDenied as current_denial:
            raise current_denial.denial from None
        return result
