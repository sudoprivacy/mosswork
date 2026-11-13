"""Bind application authorization to one existing native execution.

The injected authorities own identity, credentials, grants and native fences.
These projections carry their original values; they create no new authority.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, TypedDict


class HostUnavailable(RuntimeError):
    """Current host evidence is unavailable; this says nothing about past effects."""


async def _require_completion(operation: Awaitable[object]) -> None:
    # Adapter assertions complete with None or raise. Silently discarding a
    # boolean/JSON verdict could turn a denied or malformed result into Ready.
    if await operation is not None:
        raise HostUnavailable("Invalid host assertion completion")


def _required_strings(*values: str) -> None:
    if any(not isinstance(value, str) or not value or "\x00" in value for value in values):
        raise ValueError("Require explicit canonical binding values")


@dataclass(frozen=True, slots=True)
class HostSelection:
    """Product resource selection, never a caller-supplied owner or native ID."""

    agent_id: str
    thread_id: str

    def __post_init__(self) -> None:
        _required_strings(self.agent_id, self.thread_id)


@dataclass(frozen=True, slots=True, repr=False)
class CohostSessionBinding:
    """Original CohostSessionBinding values, not a new native wire schema."""

    owner_id: str
    agent_id: str
    durable_session_id: str
    repository_path: str

    def __post_init__(self) -> None:
        _required_strings(
            self.owner_id, self.agent_id, self.durable_session_id, self.repository_path
        )


@dataclass(frozen=True, slots=True, repr=False)
class HostBinding:
    """Trusted canonical identity plus an exact product-to-native projection."""

    account_id: str
    organization_id: str
    selection: HostSelection
    native: CohostSessionBinding

    def __post_init__(self) -> None:
        _required_strings(self.account_id, self.organization_id)
        if not isinstance(self.selection, HostSelection) or not isinstance(
            self.native, CohostSessionBinding
        ):
            raise ValueError("Require an explicit product and native session binding")


class NativeHostPort(Protocol):
    """One injected owner-authenticated connection, supplied by trusted composition.

    check_current must check its original owner, host incarnation, live execution,
    authenticated endpoint and exact durable binding. A controller-input epoch or
    a matching durable ID alone is insufficient. Missing evidence must refuse.
    """

    async def check_current(self, binding: HostBinding) -> None: ...

    async def check_admission(self, binding: HostBinding) -> None:
        """Consume shared native/DLP admission for this exact allocation.

        Reuse @sudo/egress-policy; do not reimplement inheritance here. The
        operation must not launch a model or fall back to another engine.
        """
        ...


@dataclass(frozen=True, slots=True)
class ResolvedHost:
    binding: HostBinding = field(repr=False)
    runtime: NativeHostPort = field(repr=False)


class HostAuthority(Protocol):
    """Application adapter to current identity/membership and resource grants.

    principal is supplied by authenticated server composition, not deserialized
    tool metadata. resolve reads a published binding and must not initialize a
    replacement session. Fresh-session startup authenticates the owner first,
    then publishes the original new/load result before using this consumer.
    Codecs and numeric view IDs cannot grant membership.
    """

    async def resolve(self, principal: object, selection: HostSelection) -> ResolvedHost: ...

    async def check_current(self, principal: object, binding: HostBinding) -> None:
        """Recheck the current mapping/membership, explicit revocation and binding."""
        ...

    async def authorize(
        self, principal: object, binding: HostBinding, operation: str, resource_ref: str
    ) -> None:
        """Check the exact current action/resource grant; PermissionError denies."""
        ...


class BackendHostContextRequest(TypedDict):
    """The existing BackendHostContextResolver request (hostContext v1)."""

    sessionId: str
    nativeSessionId: str


BackendHostContextResolver = Callable[[BackendHostContextRequest], Awaitable[str]]


class TrustedHostContext:
    """Request-scoped binding; successful checks are never cached as a grant.

    Runtime dispatch must still consume the original native fence atomically.
    This consumer does not issue credentials, create sessions or own cleanup.
    """

    def __init__(self, principal: object, authority: HostAuthority, resolved: ResolvedHost) -> None:
        self._principal, self._authority = principal, authority
        self._binding, self._runtime = resolved.binding, resolved.runtime
        self._is_closed = False

    @property
    def binding(self) -> HostBinding:
        return self._binding

    def close(self) -> None:
        """Withdraw this context only; this does not prove a native writer stopped."""
        self._is_closed = True

    def _check_open(self) -> None:
        if self._is_closed:
            raise HostUnavailable("Host context has been withdrawn")

    async def check_current(self) -> None:
        self._check_open()
        try:
            await _require_completion(self._authority.check_current(self._principal, self._binding))
            self._check_open()
            await _require_completion(self._runtime.check_current(self._binding))
            self._check_open()
            # Native I/O can yield while membership or the product binding changes.
            await _require_completion(self._authority.check_current(self._principal, self._binding))
            self._check_open()
        except PermissionError:
            raise PermissionError("Current host access denied") from None
        except Exception:
            raise HostUnavailable("Current host binding is unavailable") from None

    async def authorize(self, operation: str, resource_ref: str) -> None:
        _required_strings(operation, resource_ref)
        await self.check_current()
        try:
            await _require_completion(
                self._authority.authorize(self._principal, self._binding, operation, resource_ref)
            )
        except PermissionError:
            raise PermissionError("Current host operation denied") from None
        except Exception:
            raise HostUnavailable("Current host authorization is unavailable") from None
        await self.check_current()

    async def check_ready(self, operation: str, resource_ref: str) -> None:
        """Require current grants and this allocation's native/DLP admission.

        Ready is a check, not a durable flag or permission to replay an uncertain
        effect. The native dispatch/cleanup outcome remains authoritative.
        """
        await self.authorize(operation, resource_ref)
        try:
            await _require_completion(self._runtime.check_admission(self._binding))
        except PermissionError:
            raise PermissionError("Native host admission denied") from None
        except Exception:
            raise HostUnavailable("Native host admission is unavailable") from None
        await self.authorize(operation, resource_ref)

    def host_context_resolver(
        self, resource_ref: str, read_context: Callable[[], Awaitable[str]]
    ) -> BackendHostContextResolver:
        """Bind the existing live-context callback to a registered domain reader.

        The reader is host-bound (for example PC2's SudographClient), never
        selected by an engine/browser request. No business text is cached.
        """
        _required_strings(resource_ref)

        async def resolve(request: BackendHostContextRequest) -> str:
            # Copy identifiers before awaiting authorization; do not retain a
            # mutable request as a source of target identity.
            session_id = request.get("sessionId")
            native_session_id = request.get("nativeSessionId")
            if (
                session_id != self._binding.selection.thread_id
                or native_session_id != self._binding.native.durable_session_id
            ):
                raise PermissionError("Host context is not bound to this session")
            await self.authorize("sudograph_context", resource_ref)
            try:
                text = await read_context()
            except PermissionError:
                raise PermissionError("Current model context access denied") from None
            except Exception:
                raise HostUnavailable("Current model context is unavailable") from None
            await self.authorize("sudograph_context", resource_ref)
            if not isinstance(text, str) or not text.strip():
                raise HostUnavailable("Current model context is empty")
            return text

        return resolve


async def resolve_trusted_host(
    principal: object, selection: HostSelection, authority: HostAuthority
) -> TrustedHostContext:
    """Resolve one current binding without guessing canonical IDs or credentials."""
    try:
        resolved = await authority.resolve(principal, selection)
    except PermissionError:
        raise PermissionError("Host selection denied") from None
    except Exception:
        raise HostUnavailable("Host selection is unavailable") from None
    if not isinstance(resolved, ResolvedHost) or not isinstance(resolved.binding, HostBinding):
        raise HostUnavailable("Host selection returned no trusted binding")
    if resolved.binding.selection != selection:
        raise PermissionError("Host selection does not match its trusted binding")
    context = TrustedHostContext(principal, authority, resolved)
    await context.check_current()
    return context
