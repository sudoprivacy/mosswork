"""Preserve personal Moss references and native Agent homes during migration.

This codec carries the existing organization/member identity. Membership,
entitlement and native credentials must come from their trusted authorities;
decoding a reference does not grant any of them.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

from octop.infra.errors import ErrorCode, MossworkError

_REFERENCE_PREFIX = "moss-agent:operator:"


@dataclass(frozen=True)
class PersonalMossIdentity:
    organization_id: str
    owner_id: str
    reference: str
    runtime_agent_name: str


def _invalid_reference() -> MossworkError:
    return MossworkError.localized(ErrorCode.MOSS_IDENTITY_INVALID, status=400)


def personal_moss_identity(organization_id: str, owner_id: str) -> PersonalMossIdentity:
    """Encode the original canonical org/member IDs without changing their Agent home."""
    if not isinstance(organization_id, str) or not isinstance(owner_id, str):
        raise _invalid_reference()
    if not organization_id or not owner_id:
        raise _invalid_reference()
    try:
        payload = json.dumps(
            [organization_id, owner_id], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except UnicodeError as exc:
        raise _invalid_reference() from exc
    reference = _REFERENCE_PREFIX + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return PersonalMossIdentity(
        organization_id=organization_id,
        owner_id=owner_id,
        reference=reference,
        runtime_agent_name="moss-" + hashlib.sha256(payload).hexdigest(),
    )


def resolve_personal_moss_reference(
    reference: str, *, organization_id: str, owner_id: str
) -> PersonalMossIdentity:
    """Resolve a canonical reference only for the trusted selected org/member pair."""
    if not isinstance(reference, str) or not reference.startswith(_REFERENCE_PREFIX):
        raise _invalid_reference()
    encoded = reference[len(_REFERENCE_PREFIX) :]
    try:
        payload = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        owner = json.loads(payload.decode("utf-8"))
    except ValueError as exc:
        raise _invalid_reference() from exc
    if not isinstance(owner, list) or len(owner) != 2:
        raise _invalid_reference()
    decoded = personal_moss_identity(owner[0], owner[1])
    if decoded.reference != reference:
        raise _invalid_reference()
    expected = personal_moss_identity(organization_id, owner_id)
    if decoded != expected:
        raise MossworkError.localized(ErrorCode.AGENT_NOT_FOUND, status=404)
    return decoded
