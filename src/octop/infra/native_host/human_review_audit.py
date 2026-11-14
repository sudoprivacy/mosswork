"""Append an independent human review audit under the held canonical writer.

This producer grants no read or native operation. Trusted composition supplies
the protected original repository and actual writer admission; constructing it
does not establish writer exclusion or reauthenticate a withdrawn human.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from octop.infra.db.repos.retained_model_audit import RetainedModelAuditRepo
from octop.infra.db.repos.retained_ontology_project import RetainedOntologyProject
from octop.infra.identity.request_principal import CanonicalAuthenticatedRequest
from octop.infra.identity.write_authority import CanonicalWriterAdmission, assert_canonical_writer
from octop.infra.sudograph.protocol import ModelBinding


class CanonicalHumanReviewAudit:
    """Hold one original sealed actor/project, including for a denied read.

    Each call still requires the same principal, project and full model tuple.
    Error auditing deliberately uses original attribution after human/grant
    withdrawal; it never authorizes another read, effect or session allocation.
    """

    def __init__(
        self,
        *,
        principal: CanonicalAuthenticatedRequest,
        project: RetainedOntologyProject,
        audits: RetainedModelAuditRepo,
        writer: CanonicalWriterAdmission,
    ) -> None:
        if (
            type(principal) is not CanonicalAuthenticatedRequest
            or type(project) is not RetainedOntologyProject
            or type(audits) is not RetainedModelAuditRepo
            or not callable(getattr(writer, "assert_current_writer", None))
        ):
            raise ValueError("Require the original human review audit services")
        identity = principal.identity  # Validate the real private factory seal.
        if identity.snapshot.organization_id != project.organization_id:
            raise PermissionError("Human review audit organization mismatch")
        self._principal, self._project = principal, project
        self._model = ModelBinding(
            project.authority_ref, project.model_ref, project.project_id, project.source_refs
        )
        self._actor = identity.snapshot.member.owner_id
        self._audits, self._writer = audits, writer

    async def __call__(
        self,
        principal: CanonicalAuthenticatedRequest,
        project: RetainedOntologyProject,
        model: ModelBinding,
        status: Literal["success", "error"],
    ) -> None:
        if (
            principal is not self._principal
            or type(project) is not RetainedOntologyProject
            or project != self._project
            or type(model) is not ModelBinding
            or model != self._model
            or type(status) is not str
            or status not in ("success", "error")
        ):
            raise PermissionError("Human review audit scope mismatch")
        await assert_canonical_writer(self._writer)
        await asyncio.to_thread(
            self._audits.record_project_access,
            self._project,
            actor_id=self._actor,
            operation="review",
            status=status,
        )
        # SQL may already have committed. Lost writer completion or cancellation
        # releases no success; do not retry, claim rollback, or synthesize ACK.
        await assert_canonical_writer(self._writer)
