"""Compatibility with retained personal Moss references and owner isolation."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from octop.infra.agents.personal_moss_identity import (
    personal_moss_identity,
    resolve_personal_moss_reference,
)
from octop.infra.errors import ErrorCode, MossworkError

_FIXTURES = Path(__file__).parent / "fixtures" / "personal_moss_legacy.json"


def test_matches_original_typescript_codec_vectors() -> None:
    fixture = json.loads(_FIXTURES.read_text(encoding="utf-8"))
    assert fixture["sourceCommit"] == "59310782774b136ee44c2a3fe7fe334613e0b8fe"
    for case in fixture["cases"]:
        identity = personal_moss_identity(case["organizationId"], case["ownerId"])
        assert identity.reference == case["reference"]
        assert identity.runtime_agent_name == case["runtimeAgentName"]
        assert re.fullmatch(r"moss-[a-f0-9]{64}", identity.runtime_agent_name)
        assert (
            resolve_personal_moss_reference(
                case["reference"], organization_id=case["organizationId"], owner_id=case["ownerId"]
            )
            == identity
        )


@pytest.mark.parametrize(
    ("organization_id", "owner_id"), [("org-b", "member-a"), ("org-a", "member-b")]
)
def test_foreign_organization_or_member_is_not_resolved(
    organization_id: str, owner_id: str
) -> None:
    retained = personal_moss_identity("org-a", "member-a")
    with pytest.raises(MossworkError) as caught:
        resolve_personal_moss_reference(
            retained.reference, organization_id=organization_id, owner_id=owner_id
        )
    assert caught.value.code == ErrorCode.AGENT_NOT_FOUND
    assert caught.value.status == 404


def test_one_human_keeps_different_homes_in_different_organizations() -> None:
    opc = personal_moss_identity("opc", "opc-member")
    company = personal_moss_identity("company", "company-member")
    assert opc.reference != company.reference
    assert opc.runtime_agent_name != company.runtime_agent_name


@pytest.mark.parametrize(
    "reference",
    ["", "moss-agent:user:member-a", "moss-agent:operator:%%%", "moss-agent:operator:W10"],
)
def test_malformed_or_other_reference_families_are_rejected(reference: str) -> None:
    with pytest.raises(MossworkError) as caught:
        resolve_personal_moss_reference(reference, organization_id="org-a", owner_id="member-a")
    assert caught.value.code == ErrorCode.MOSS_IDENTITY_INVALID
    assert caught.value.status == 400


@pytest.mark.parametrize(
    "payload",
    [
        '["org-a"]',
        '{"organizationId":"org-a","ownerId":"member-a"}',
        '["org-a",42]',
        '["org-a","member-a","extra"]',
        '["org-a", "member-a"]',
        '["org-a","member-a"] ',
    ],
)
def test_noncanonical_owner_payload_is_rejected(payload: str) -> None:
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    with pytest.raises(MossworkError) as caught:
        resolve_personal_moss_reference(
            "moss-agent:operator:" + encoded, organization_id="org-a", owner_id="member-a"
        )
    assert caught.value.code == ErrorCode.MOSS_IDENTITY_INVALID


def test_padded_alias_is_rejected() -> None:
    reference = personal_moss_identity("org-a", "member-a").reference
    with pytest.raises(MossworkError) as caught:
        resolve_personal_moss_reference(
            reference + "=", organization_id="org-a", owner_id="member-a"
        )
    assert caught.value.code == ErrorCode.MOSS_IDENTITY_INVALID


@pytest.mark.parametrize(("organization_id", "owner_id"), [("", "member-a"), ("org-a", "")])
def test_empty_owner_identity_is_rejected(organization_id: str, owner_id: str) -> None:
    with pytest.raises(MossworkError) as caught:
        personal_moss_identity(organization_id, owner_id)
    assert caught.value.code == ErrorCode.MOSS_IDENTITY_INVALID
