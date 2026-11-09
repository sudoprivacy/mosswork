"""Retain original grants, explicit revocation and selected-member confinement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from octop.infra.agents.personal_moss_access import (
    MossMemberSnapshot,
    decide_moss_access,
    require_personal_moss_access,
)
from octop.infra.errors import ErrorCode, MossworkError

_FIXTURES = Path(__file__).parent / "fixtures" / "personal_moss_access_legacy.json"


def test_matches_original_entitlement_service_with_real_sqlite_grants() -> None:
    fixture = json.loads(_FIXTURES.read_text(encoding="utf-8"))
    assert fixture["sourceCommit"] == "59310782774b136ee44c2a3fe7fe334613e0b8fe"
    for case in fixture["cases"]:
        raw = case["member"]
        member = MossMemberSnapshot(
            organization_id=raw["orgId"],
            owner_id=raw["id"],
            role=raw["role"],
            status=raw["status"],
        )
        decision = decide_moss_access(member, case["explicitGrant"])
        expected = case["decision"]
        assert decision.is_granted == expected["isGranted"], case["name"]
        assert decision.is_enabled == expected["isEnabled"], case["name"]
        assert decision.source == expected["source"], case["name"]
        assert decision.member_status == expected["memberStatus"], case["name"]
        arguments = {
            "organization_id": case["requestedOrgId"],
            "owner_id": case["requestedOwnerId"],
            "explicit_grant": case["explicitGrant"],
        }
        if case["allowed"]:
            agent = require_personal_moss_access(member, **arguments)
            assert agent.reference == case["agent"]["ref"]
            assert agent.runtime_agent_name == case["agent"]["agentName"]
        else:
            with pytest.raises(MossworkError) as caught:
                require_personal_moss_access(member, **arguments)
            assert caught.value.code == ErrorCode.MOSS_ACCESS_DENIED, case["name"]
            assert caught.value.status == 403


def test_missing_current_member_never_acquires_a_personal_agent() -> None:
    with pytest.raises(MossworkError) as caught:
        require_personal_moss_access(
            None, organization_id="org-a", owner_id="member-a", explicit_grant=True
        )
    assert caught.value.code == ErrorCode.MOSS_ACCESS_DENIED


def test_wrong_member_snapshot_cannot_satisfy_the_selected_owner() -> None:
    member = MossMemberSnapshot("org-a", "member-a", "admin", "active")
    with pytest.raises(MossworkError) as caught:
        require_personal_moss_access(member, organization_id="org-a", owner_id="member-b")
    assert caught.value.code == ErrorCode.MOSS_ACCESS_DENIED


@pytest.mark.parametrize("grant", ["false", "true", 0, 1])
def test_mistyped_grant_is_not_coerced_to_authority(grant: object) -> None:
    member = MossMemberSnapshot("org-a", "member-a", "user", "active")
    with pytest.raises(TypeError, match="explicit_grant"):
        decide_moss_access(member, grant)  # type: ignore[arg-type]
