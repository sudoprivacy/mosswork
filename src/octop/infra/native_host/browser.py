"""Bind existing browser transport to the original canonical cohost caller.

This is the execution-bound consumer. Human-only profile viewing needs its own
current personal-profile grant and must not create a native session to use it.
No browser service, native custody journal or credential issuer is created here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from octop.infra.browser.worker import BrowserWorkerClient, WorkerBinding
from octop.infra.identity.canonical import CanonicalIdentitySnapshot

from .context import (
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

CurrentBrowserActor = Callable[[], Awaitable[CanonicalIdentitySnapshot]]
WorkerResolver = Callable[[WorkerBinding, str], Awaitable[BrowserWorkerClient]]
AllocationCheck = Callable[[WorkerBinding, str], Awaitable[None]]


class BrowserActionAccess(Protocol):
    """Exact action/resource grant from the same current actor and native input.

    The provider must consume actual canonical/native/run authority. For take,
    return and lifecycle operations it also enforces the corresponding original
    Stop/drain or fresh normal-turn admission; neither browser epochs nor an
    HTTP action can assert those facts. This assertion may run repeatedly. It
    must not send cancel/take/return, admit a new input or mutate native custody.
    Actual control dispatch remains with the original owner. Complete with None
    or raise.
    """

    async def __call__(
        self,
        host: HostBinding,
        actor: CanonicalIdentitySnapshot,
        binding: WorkerBinding,
        action: dict[str, Any],
    ) -> None: ...


def browser_controller(binding: WorkerBinding) -> str:
    """Preserve original Moss JSON.stringify([orgId,userId]) UTF-8 identity."""
    _required_strings(binding.org_id, binding.user_id)
    encoded = json.dumps(
        [binding.org_id, binding.user_id], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return "user-" + hashlib.sha256(encoded).hexdigest()


def _snapshot(action: dict[str, Any]) -> str:
    if (
        not isinstance(action, dict)
        or not isinstance(action.get("kind"), str)
        or not action["kind"]
        or "\x00" in action["kind"]
    ):
        raise ValueError("Require an explicit browser action")
    # Action grammar remains in the existing API/Worker. This boundary captures
    # exactly its trusted parsed action; it cannot turn fields into identities.
    return json.dumps(action, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedBrowserAction:
    """One fixed transport/action and a current assertion, never a cached grant.

    PC3 installs before_effect inside the actual Worker operation/lifecycle
    barrier, then rechecks its epochs. A check before HTTP does not close that
    remote wait. This object does not dispatch, retry or acknowledge cleanup.
    """

    client: BrowserWorkerClient
    binding: WorkerBinding
    controller: str
    _action_json: str = field(repr=False)
    before_effect: Callable[[], Awaitable[None]] = field(repr=False)

    @property
    def action(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self._action_json)
        return value


class CohostBrowserAdmission:
    """Current original personal browser policy with mandatory runtime providers.

    current_actor revalidates the original sealed canonical carrier. The worker
    resolver borrows only the registered existing connection for the supplied
    binding/controller; it must not provision, rebind or select a local fallback.
    assert_allocation checks actual original registered worker/placement state,
    including its existing binding, without using invoke(state) to claim one.
    """

    def __init__(
        self,
        host: TrustedHostContext,
        original: RetainedCohostBinding,
        *,
        bindings: RetainedCohostBindingRepo,
        current_actor: CurrentBrowserActor,
        require_action: BrowserActionAccess,
        assert_allocation: AllocationCheck,
        resolve_worker: WorkerResolver,
    ) -> None:
        from octop.infra.db.repos.retained_cohost import RetainedCohostBinding

        if (
            type(original) is not RetainedCohostBinding
            or host.binding.organization_id != original.organization_id
            or host.binding.native
            != CohostSessionBinding(
                original.owner_id,
                original.native_agent_id,
                original.durable_session_id,
                original.repository_path,
            )
        ):
            raise PermissionError("Browser is not bound to this original native execution")
        self._host, self._original, self._bindings = host, original, bindings
        self._actor, self._require_action = current_actor, require_action
        self._assert_allocation, self._resolve_worker = assert_allocation, resolve_worker
        # Preserve the original application identity. Neither product thread
        # nor native durable session can replace this migration affinity.
        self._binding = WorkerBinding(
            original.organization_id, original.owner_id, original.application_session_id
        )
        self._controller = browser_controller(self._binding)

    async def _current(self) -> CanonicalIdentitySnapshot:
        await self._host.check_current()
        actor = await self._actor()
        if (
            type(actor) is not CanonicalIdentitySnapshot
            or actor.account_id != self._host.binding.account_id
            or actor.organization_id != self._binding.org_id
            or actor.member.organization_id != self._binding.org_id
            or actor.member.owner_id != self._binding.user_id
            or actor.member.status != "active"
        ):
            # Preserve authorizeSessionBrowser: inspection/admin authority does
            # not expose another member's personal cookies or browser profile.
            raise PermissionError("Current personal browser access denied")
        await asyncio.to_thread(self._bindings.assert_current, self._original)
        await self._host.check_current()
        if await self._actor() != actor:
            raise PermissionError("Canonical browser actor changed during admission")
        return actor

    async def _check_action(self, action_json: str) -> None:
        actor = await self._current()
        action = json.loads(action_json)
        await _require_completion(
            self._require_action(self._host.binding, actor, self._binding, action)
        )
        # The callback gets its own copy. Its mutation cannot retarget dispatch
        # or the exact operation/resource check after it returns.
        kind = json.loads(action_json)["kind"]
        await self._host.check_ready("browser." + kind, self._original.application_session_id)
        await _require_completion(self._assert_allocation(self._binding, self._controller))
        if await self._current() != actor:
            raise PermissionError("Canonical browser actor changed during grant check")

    async def prepare(self, action: dict[str, Any]) -> ResolvedBrowserAction:
        """Authorize before resolving a private connection; retain a fresh guard.

        Preparation does not submit a browser effect. Missing policy/allocation
        refuses before credential resolution. Native cancellation/Unknown and
        joined cleanup remain with the original execution owner, not this class.
        """
        action_json = _snapshot(action)
        await self._check_action(action_json)
        client = await self._resolve_worker(self._binding, self._controller)
        if type(client) is not BrowserWorkerClient:
            raise HostUnavailable("No registered original browser connection")
        await self._check_action(action_json)

        async def before_effect() -> None:
            await self._check_action(action_json)

        return ResolvedBrowserAction(
            client, self._binding, self._controller, action_json, before_effect
        )
