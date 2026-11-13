"""Opaque canonical human request carrier for explicitly adapted consumers.

This is an internal object, never a request/response DTO. Its factory uses the
real current resolver; metadata, decoded claims and target-owner fields cannot
construct it. Python process internals remain trusted composition, not a sandbox.
"""

from __future__ import annotations

from typing import NoReturn, final

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.projection import (
    CanonicalLocalUserProjection,
    CanonicalRequestIdentity,
    CanonicalRequestIdentityResolver,
)

_FACTORY_SEAL = object()


def _unauthorized() -> MossworkError:
    return MossworkError.localized(ErrorCode.AUTH_FAILED)


@final
class CanonicalAuthenticatedRequest:
    """Carry the authenticated actor and private credential, not an effect grant.

    Consumers must call revalidate for each effect and then apply their current
    scope/resource/Moss/native policy. Actor account/member and administrator-
    selected target owner remain distinct. The local integer is only a product
    compatibility ID; no native session, credential or execution ID is invented.
    """

    __slots__ = ("__resolver", "__token", "__identity", "__seal")

    __resolver: CanonicalRequestIdentityResolver
    __token: str
    __identity: CanonicalRequestIdentity
    __seal: object

    def __new__(cls) -> CanonicalAuthenticatedRequest:
        raise TypeError("Use the authenticate factory")

    def __init_subclass__(cls) -> NoReturn:
        raise TypeError("Canonical request principals cannot be subclassed")

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("Canonical request principals are read-only")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("Canonical request principals are read-only")

    def __repr__(self) -> str:
        return "CanonicalAuthenticatedRequest()"

    def __getstate__(self) -> NoReturn:
        raise TypeError("Canonical request principals cannot be serialized")

    def _assert_authenticated(self) -> None:
        if getattr(self, "_CanonicalAuthenticatedRequest__seal", None) is not _FACTORY_SEAL:
            raise _unauthorized()

    @property
    def identity(self) -> CanonicalRequestIdentity:
        """Return the original frozen actor identity, never a cached Ready decision."""
        self._assert_authenticated()
        return self.__identity

    async def revalidate(self) -> CanonicalRequestIdentity:
        """Use the same private credential and original expected identity every time."""
        self._assert_authenticated()
        current = await self.__resolver.revalidate(self.__token, self.__identity)
        if type(current) is not CanonicalRequestIdentity or current != self.__identity:
            raise MossworkError.localized(ErrorCode.FORBIDDEN, status=403)
        return current


async def authenticate(
    resolver: CanonicalRequestIdentityResolver, opaque_access_token: str
) -> CanonicalAuthenticatedRequest:
    """Resolve a real canonical human credential and retain its original proof.

    Trusted composition supplies the concrete resolver. This factory accepts no
    snapshot, projection, target owner or service-principal metadata argument.
    Allocation, verification, current grants and database I/O stay in the resolver.
    """
    if type(resolver) is not CanonicalRequestIdentityResolver:
        raise MossworkError.localized(
            ErrorCode.INTERNAL_ERROR,
            status=503,
            details={"dependency": "canonical_request_resolver"},
        )
    identity = await resolver.resolve(opaque_access_token)
    return _seal(resolver, opaque_access_token, identity)


async def reauthenticate(
    resolver: CanonicalRequestIdentityResolver,
    opaque_access_token: str,
    original_actor: CanonicalLocalUserProjection,
) -> CanonicalAuthenticatedRequest:
    """Reauthenticate one existing actor without serializing a prior principal.

    The caller supplies a fresh normal human credential and the original queue
    actor constraint from trusted product storage. Metadata cannot authenticate
    an actor. A new JTI is allowed only after real current verification; target
    owner/resource/native/effect admission remains the consumer's responsibility.
    """
    if type(resolver) is not CanonicalRequestIdentityResolver:
        raise MossworkError.localized(
            ErrorCode.INTERNAL_ERROR,
            status=503,
            details={"dependency": "canonical_request_resolver"},
        )
    identity = await resolver.reauthenticate(opaque_access_token, original_actor)
    return _seal(resolver, opaque_access_token, identity)


def _seal(
    resolver: CanonicalRequestIdentityResolver,
    opaque_access_token: str,
    identity: CanonicalRequestIdentity,
) -> CanonicalAuthenticatedRequest:
    if type(identity) is not CanonicalRequestIdentity:
        raise _unauthorized()
    principal: CanonicalAuthenticatedRequest = object.__new__(CanonicalAuthenticatedRequest)
    object.__setattr__(principal, "_CanonicalAuthenticatedRequest__resolver", resolver)
    object.__setattr__(principal, "_CanonicalAuthenticatedRequest__token", opaque_access_token)
    object.__setattr__(principal, "_CanonicalAuthenticatedRequest__identity", identity)
    object.__setattr__(principal, "_CanonicalAuthenticatedRequest__seal", _FACTORY_SEAL)
    return principal
