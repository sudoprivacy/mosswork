"""Fresh canonical request identity and separate product compatibility IDs.

Only explicitly composed canonical routes may allocate these product mappings.
The read-only public identity endpoint does not call this resolver. Neither a
projection nor a request identity is action/resource/Moss/native authorization.
Do not turn the projected User into an alternate local login or permission path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial

from octop.infra.db.repos.canonical_identity import (
    CanonicalIdentityProjectionRepo,
    CanonicalProjectionConflict,
    CanonicalProjectionRow,
    CanonicalProjectionUserChanged,
)
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import CanonicalIdentitySnapshot, CanonicalSnapshotService


@dataclass(frozen=True)
class CanonicalIdentityKey:
    authority_id: str
    account_id: str
    organization_id: str
    member_id: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (
                self.authority_id,
                self.account_id,
                self.organization_id,
                self.member_id,
            )
        ):
            raise ValueError("Invalid canonical identity key")


@dataclass(frozen=True)
class CanonicalLocalUserProjection:
    identity: CanonicalIdentityKey
    local_user_id: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.identity, CanonicalIdentityKey)
            or type(self.local_user_id) is not int
            or self.local_user_id <= 0
        ):
            raise ValueError("Invalid product user projection")


def _identity(snapshot: CanonicalIdentitySnapshot) -> CanonicalIdentityKey:
    return CanonicalIdentityKey(
        snapshot.authority_id,
        snapshot.account_id,
        snapshot.organization_id,
        snapshot.member.owner_id,
    )


@dataclass(frozen=True)
class CanonicalRequestIdentity:
    snapshot: CanonicalIdentitySnapshot
    projection: CanonicalLocalUserProjection

    def __post_init__(self) -> None:
        if (
            not isinstance(self.snapshot, CanonicalIdentitySnapshot)
            or not isinstance(self.projection, CanonicalLocalUserProjection)
            or self.projection.identity != _identity(self.snapshot)
        ):
            raise ValueError("Request identity and product projection disagree")


def _stale() -> MossworkError:
    return MossworkError.localized(ErrorCode.FORBIDDEN, status=403)


class CanonicalRequestIdentityResolver:
    """Resolve verified human/self identity, never a cached Ready or grant.

    Product allocation/read awaits are bracketed by fresh authority snapshots.
    The mapping is read again after allocation and canonical revalidation. Any
    snapshot change invalidates the attempt, including role, grants, revision,
    signed scopes and credential provenance. Revalidate never allocates.

    Separate stores cannot provide one cross-database transaction. A returned
    value records the observed identity only; consumers must revalidate and
    apply current action/resource/native policy at the actual effect boundary.
    Canonical admin never becomes product admin or inherits local permissions.
    """

    def __init__(
        self,
        snapshots: CanonicalSnapshotService,
        product_projections: CanonicalIdentityProjectionRepo,
    ) -> None:
        self._snapshots = snapshots
        self._projections = product_projections

    async def resolve(self, opaque_access_token: str) -> CanonicalRequestIdentity:
        """Explicitly allocate a product-only compatibility mapping if absent."""
        return await self._resolve(opaque_access_token, expected=None)

    async def revalidate(
        self, opaque_access_token: str, expected: CanonicalRequestIdentity
    ) -> CanonicalRequestIdentity:
        """Require unchanged current identity and mapping; rerun effect authorization."""
        if not isinstance(expected, CanonicalRequestIdentity):
            raise _stale()
        return await self._resolve(opaque_access_token, expected=expected)

    async def reauthenticate(
        self, opaque_access_token: str, original_actor: CanonicalLocalUserProjection
    ) -> CanonicalRequestIdentity:
        """Verify a fresh human credential against an existing original actor.

        Trusted queue composition supplies the original immutable projection as
        a constraint, never proof. Current identity/scopes/JTI come exclusively
        from the newly verified credential. This path never allocates or links
        a missing projection and grants no right to replay an uncertain effect.
        """
        if type(original_actor) is not CanonicalLocalUserProjection:
            raise _stale()
        return await self._resolve(
            opaque_access_token, expected=None, original_actor=original_actor
        )

    async def _product(
        self, identity: CanonicalIdentityKey, *, allocate: bool
    ) -> CanonicalLocalUserProjection:
        operation = self._projections.get_or_create if allocate else self._projections.get
        try:
            row: CanonicalProjectionRow | None = await asyncio.get_running_loop().run_in_executor(
                None,
                partial(
                    operation,
                    authority_id=identity.authority_id,
                    account_id=identity.account_id,
                    organization_id=identity.organization_id,
                    member_id=identity.member_id,
                ),
            )
        except CanonicalProjectionConflict as exc:
            raise MossworkError.localized(ErrorCode.AUTH_FAILED) from exc
        except CanonicalProjectionUserChanged as exc:
            raise _stale() from exc
        except Exception as exc:
            raise MossworkError.localized(
                ErrorCode.INTERNAL_ERROR,
                status=503,
                details={"dependency": "canonical_product_projection"},
            ) from exc
        if row is None:
            raise _stale()
        actual_identity = CanonicalIdentityKey(
            row.authority_id, row.account_id, row.organization_id, row.member_id
        )
        if actual_identity != identity:
            raise _stale()
        return CanonicalLocalUserProjection(actual_identity, row.local_user_id)

    async def _resolve(
        self,
        token: str,
        *,
        expected: CanonicalRequestIdentity | None,
        original_actor: CanonicalLocalUserProjection | None = None,
    ) -> CanonicalRequestIdentity:
        before = await self._snapshots.snapshot(token)
        if expected is not None and before != expected.snapshot:
            raise _stale()
        key = _identity(before)
        constraint = expected.projection if expected is not None else original_actor
        if constraint is not None and key != constraint.identity:
            raise _stale()
        projection = await self._product(key, allocate=constraint is None)
        if constraint is not None and projection != constraint:
            raise _stale()
        after = await self._snapshots.snapshot(token)
        if after != before:
            raise _stale()
        current_projection = await self._product(key, allocate=False)
        if current_projection != projection:
            raise _stale()
        current = await self._snapshots.snapshot(token)
        if current != after:
            raise _stale()
        return CanonicalRequestIdentity(current, current_projection)
