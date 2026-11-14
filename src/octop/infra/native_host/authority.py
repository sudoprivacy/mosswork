"""Compose canonical caller checks before resolving an existing native port.

The injected producer is Owner5's current canonical authority. Selection and
grant adapters remain trusted product services; this module reads the original
cohost binding and never creates an account, session, credential or native fence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from octop.infra.db.repos.retained_cohost import (
    RetainedCohostBinding,
    RetainedCohostBindingRepo,
)
from octop.infra.identity.canonical import CanonicalIdentitySnapshot

from .context import (
    CohostSessionBinding,
    HostBinding,
    HostSelection,
    HostUnavailable,
    NativeHostPort,
    ResolvedHost,
    _require_completion,
)

CurrentActor = Callable[[object], Awaitable[CanonicalIdentitySnapshot]]
CurrentSelection = Callable[
    [CanonicalIdentitySnapshot, HostSelection], Awaitable[RetainedCohostBinding]
]
OwnerAccess = Callable[[CanonicalIdentitySnapshot, RetainedCohostBinding], Awaitable[None]]
OperationAccess = Callable[
    [CanonicalIdentitySnapshot, RetainedCohostBinding, str, str], Awaitable[None]
]
RuntimeResolver = Callable[[HostBinding], Awaitable[NativeHostPort]]


@dataclass(frozen=True, slots=True, repr=False)
class _OriginalSelection:
    principal: object
    actor: CanonicalIdentitySnapshot
    selection: HostSelection
    original: RetainedCohostBinding


class RetainedCohostAuthority:
    """One concrete HostAuthority consumer, with no permissive default adapters.

    Use one instance per request/attachment, not a global principal cache.
    current_actor must accept only the actual authenticated server carrier and
    revalidate its original credential. current_selection reads trusted product
    projection; it cannot resolve credentials or create a replacement session.
    require_owner_access checks the target's current membership AND entitlement;
    require_operation separately checks the actual action/resource grant.

    resolve_runtime is reached only after those checks. It borrows an existing
    native port from its owning connection manager and must never start/replace
    a native execution. The manager owns credential/attachment cleanup; neither
    a failed product check nor this authority may kill a shared native writer.
    """

    def __init__(
        self,
        bindings: RetainedCohostBindingRepo,
        *,
        current_actor: CurrentActor,
        current_selection: CurrentSelection,
        require_owner_access: OwnerAccess,
        require_operation: OperationAccess,
        resolve_runtime: RuntimeResolver,
    ) -> None:
        self._bindings = bindings
        self._current_actor, self._current_selection = current_actor, current_selection
        self._require_owner_access, self._require_operation = (
            require_owner_access,
            require_operation,
        )
        self._resolve_runtime = resolve_runtime
        self._resolved: tuple[HostBinding, _OriginalSelection] | None = None
        self._resolve_started = False

    async def _actor(self, principal: object) -> CanonicalIdentitySnapshot:
        actor = await self._current_actor(principal)
        if (
            type(actor) is not CanonicalIdentitySnapshot
            or not actor.account_id
            or not actor.organization_id
            or actor.member.organization_id != actor.organization_id
            or not actor.member.owner_id
            or actor.member.status != "active"
        ):
            raise PermissionError("Current canonical actor is unavailable")
        return actor

    async def _current(self, saved: _OriginalSelection) -> CanonicalIdentitySnapshot:
        actor = await self._actor(saved.principal)
        if actor != saved.actor:
            raise PermissionError("Canonical actor changed during host selection")
        original = await self._current_selection(actor, saved.selection)
        if type(original) is not RetainedCohostBinding or original != saved.original:
            raise PermissionError("Original host selection changed")
        if original.organization_id != actor.organization_id:
            raise PermissionError("Host selection belongs to another organization")
        await asyncio.to_thread(self._bindings.assert_current, original)
        await _require_completion(self._require_owner_access(actor, original))
        # Target policy I/O can yield while a product projection or the native
        # durable binding is changed. Re-read both before returning it.
        if await self._current_selection(actor, saved.selection) != original:
            raise PermissionError("Original host selection changed")
        await asyncio.to_thread(self._bindings.assert_current, original)
        # Neither a SQL result nor a target entitlement may pin the caller's
        # membership while the next callback awaits a different authority.
        if await self._actor(saved.principal) != saved.actor:
            raise PermissionError("Canonical actor changed during host selection")
        return actor

    async def _operation(
        self, saved: _OriginalSelection, operation: str, resource_ref: str
    ) -> None:
        if any(
            not isinstance(value, str) or not value or "\x00" in value
            for value in (operation, resource_ref)
        ):
            raise ValueError("Require an exact host operation and resource")
        actor = await self._current(saved)
        await _require_completion(
            self._require_operation(actor, saved.original, operation, resource_ref)
        )
        await self._current(saved)

    async def resolve(self, principal: object, selection: HostSelection) -> ResolvedHost:
        """Resolve once, authorizing before runtime credential/connection access.

        session_connect is this product adapter's operation label, not a native
        permission or scope. Its injected grant adapter must map the exact
        original app session to the existing current authorization service.
        """
        if self._resolve_started:
            raise HostUnavailable("Host authority already used for an attachment")
        self._resolve_started = True
        if type(selection) is not HostSelection:
            raise ValueError("Require a trusted product selection")
        actor = await self._actor(principal)
        original = await self._current_selection(actor, selection)
        if type(original) is not RetainedCohostBinding:
            raise HostUnavailable("No original native binding was selected")
        saved = _OriginalSelection(principal, actor, selection, original)
        await self._operation(saved, "session_connect", original.application_session_id)
        binding = HostBinding(
            account_id=actor.account_id,
            organization_id=actor.organization_id,
            selection=selection,
            native=CohostSessionBinding(
                owner_id=original.owner_id,
                agent_id=original.native_agent_id,
                durable_session_id=original.durable_session_id,
                repository_path=original.repository_path,
            ),
        )
        runtime = await self._resolve_runtime(binding)
        # Connection lookup/mint can yield. Refuse a result after revocation;
        # the owning manager retains its attachment/Unknown cleanup contract.
        await self._operation(saved, "session_connect", original.application_session_id)
        self._resolved = binding, saved
        return ResolvedHost(binding, runtime)

    def _original(self, principal: object, binding: HostBinding) -> _OriginalSelection:
        if (
            self._resolved is None
            or binding != self._resolved[0]
            or principal is not self._resolved[1].principal
        ):
            raise PermissionError("Host authority is not attached to this caller and binding")
        return self._resolved[1]

    async def check_current(self, principal: object, binding: HostBinding) -> None:
        await self._current(self._original(principal, binding))

    async def authorize(
        self, principal: object, binding: HostBinding, operation: str, resource_ref: str
    ) -> None:
        await self._operation(self._original(principal, binding), operation, resource_ref)
