"""Membership HTTP calls keep canonical company/actor checks and typed responses."""

from pathlib import Path

import httpx
from tests.support.app import mosswork_client, write_mosswork_config
from tests.unit.identity.test_canonical_organization_members_port import (
    _ACCOUNT,
    _IDS,
    _PHONE,
    _login,
    _seed,
)
from tests.unit.identity.test_canonical_organization_members_port import legacy_db as legacy_db
from tests.unit.identity.test_canonical_organization_members_port import members as members
from tests.unit.identity.test_canonical_organization_members_port import sessions as sessions
from tests.unit.identity.test_human_sessions import _Environment

from octop.api.app import build_app
from octop.infra.identity.organization_members import CanonicalOrganizationMemberService


async def test_same_account_retains_opc_and_gets_independent_enterprise_member_through_http(
    sessions: _Environment, members: CanonicalOrganizationMemberService, tmp_path: Path
) -> None:
    _seed(sessions)
    home = tmp_path / "product"
    write_mosswork_config(home, enable_api_docs=True, enable_dashboard=False)
    admin = await _login(sessions)
    headers = {"Authorization": "Bearer " + admin.access_token}
    async with mosswork_client(home) as (_, server):
        app = build_app(server, canonical_sessions=sessions.service, canonical_members=members)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            path = "/api/identity/members"
            assert (await client.get(path)).status_code == 401
            initial = await client.get(path, headers=headers)
            assert initial.status_code == 200
            assert {row["member_id"] for row in initial.json()["members"]} == {_IDS["homeMember"]}
            added = await client.post(path, json={"phone": _PHONE}, headers=headers)
            assert added.status_code == 200, added.text
            body = added.json()
            assert body["created"] is True and body["member"]["account_id"] == _ACCOUNT
            assert body["member"]["role"] == "user"
            member_id = body["member"]["member_id"]
            again = await client.post(path, json={"phone": _PHONE}, headers=headers)
            assert again.status_code == 200
            assert again.json() == {"member": body["member"], "created": False}
            listing = await client.get(path, headers=headers)
            assert {row["member_id"] for row in listing.json()["members"]} == {
                _IDS["homeMember"],
                member_id,
            }
            assert listing.headers["cache-control"] == "no-store"
            assert not any(
                word in listing.text for word in ("password", "secret", "token_limit", _PHONE)
            )
            selector = await client.post(
                path, json={"phone": _PHONE, "organization_id": _IDS["companyOrg"]}, headers=headers
            )
            assert selector.status_code == 422
            invalid = await client.post(
                path, json={"phone": "not-a-phone-value-for-this-request"}, headers=headers
            )
            assert invalid.status_code == 400
            assert "not-a-phone-value-for-this-request" not in invalid.text
            switched = await sessions.service.switch_organization(
                admin.access_token, _IDS["companyOrg"]
            )
            denied = await client.get(
                path, headers={"Authorization": "Bearer " + switched.access_token}
            )
            assert denied.status_code == 403
            assert (await client.get(path + "/extra", headers=headers)).status_code == 503
            operation = app.openapi()["paths"][path]["post"]
            assert operation["security"] == [{"CanonicalHumanBearer": []}]
            assert operation["requestBody"]["content"]["application/json"]["schema"]
            assert server.user_manager is not None and server.user_manager.count() == 0
