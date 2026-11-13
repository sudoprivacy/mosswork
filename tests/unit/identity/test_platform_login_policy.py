"""Pinned original platform policy compatibility and real actor/writer races."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from tests.unit.identity.test_canonical_organization_members_port import _signed
from tests.unit.identity.test_human_sessions import _IDS, _Environment, _login, _write
from tests.unit.identity.test_human_sessions import legacy_db as legacy_db
from tests.unit.identity.test_human_sessions import sessions as sessions

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.platform_login_policy import CanonicalPlatformLoginPolicyService

_ORACLE = json.loads(
    (Path(__file__).parent / "fixtures/platform_login_policy_legacy.json").read_text(
        encoding="utf-8"
    )
)


class _Readiness:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.result: Any = None
        self.on_call: Any = None

    async def assert_platform_available(self, method: str) -> Any:
        self.calls.append(method)
        if self.on_call is not None:
            self.on_call()
        return self.result


def _service(
    environment: _Environment, readiness: _Readiness | None = None
) -> CanonicalPlatformLoginPolicyService:
    return CanonicalPlatformLoginPolicyService(
        environment.path,
        environment.service.snapshots,
        writer=environment.writer,
        readiness=readiness if readiness is not None else _Readiness(),
        clock=lambda: environment.now[0],
    )


async def _super_token(environment: _Environment) -> str:
    _write(environment, "UPDATE users SET role='super_admin' WHERE id=?", (_IDS["homeMember"],))
    return (await _login(environment)).access_token


def _row(environment: _Environment, scope: str, scope_id: str) -> tuple[Any, ...] | None:
    with sqlite3.connect(environment.path) as db:
        return db.execute(
            "SELECT policy_json,updated_by,created_at,updated_at FROM client_delivery_policies "
            "WHERE scope_type=? AND scope_id=?",
            (scope, scope_id),
        ).fetchone()


@pytest.mark.parametrize(
    "case",
    [case for case in _ORACLE["cases"] if case["status"] == 200],
    ids=lambda case: case["name"],
)
async def test_platform_rows_match_original_scope_update_and_sqlite(
    sessions: _Environment, case: dict[str, Any]
) -> None:
    sessions.now[0] = _ORACLE["nowMs"] / 1000
    token = await _super_token(sessions)
    with sqlite3.connect(sessions.path) as db:
        if "platform" in case or "platformRaw" in case:
            raw = case.get("platformRaw", json.dumps(case.get("platform")))
            db.execute(
                "INSERT INTO client_delivery_policies VALUES ('platform','default',?,?,7,8)",
                (raw, "original-writer"),
            )
        db.execute(
            "INSERT INTO client_delivery_policies VALUES ('organization',?,?,?,3,4)",
            (_IDS["homeOrg"], case["organization"]["policy_json"], "original-org-writer"),
        )
        db.execute(
            "INSERT INTO client_delivery_policies VALUES ('organization',?,?,?,3,4)",
            (_IDS["companyOrg"], '{"foreign":"unchanged"}', "foreign-writer"),
        )
    original_org = _row(sessions, "organization", _IDS["homeOrg"])
    foreign_org = _row(sessions, "organization", _IDS["companyOrg"])
    readiness = _Readiness()
    method = ("sms", "password", "cas")[case["body"]["login_method"]]
    await _service(sessions, readiness).set_method(token, method)
    row = _row(sessions, "platform", "default")
    expected = case["expected"]
    assert row is not None and json.loads(row[0]) == expected["policy_json"]
    assert row[1:] == (_IDS["homeMember"], expected["created_at"], expected["updated_at"])
    assert _row(sessions, "organization", _IDS["homeOrg"]) == original_org
    assert _row(sessions, "organization", _IDS["companyOrg"]) == foreign_org
    assert readiness.calls == ([method] if method != "password" else [])


@pytest.mark.parametrize(
    ("role", "scopes"),
    [
        ("admin", ["*"]),
        ("dept_admin", ["*"]),
        ("user", ["*"]),
        ("super_admin", []),
        ("super_admin", ["admin:users"]),
        ("super_admin", ["sessions:create"]),
    ],
)
async def test_current_super_admin_and_signed_ceiling_both_required(
    sessions: _Environment, role: str, scopes: list[str]
) -> None:
    _write(sessions, "UPDATE users SET role=? WHERE id=?", (role, _IDS["homeMember"]))
    token = _signed(await _login(sessions), scopes=scopes)
    calls = sessions.writer.calls
    before = sessions.path.read_bytes()
    with pytest.raises(MossworkError) as denied:
        await _service(sessions).set_method(token, "password")
    assert denied.value.status == 403
    assert sessions.writer.calls == calls
    assert sessions.path.read_bytes() == before


async def test_existing_admin_namespace_ceiling_keeps_platform_super_admin_requirement(
    sessions: _Environment,
) -> None:
    _write(sessions, "UPDATE users SET role='super_admin' WHERE id=?", (_IDS["homeMember"],))
    token = _signed(await _login(sessions), scopes=["admin:*"])
    await _service(sessions).set_method(token, "password")
    assert _row(sessions, "platform", "default") is not None
    assert _row(sessions, "organization", _IDS["homeOrg"]) is None


@pytest.mark.parametrize("result", [False, True, 0, {}, "ready"])
async def test_malformed_platform_readiness_never_writes(
    sessions: _Environment, result: object
) -> None:
    token = await _super_token(sessions)
    readiness = _Readiness()
    readiness.result = result
    with pytest.raises(MossworkError) as refused:
        await _service(sessions, readiness).set_method(token, "sms")
    assert refused.value.status == 503
    assert _row(sessions, "platform", "default") is None


@pytest.mark.parametrize("method", ["sms", "cas"])
async def test_actual_platform_unavailability_refuses_without_write(
    sessions: _Environment, method: Any
) -> None:
    token = await _super_token(sessions)
    readiness = _Readiness()

    def unavailable() -> None:
        raise MossworkError.localized(ErrorCode.IDENTITY_REQUEST_INVALID, status=400)

    readiness.on_call = unavailable
    with pytest.raises(MossworkError) as refused:
        await _service(sessions, readiness).set_method(token, method)
    assert refused.value.status == 400
    assert readiness.calls == [method]
    assert _row(sessions, "platform", "default") is None


@pytest.mark.parametrize(
    ("sql", "parameters"),
    [
        ("UPDATE users SET role='admin' WHERE id=?", (_IDS["homeMember"],)),
        ("UPDATE users SET status='disabled' WHERE id=?", (_IDS["homeMember"],)),
        ("UPDATE users SET membership_revision=9 WHERE id=?", (_IDS["homeMember"],)),
        ("DELETE FROM human_account_memberships WHERE user_id=?", (_IDS["homeMember"],)),
    ],
)
async def test_actor_withdrawal_during_readiness_refuses_before_write(
    sessions: _Environment, sql: str, parameters: tuple[Any, ...]
) -> None:
    token = await _super_token(sessions)
    readiness = _Readiness()
    readiness.on_call = lambda: _write(sessions, sql, parameters)
    with pytest.raises(MossworkError) as denied:
        await _service(sessions, readiness).set_method(token, "cas")
    assert denied.value.status in (401, 403)
    assert _row(sessions, "platform", "default") is None


async def test_sql_transaction_checks_role_after_last_await(sessions: _Environment) -> None:
    token = await _super_token(sessions)
    sessions.writer.calls = 0

    def demote(call: int) -> None:
        if call == 2:
            _write(sessions, "UPDATE users SET role='admin' WHERE id=?", (_IDS["homeMember"],))

    sessions.writer.on_call = demote
    with pytest.raises(MossworkError) as denied:
        await _service(sessions).set_method(token, "password")
    assert denied.value.status == 401
    assert _row(sessions, "platform", "default") is None


async def test_late_writer_withdrawal_preserves_committed_outcome_without_retry(
    sessions: _Environment,
) -> None:
    token = await _super_token(sessions)
    sessions.writer.calls = 0

    def withdraw(call: int) -> None:
        if call == 3:
            sessions.writer.is_allowed = False

    sessions.writer.on_call = withdraw
    with pytest.raises(MossworkError) as refused:
        await _service(sessions).set_method(token, "password")
    assert refused.value.status == 503
    row = _row(sessions, "platform", "default")
    assert row is not None and json.loads(row[0])["loginMethod"] == 1
    assert sessions.writer.calls == 3


async def test_missing_original_schema_is_not_initialized(sessions: _Environment) -> None:
    token = await _super_token(sessions)
    _write(sessions, "DROP TABLE client_delivery_policies")
    with pytest.raises(MossworkError) as unavailable:
        await _service(sessions).set_method(token, "password")
    assert unavailable.value.status == 503
    with sqlite3.connect(sessions.path) as db:
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='client_delivery_policies'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("method", [False, 0, None, "", "SMS", " sms "])
async def test_invalid_method_refuses_before_credential_or_writer(
    sessions: _Environment, method: Any
) -> None:
    calls = sessions.writer.calls
    with pytest.raises(MossworkError) as invalid:
        await _service(sessions).set_method("not-a-credential", method)
    assert invalid.value.status == 400
    assert sessions.writer.calls == calls
    assert _row(sessions, "platform", "default") is None


async def test_different_database_cannot_receive_the_verified_actor(
    sessions: _Environment, tmp_path: Path
) -> None:
    other = tmp_path / "unselected.sqlite"
    with pytest.raises(MossworkError) as unavailable:
        CanonicalPlatformLoginPolicyService(
            other, sessions.service.snapshots, writer=sessions.writer, readiness=_Readiness()
        )
    assert unavailable.value.status == 503
    assert not other.exists()


async def test_org_only_readiness_cannot_be_used_as_platform_readiness(
    sessions: _Environment,
) -> None:
    from tests.unit.identity.test_organization_login_policy import _Readiness as OrgReadiness

    invalid_readiness: Any = OrgReadiness()
    with pytest.raises(TypeError, match="platform login-method readiness"):
        CanonicalPlatformLoginPolicyService(
            sessions.path,
            sessions.service.snapshots,
            writer=sessions.writer,
            readiness=invalid_readiness,
        )


async def test_broken_platform_readiness_is_unavailable(sessions: _Environment) -> None:
    token = await _super_token(sessions)
    readiness = _Readiness()

    def broken() -> None:
        raise RuntimeError("fixture provider unavailable")

    readiness.on_call = broken
    with pytest.raises(MossworkError) as unavailable:
        await _service(sessions, readiness).set_method(token, "cas")
    assert unavailable.value.status == 503
    assert _row(sessions, "platform", "default") is None
