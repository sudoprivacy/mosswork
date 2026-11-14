"""Keep current canonical human admission outside the durable request payload.

Persisted actor IDs are attribution only. After process restart, queued work is
ineligible until a real human reauthenticates the same original unsent request.
Claimed/Unknown work never acquires a fresh attempt through this registry.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from octop.infra.db.repos.execution import ExecutionConflict, ExecutionRepo, wire
from octop.infra.errors import MossworkError
from octop.infra.identity.projection import (
    CanonicalIdentityKey,
    CanonicalLocalUserProjection,
    CanonicalRequestIdentityResolver,
)
from octop.infra.identity.request_principal import (
    CanonicalAuthenticatedRequest,
)
from octop.infra.identity.request_principal import (
    reauthenticate as reauthenticate_actor,
)


class CurrentAdmissionRequired(ExecutionConflict):
    """No current authenticated human is bound to this original queued request."""


def _principal(value: object) -> CanonicalAuthenticatedRequest:
    if type(value) is not CanonicalAuthenticatedRequest:
        raise CurrentAdmissionRequired("current_human_admission_required")
    # The producer checks its private factory seal here; a decoded DTO cannot
    # construct a request principal or substitute an integer product User.
    _ = value.identity
    return value


def _actor(principal: CanonicalAuthenticatedRequest) -> dict[str, str | int]:
    key = principal.identity.projection.identity
    return {
        "authority_id": key.authority_id,
        "account_id": key.account_id,
        "organization_id": key.organization_id,
        "member_id": key.member_id,
        "local_user_id": principal.identity.projection.local_user_id,
    }


@dataclass(frozen=True, slots=True, repr=False)
class _HeldAdmission:
    principal: CanonicalAuthenticatedRequest
    identity: tuple[object, ...]


def _run_identity(run: dict[str, Any]) -> tuple[object, ...]:
    return tuple(
        run[key]
        for key in (
            "run_id",
            "user_id",
            "agent_id",
            "thread_id",
            "operation",
            "request_key",
            "request_json",
        )
    )


def _unsent(run: dict[str, Any]) -> bool:
    return (
        run["state"] == "queued"
        and run["node_id"] is None
        and run["incarnation"] is None
        and run["token"] is None
    )


def _original_actor(run: dict[str, Any]) -> CanonicalLocalUserProjection:
    """Read only the original server-created attribution, never target user_id."""
    try:
        actor = json.loads(run["request_json"])["canonical_actor"]
        if type(actor) is not dict or set(actor) != {
            "authority_id",
            "account_id",
            "organization_id",
            "member_id",
            "local_user_id",
        }:
            raise ValueError("Invalid original actor")
        return CanonicalLocalUserProjection(
            CanonicalIdentityKey(
                actor["authority_id"],
                actor["account_id"],
                actor["organization_id"],
                actor["member_id"],
            ),
            actor["local_user_id"],
        )
    except (KeyError, TypeError, ValueError):
        raise CurrentAdmissionRequired("original_request_actor_unavailable") from None


class CanonicalRunAdmissions:
    """A process-local carrier, never an action/resource/native grant provider.

    Trusted composition attaches this gate to the SAME ExecutionRepo used by
    the worker. The provider must still check current original resource/model,
    target entitlement and native/DLP authority immediately before effects.
    Human credentials stay solely inside Owner5's sealed principal. IM and
    machine actors need their own verified carrier; this class cannot mint one.
    """

    def __init__(self, repo: ExecutionRepo) -> None:
        self.repo = repo
        self._held: dict[str, _HeldAdmission] = {}

    async def submit(
        self,
        principal: CanonicalAuthenticatedRequest,
        *,
        user: int,
        agent: str,
        thread: str,
        operation: str,
        key: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        principal = _principal(principal)
        if "canonical_actor" in request:
            raise CurrentAdmissionRequired("caller_cannot_supply_canonical_attribution")
        # Use the real queue's JSON validator/snapshot before the first await.
        # This deliberately has no default serializer for opaque principals.
        payload = json.loads(wire({**request, "canonical_actor": _actor(principal)}))
        await principal.revalidate()
        run = await asyncio.to_thread(
            self.repo.submit,
            # Resource owner and authenticated actor can differ. The selected
            # provider must authorize that exact target independently.
            user=user,
            agent=agent,
            thread=thread,
            operation=operation,
            key=key,
            request=payload,
        )
        await self.reauthenticate(run["run_id"], principal)
        return run

    async def reauthenticate(self, run_id: str, principal: CanonicalAuthenticatedRequest) -> None:
        """Bind fresh human auth to the SAME never-claimed queued request only."""
        await self._bind(run_id, principal)

    async def original_actor(
        self, run_id: str, principal: CanonicalAuthenticatedRequest
    ) -> dict[str, Any]:
        """Read one original run for its current actor without reopening it.

        This checks attribution only. The transport must separately require
        current resource/action authorization before returning events or
        requesting cancellation. No held admission or run state is changed.
        """
        principal = _principal(principal)
        await principal.revalidate()
        original = await asyncio.to_thread(self.repo.get, run_id)
        if original is None:
            raise CurrentAdmissionRequired("original_request_unavailable")
        self._check_actor(original, principal)
        await principal.revalidate()
        current = await asyncio.to_thread(self.repo.get, run_id)
        if current is None or _run_identity(current) != _run_identity(original):
            raise CurrentAdmissionRequired("original_queued_request_changed")
        self._check_actor(current, principal)
        return current

    async def reauthenticate_human(
        self,
        run_id: str,
        fresh_opaque_access_token: str,
        *,
        resolver: CanonicalRequestIdentityResolver,
    ) -> None:
        """Verify fresh login against the stored actor without allocating a user.

        Trusted ingress supplies the configured resolver and opaque credential;
        neither enters durable JSON. Read the original request before credential
        I/O and preserve its identity through binding. This private bridge adds
        no HTTP/WS credential channel, native attempt or action grant.
        """
        original = await asyncio.to_thread(self.repo.get, run_id)
        if original is None or not _unsent(original):
            raise CurrentAdmissionRequired("original_request_is_not_unsent")
        expected = _run_identity(original)
        principal = await reauthenticate_actor(
            resolver, fresh_opaque_access_token, _original_actor(original)
        )
        await self._bind(run_id, principal, expected=expected)

    async def _bind(
        self,
        run_id: str,
        principal: CanonicalAuthenticatedRequest,
        *,
        expected: tuple[object, ...] | None = None,
    ) -> None:
        principal = _principal(principal)
        await principal.revalidate()
        run = await asyncio.to_thread(self.repo.get, run_id)
        if run is None or not _unsent(run):
            raise CurrentAdmissionRequired("original_request_is_not_unsent")
        if expected is not None and _run_identity(run) != expected:
            raise CurrentAdmissionRequired("original_queued_request_changed")
        self._check_actor(run, principal)
        await principal.revalidate()
        current = await asyncio.to_thread(self.repo.get, run_id)
        if current is None or not _unsent(current) or _run_identity(current) != _run_identity(run):
            raise CurrentAdmissionRequired("original_queued_request_changed")
        self._held[run_id] = _HeldAdmission(principal, _run_identity(current))

    @staticmethod
    def _check_actor(run: dict[str, Any], principal: CanonicalAuthenticatedRequest) -> None:
        request = json.loads(run["request_json"])
        if request.get("canonical_actor") != _actor(principal):
            raise CurrentAdmissionRequired("original_request_actor_mismatch")

    async def current(self, run: dict[str, Any]) -> CanonicalAuthenticatedRequest:
        """Revalidate after queue/capacity waits without reconstructing a caller."""
        held = self._held.get(run["run_id"])
        if held is None or held.identity != _run_identity(run):
            raise CurrentAdmissionRequired("current_human_admission_required")
        self._check_actor(run, held.principal)
        await held.principal.revalidate()
        current = await asyncio.to_thread(self.repo.get, run["run_id"])
        if current is None or _run_identity(current) != held.identity:
            raise CurrentAdmissionRequired("original_queued_request_changed")
        if current["state"] not in {"queued", "running"}:
            raise CurrentAdmissionRequired("original_request_is_not_dispatchable")
        if run["state"] == "running":
            # A thread/credential wait must not borrow a newer product lease or
            # reopen Unknown. This is the existing PRODUCT fence, not native
            # incarnation, input admission or writer-completion evidence.
            await asyncio.to_thread(
                self.repo.check, run["run_id"], run["node_id"], run["incarnation"], run["token"]
            )
        # Reauthentication must not be replaced during the preceding awaits.
        if self._held.get(run["run_id"]) is not held:
            raise CurrentAdmissionRequired("current_human_admission_changed")
        return held.principal

    async def claim(self, node: str, incarnation: str) -> dict[str, Any] | None:
        """Never claim restored attribution-only work or replay a submitted run."""
        eligible = []
        for run_id in tuple(self._held):
            run = await asyncio.to_thread(self.repo.get, run_id)
            if run is None or not _unsent(run):
                continue
            try:
                await self.current(run)
            except (MossworkError, CurrentAdmissionRequired):
                self.forget(run_id)
                continue
            eligible.append(run_id)
        # Preserve the existing database fairness/capacity/fence selection;
        # admission only filters which original requests may be considered.
        return await asyncio.to_thread(
            self.repo.claim, node, incarnation, eligible_run_ids=tuple(eligible)
        )

    def forget(self, run_id: str) -> None:
        """Withdraw this process's credential only, never original native custody."""
        self._held.pop(run_id, None)

    def clear(self) -> None:
        self._held.clear()
