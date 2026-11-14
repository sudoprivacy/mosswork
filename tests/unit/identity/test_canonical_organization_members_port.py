"""Original TS/SQLite oracle and real human authorization for membership adds."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import UUID

import jwt
import pytest
from tests.unit.identity.test_human_sessions import (
    _IDS,
    _SECRET,
    _claims,
    _Environment,
    _login,
    _write,
)
from tests.unit.identity.test_human_sessions import legacy_db as legacy_db
from tests.unit.identity.test_human_sessions import sessions as sessions

from octop.infra.errors import MossworkError
from octop.infra.identity import organization_members
from octop.infra.identity.organization_members import CanonicalOrganizationMemberService

_ORACLE = json.loads(
    (Path(__file__).parent / "fixtures" / "organization_members_port_legacy.json").read_text(
        encoding="utf-8"
    )
)
_PHONE = _ORACLE["phone"]
_ACCOUNT = _ORACLE["account"]
_EXISTING = _ORACLE["existingMember"]
_TABLES = _ORACLE["tables"]


@pytest.fixture
def members(sessions: _Environment) -> CanonicalOrganizationMemberService:
    with sqlite3.connect(sessions.path) as db:
        # Exact original DDL/column additions, fixture initialization only.
        db.executescript("BEGIN;" + _ORACLE["supplementalSchema"])
    return CanonicalOrganizationMemberService(
        sessions.path,
        sessions.service.snapshots,
        writer=sessions.writer,
        clock=lambda: sessions.now[0] + 0.123,
    )


def _seed(sessions: _Environment, name: str = "new_profile") -> dict[str, Any]:
    case = next(case for case in _ORACLE["cases"] if case["name"] == name)
    with sqlite3.connect(sessions.path) as db:
        for table in _TABLES:
            for row in case["before"][table]:
                if table == "users" and row["id"] == _IDS["homeMember"]:
                    db.execute("UPDATE users SET role=? WHERE id=?", (row["role"], row["id"]))
                    continue
                db.execute(
                    f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                    tuple(row.values()),
                )
        if name == "outbox_abort":
            db.executescript(
                "CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox_events "
                "BEGIN SELECT RAISE(ABORT,'fixture fail outbox'); END;"
            )
        if name == "command_abort":
            db.executescript(
                "CREATE TRIGGER fail_command BEFORE INSERT ON command_executions "
                "BEGIN SELECT RAISE(ABORT,'fixture fail command'); END;"
            )
    return case


def _rows(sessions: _Environment) -> dict[str, list[dict[str, Any]]]:
    with sqlite3.connect(sessions.path) as db:
        db.row_factory = sqlite3.Row
        return {
            table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in _TABLES
        }


def _without_fixture_actor(rows: dict[str, Any]) -> dict[str, Any]:
    result = dict(rows)
    result["users"] = [
        row
        for row in result["users"]
        if row["id"] not in (_IDS["homeMember"], _IDS["companyMember"])
    ]
    result["human_accounts"] = [
        row for row in result["human_accounts"] if row["id"] != _IDS["account"]
    ]
    result["human_account_memberships"] = [
        row for row in result["human_account_memberships"] if row["account_id"] != _IDS["account"]
    ]
    return result


def _signed(pair: Any, *, scopes: list[str] | None = None, **patch: Any) -> str:
    claims = _claims(pair.access_token)
    claims.update(scopes=["*"] if scopes is None else scopes, **patch)
    return jwt.encode(claims, _SECRET, algorithm="HS256")


def test_oracle_is_exact_original_methods_not_python_model() -> None:
    assert _ORACLE["sourceCommit"] == "d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d"
    assert len(_ORACLE["sourceHashes"]) == 10
    assert "Exact original addOrganizationMember" in _ORACLE["producer"]
    assert len(_ORACLE["cases"]) == 20


@pytest.mark.parametrize("name", [case["name"] for case in _ORACLE["cases"]])
async def test_exact_original_sqlite_oracle(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    case = _seed(sessions, name)
    sequence = iter(UUID(f"aaaaaaaa-aaaa-4aaa-8aaa-{n:012}") for n in range(1, 5))
    monkeypatch.setattr(organization_members, "uuid4", lambda: next(sequence))
    pair = await _login(sessions)
    token = _signed(pair, scopes=["sessions:list"]) if name == "narrow_scope" else pair.access_token
    if case["error"]:
        with pytest.raises(MossworkError) as raised:
            await members.add_registered_account(
                token, "bad" if name == "phone_invalid" else _PHONE
            )
        assert raised.value.status == (case["error"]["status"] or 503)
    else:
        result = await members.add_registered_account(token, "+86 " + _PHONE)
        assert {"id": result.member.member_id, "created": result.created} == case["result"]
    assert _without_fixture_actor(_rows(sessions)) == _without_fixture_actor(case["after"])


async def test_admin_add_then_real_target_login_and_company_switch_preserves_opc(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed(sessions)
    admin = await _login(sessions)
    before = _rows(sessions)
    added = await members.add_registered_account(admin.access_token, _PHONE)
    assert added.created and added.member.account_id == _ACCOUNT
    assert added.member.role == "user" and added.member.status == "active"
    assert added.member.organization_id == _IDS["homeOrg"]
    assert added.member.legacy_id == 2_000_000_000
    assert UUID(added.member.member_id) and added.member.member_id != _EXISTING
    assert [row for row in _rows(sessions)["users"] if row["id"] == _EXISTING] == [
        row for row in before["users"] if row["id"] == _EXISTING
    ]
    assert _rows(sessions)["user_auth_identities"] == before["user_auth_identities"]
    await sessions.service.send_code(_PHONE)
    target = await sessions.service.login(_PHONE, sessions.sender.sent[-1][1])
    organizations = await sessions.service.snapshots.organizations(target.access_token)
    assert {(m.organization_id, m.member_id) for m in organizations} == {
        (_IDS["companyOrg"], _EXISTING),
        (_IDS["homeOrg"], added.member.member_id),
    }
    switched = await sessions.service.switch_organization(target.access_token, _IDS["homeOrg"])
    snapshot = await sessions.service.snapshots.snapshot(switched.access_token)
    assert snapshot.member.owner_id == added.member.member_id
    assert snapshot.account_id == _ACCOUNT
    assert "admin:users" not in snapshot.scopes and "*" not in snapshot.scopes
    with pytest.raises(MossworkError) as denied:
        await members.list_members(switched.access_token)
    assert denied.value.status == 403


async def test_list_is_current_org_safe_readonly_and_writer_independent(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed(sessions)
    pair = await _login(sessions)
    added = await members.add_registered_account(pair.access_token, _PHONE)
    sessions.writer.is_allowed = False
    before, calls = sessions.path.read_bytes(), sessions.writer.calls
    rows = await members.list_members(pair.access_token)
    assert {row.member_id for row in rows} == {_IDS["homeMember"], added.member.member_id}
    assert all(row.organization_id == _IDS["homeOrg"] for row in rows)
    assert all(
        not {"phone", "password_hash", "sudorouter_key", "balance_units"}.intersection(asdict(row))
        for row in rows
    )
    assert sessions.writer.calls == calls
    assert sessions.path.read_bytes() == before


async def test_existing_disabled_membership_is_not_reenabled_or_backfilled(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed(sessions, "existing_disabled")
    pair = await _login(sessions)
    before = _rows(sessions)
    result = await members.add_registered_account(pair.access_token, _PHONE)
    assert not result.created and result.member.member_id == _EXISTING
    assert result.member.status == "disabled" and result.member.membership_revision == 9
    assert _rows(sessions) == before


async def test_legacy_phone_member_without_real_human_account_is_not_backfilled(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed(sessions)
    pair = await _login(sessions)
    _write(sessions, "DELETE FROM human_account_memberships WHERE account_id=?", (_ACCOUNT,))
    _write(sessions, "DELETE FROM human_accounts WHERE id=?", (_ACCOUNT,))
    before = _rows(sessions)
    with pytest.raises(MossworkError) as denied:
        await members.add_registered_account(pair.access_token, _PHONE)
    assert denied.value.status == 404
    assert _rows(sessions) == before


async def test_admin_moss_use_revocation_does_not_remove_membership_management(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed(sessions)
    pair = await _login(sessions)
    _write(
        sessions,
        "INSERT INTO moss_member_access VALUES (?,?,0,?,1)",
        (_IDS["homeOrg"], _IDS["homeMember"], _IDS["homeMember"]),
    )
    assert (await sessions.service.snapshots.snapshot(pair.access_token)).personal_agent is None
    result = await members.add_registered_account(pair.access_token, _PHONE)
    assert result.created
    assert result.member in await members.list_members(pair.access_token)


async def test_dangling_existing_membership_is_conflict_without_replacement(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed(sessions, "existing_active")
    pair = await _login(sessions)
    _write(sessions, "DELETE FROM users WHERE id=?", (_EXISTING,))
    before = _rows(sessions)
    with pytest.raises(MossworkError) as denied:
        await members.add_registered_account(pair.access_token, _PHONE)
    assert denied.value.status == 409
    assert _rows(sessions) == before


async def test_two_service_instances_same_account_have_single_transaction_winner(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed(sessions)
    pair = await _login(sessions)
    other = CanonicalOrganizationMemberService(
        sessions.path,
        sessions.service.snapshots,
        writer=sessions.writer,
        clock=lambda: sessions.now[0],
    )
    results = await asyncio.gather(
        members.add_registered_account(pair.access_token, _PHONE),
        other.add_registered_account(pair.access_token, _PHONE),
    )
    assert sorted(result.created for result in results) == [False, True]
    assert results[0].member.member_id == results[1].member.member_id
    after = _rows(sessions)
    assert len(after["outbox_events"]) == len(after["command_executions"]) == 1
    assert len(after["wallets"]) == 2
    assert after["compatibility_id_counters"] == [
        {"counter_key": "resource_numeric_aliases:user", "last_value": 2_000_000_000}
    ]


@pytest.mark.parametrize(
    "change", ["revoke", "revision", "role", "account", "org", "expiry", "target_account"]
)
async def test_authorization_is_rechecked_after_writer_await_in_actual_transaction(
    sessions: _Environment, members: CanonicalOrganizationMemberService, change: str
) -> None:
    _seed(sessions)
    pair = await _login(sessions)
    claims = _claims(pair.access_token)
    sessions.writer.calls = 0

    def mutate(call: int) -> None:
        if call != 1:
            return
        if change == "revoke":
            _write(
                sessions, "INSERT INTO revoked_tokens VALUES (?,?)", (claims["jti"], claims["exp"])
            )
        elif change == "revision":
            _write(
                sessions, "UPDATE users SET membership_revision=1 WHERE id=?", (_IDS["homeMember"],)
            )
        elif change == "role":
            _write(sessions, "UPDATE users SET role='user' WHERE id=?", (_IDS["homeMember"],))
        elif change == "account":
            _write(
                sessions,
                "UPDATE human_accounts SET status='disabled' WHERE id=?",
                (_IDS["account"],),
            )
        elif change == "org":
            _write(sessions, "DELETE FROM organizations WHERE id=?", (_IDS["homeOrg"],))
        elif change == "expiry":
            sessions.now[0] += 300
        else:
            _write(sessions, "UPDATE human_accounts SET status='locked' WHERE id=?", (_ACCOUNT,))

    sessions.writer.on_call = mutate
    with pytest.raises(MossworkError) as denied:
        await members.add_registered_account(pair.access_token, _PHONE)
    assert denied.value.status == (404 if change == "target_account" else 401)
    assert not _rows(sessions)["outbox_events"]
    assert not _rows(sessions)["command_executions"]


@pytest.mark.parametrize("operation", ["list", "add"])
@pytest.mark.parametrize("scopes", [[], ["sessions:list"]])
async def test_signed_scope_is_required_even_for_admin(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
    operation: str,
    scopes: list[str],
) -> None:
    _seed(sessions)
    pair = await _login(sessions)
    before = _rows(sessions)
    with pytest.raises(MossworkError) as denied:
        if operation == "list":
            await members.list_members(_signed(pair, scopes=scopes))
        else:
            await members.add_registered_account(_signed(pair, scopes=scopes), _PHONE)
    assert denied.value.status == 403
    assert _rows(sessions) == before


async def test_foreign_org_super_admin_claim_is_not_membership(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed(sessions)
    pair = await _login(sessions)
    with pytest.raises(MossworkError) as denied:
        await members.add_registered_account(
            _signed(pair, org_id=_IDS["companyOrg"], role="super_admin"), _PHONE
        )
    assert denied.value.status == 401
    assert not _rows(sessions)["outbox_events"]


async def test_revocation_after_list_read_is_not_reported_as_authorized(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = await _login(sessions)
    claims, read = _claims(pair.access_token), members._list

    def read_then_revoke(snapshot: Any):
        result = read(snapshot)
        _write(sessions, "INSERT INTO revoked_tokens VALUES (?,?)", (claims["jti"], claims["exp"]))
        return result

    monkeypatch.setattr(members, "_list", read_then_revoke)
    with pytest.raises(MossworkError) as denied:
        await members.list_members(pair.access_token)
    assert denied.value.status == 401


async def test_initial_writer_denial_never_creates_any_member_effect(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed(sessions)
    pair = await _login(sessions)
    before = _rows(sessions)
    sessions.writer.is_allowed = False
    with pytest.raises(MossworkError) as denied:
        await members.add_registered_account(pair.access_token, _PHONE)
    assert denied.value.status == 503
    assert _rows(sessions) == before


async def test_final_writer_loss_does_not_claim_rollback_of_committed_transaction(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed(sessions)
    pair = await _login(sessions)
    sessions.writer.calls = 0

    def lose_lease(call: int) -> None:
        if call == 2:
            sessions.writer.is_allowed = False

    sessions.writer.on_call = lose_lease
    with pytest.raises(MossworkError) as denied:
        await members.add_registered_account(pair.access_token, _PHONE)
    assert denied.value.status == 503
    assert len(_rows(sessions)["outbox_events"]) == 1
    sessions.writer.is_allowed, sessions.writer.on_call = True, None
    assert not (await members.add_registered_account(pair.access_token, _PHONE)).created
    assert len(_rows(sessions)["command_executions"]) == 1


@pytest.mark.parametrize(
    "table", ["wallets", "outbox_events", "command_executions", "compatibility_id_counters"]
)
async def test_missing_retained_tables_fail_closed_without_schema_repair(
    sessions: _Environment, members: CanonicalOrganizationMemberService, table: str
) -> None:
    _seed(sessions)
    pair = await _login(sessions)
    _write(sessions, f"DROP TABLE {table}")
    before = sessions.path.read_bytes()
    with pytest.raises(MossworkError) as denied:
        await members.add_registered_account(pair.access_token, _PHONE)
    assert denied.value.status == 503
    assert sessions.path.read_bytes() == before


def test_snapshot_and_writer_store_must_match(sessions: _Environment, tmp_path: Path) -> None:
    with pytest.raises(MossworkError) as denied:
        CanonicalOrganizationMemberService(
            tmp_path / "other.sqlite", sessions.service.snapshots, writer=sessions.writer
        )
    assert denied.value.status == 503
    assert not (tmp_path / "other.sqlite").exists()
