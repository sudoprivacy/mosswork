"""Personal Moss entitlement rules for canonical organization members.

The caller obtains current member and explicit-grant records from the existing
authority before dispatch. A decoded token role or a local integer view ID is
not an authoritative member record. This module stores no identities or grants.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from octop.infra.agents.personal_moss_identity import PersonalMossIdentity, personal_moss_identity
from octop.infra.errors import ErrorCode, MossworkError


@dataclass(frozen=True)
class MossMemberSnapshot:
    organization_id: str
    owner_id: str
    role: str
    status: str


@dataclass(frozen=True)
class MossAccessDecision:
    organization_id: str
    owner_id: str
    is_granted: bool
    is_enabled: bool
    source: Literal["role", "explicit"]
    member_status: str


def decide_moss_access(
    member: MossMemberSnapshot, explicit_grant: bool | None = None
) -> MossAccessDecision:
    """Apply explicit grants/revocations before role defaults, then member status."""
    if explicit_grant is not None and not isinstance(explicit_grant, bool):
        raise TypeError("explicit_grant must be a boolean or None")
    is_granted = (
        explicit_grant if explicit_grant is not None else member.role in {"admin", "super_admin"}
    )
    return MossAccessDecision(
        organization_id=member.organization_id,
        owner_id=member.owner_id,
        is_granted=is_granted,
        is_enabled=member.status == "active" and is_granted,
        source="explicit" if explicit_grant is not None else "role",
        member_status=member.status,
    )


def require_personal_moss_access(
    member: MossMemberSnapshot | None,
    *,
    organization_id: str,
    owner_id: str,
    explicit_grant: bool | None = None,
) -> PersonalMossIdentity:
    """Require the selected canonical member and entitlement before resolving its Agent."""
    if (
        member is None
        or member.organization_id != organization_id
        or member.owner_id != owner_id
        or not decide_moss_access(member, explicit_grant).is_enabled
    ):
        raise MossworkError.localized(ErrorCode.MOSS_ACCESS_DENIED, status=403)
    return personal_moss_identity(organization_id, owner_id)
