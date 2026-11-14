"""Product CurrentSelection producer using original published cohost facts.

This supplies Owner6's existing (actor, HostSelection)->RetainedCohostBinding
port. It selects no credentials, entitlement kind, runtime, grant or engine.
"""

from __future__ import annotations

import asyncio

from octop.infra.db.repos.retained_cohost import RetainedCohostBinding, RetainedCohostBindingRepo
from octop.infra.db.repos.retained_session_projection import (
    RetainedSessionProjection,
    RetainedSessionProjectionRepo,
    SessionProjectionUnavailable,
)
from octop.infra.identity.canonical import CanonicalIdentitySnapshot
from octop.infra.native_host.context import HostSelection


class ProductCohostSelections:
    def __init__(
        self, products: RetainedSessionProjectionRepo, originals: RetainedCohostBindingRepo
    ) -> None:
        self._products, self._originals = products, originals

    async def record_existing(
        self,
        selection: HostSelection,
        *,
        authority_id: str,
        original_app_session_id: str,
        organization_id: str,
        owner_id: str,
    ) -> RetainedSessionProjection:
        """Reference one verified original session from EXISTING product rows.

        Trusted migration/composition supplies the original IDs; this is not
        exposed to caller metadata or a tool. No Agent/thread/account/native
        session is allocated. A mid-I/O source change refuses; committed product
        references remain immutable and cannot silently retarget old history.
        """
        if type(selection) is not HostSelection:
            raise ValueError("Require an explicit product selection")
        original = await asyncio.to_thread(
            self._originals.get_for_resume,
            original_app_session_id,
            organization_id=organization_id,
            owner_id=owner_id,
        )
        projection = RetainedSessionProjection(
            authority_id, selection.agent_id, selection.thread_id, original
        )
        await asyncio.to_thread(self._products.record_existing, projection)
        await asyncio.to_thread(self._originals.assert_current, original)
        if (
            await asyncio.to_thread(
                self._products.get, authority_id, selection.agent_id, selection.thread_id
            )
            != projection
        ):
            raise SessionProjectionUnavailable("Original product projection changed")
        return projection

    async def current(
        self, actor: CanonicalIdentitySnapshot, selection: HostSelection
    ) -> RetainedCohostBinding:
        """Read exact product/original identities; current grants stay separate."""
        if type(actor) is not CanonicalIdentitySnapshot or type(selection) is not HostSelection:
            raise PermissionError("Require current canonical actor and product selection")
        before = await asyncio.to_thread(
            self._products.get, actor.authority_id, selection.agent_id, selection.thread_id
        )
        if (
            actor.organization_id != before.original.organization_id
            or actor.member.organization_id != actor.organization_id
            or actor.member.status != "active"
        ):
            raise PermissionError("Original selection belongs to another current organization")
        original = await asyncio.to_thread(
            self._originals.get_for_resume,
            before.original.application_session_id,
            organization_id=before.original.organization_id,
            owner_id=before.original.owner_id,
        )
        if original != before.original:
            raise SessionProjectionUnavailable("Original durable session mapping changed")
        after = await asyncio.to_thread(
            self._products.get, actor.authority_id, selection.agent_id, selection.thread_id
        )
        if after != before:
            raise SessionProjectionUnavailable("Original product selection changed")
        await asyncio.to_thread(self._originals.assert_current, original)
        return original
