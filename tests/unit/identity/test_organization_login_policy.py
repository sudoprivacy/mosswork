"""Original TS/SQLite policy oracle plus real canonical actor/writer races."""

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
from octop.infra.identity.organization_login_policy import CanonicalOrganizationLoginPolicyService

_ORACLE = json.loads(
    (Path(__file__).parent / "fixtures/organization_login_policy_legacy.json").read_text(
        encoding="utf-8"
    )
)


class _Readiness:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.result: Any = None
        self.on_call: Any = None

    async def assert_available(self, method: str, organization_id: str) -> Any:
        self.calls.append((method, organization_id))
        if self.on_call is not None:
            self.on_call()
        return self.result


def _service(
    environment: _Environment, readiness: _Readiness
) -> CanonicalOrganizationLoginPolicyService:
    return CanonicalOrganizationLoginPolicyService(
        environment.path,
        environment.service.snapshots,
        writer=environment.writer,
        readiness=readiness,
        clock=lambda: environment.now[0],
    )


def _policy(environment: _Environment, scope: str = "organization") -> tuple[Any, ...] | None:
    with sqlite3.connect(environment.path) as db:
        return db.execute(
            "SELECT policy_json,updated_by,created_at,updated_at FROM client_delivery_policies "
            "WHERE scope_type=? AND scope_id=?",
            (scope, _IDS["homeOrg"] if scope == "organization" else "default"),
        ).fetchone()


@pytest.mark.parametrize("case", _ORACLE["cases"], ids=lambda case: case["name"])
async def test_current_org_updates_match_original_typescript_sqlite(
    sessions: _Environment, case: dict[str, Any]
) -> None:
    sessions.now[0] = _ORACLE["nowMs"] / 1000
    _write(
        sessions,
        "UPDATE users SET role=? WHERE id=?",
        (case.get("role", "admin"), _IDS["homeMember"]),
    )
    token = (await _login(sessions)).access_token
    with sqlite3.connect(sessions.path) as db:
        for scope, scope_id, field in (
            ("platform", "default", "platform"),
            ("organization", _IDS["homeOrg"], "organization"),
        ):
            if field in case or field + "Raw" in case:
                raw = case.get(field + "Raw", json.dumps(case.get(field)))
                db.execute(
                    "INSERT INTO client_delivery_policies VALUES (?,?,?,?,?,?)",
                    (scope, scope_id, raw, "original-writer", 7, 8),
                )
    readiness = _Readiness()
    service = _service(sessions, readiness)
    platform = _policy(sessions, "platform")
    body = case["body"]
    if body.get("inherit_login_method") is True:
        await service.inherit(token)
    else:
        await service.set_method(token, ("sms", "password", "cas")[body["login_method"]])
    actual = _policy(sessions)
    assert actual is not None
    expected = case["expected"]
    assert json.loads(actual[0]) == expected["policy_json"]
    assert actual[1:] == (
        _IDS["homeMember"],
        expected["created_at"],
        expected["updated_at"],
    )
    assert _policy(sessions, "platform") == platform
    method = body.get("login_method")
    expected_calls = (
        [(("sms", "password", "cas")[method], _IDS["homeOrg"])]
        if method in (0, 2) and not body.get("inherit_login_method")
        else []
    )
    assert readiness.calls == expected_calls


@pytest.mark.parametrize(
    ("role", "scopes"),
    [("user", ["*"]), ("dept_admin", ["*"]), ("admin", []), ("admin", ["admin:users"])],
)
async def test_role_and_signed_ceiling_both_required(
    sessions: _Environment, role: str, scopes: list[str]
) -> None:
    _write(sessions, "UPDATE users SET role=? WHERE id=?", (role, _IDS["homeMember"]))
    token = _signed(await _login(sessions), scopes=scopes)
    calls = sessions.writer.calls
    before = sessions.path.read_bytes()
    with pytest.raises(MossworkError) as denied:
        await _service(sessions, _Readiness()).set_method(token, "password")
    assert denied.value.status == 403
    assert sessions.writer.calls == calls
    assert sessions.path.read_bytes() == before


@pytest.mark.parametrize("result", [False, True, 0, {}, "ready"])
async def test_malformed_readiness_never_writes(sessions: _Environment, result: object) -> None:
    token = (await _login(sessions)).access_token
    readiness = _Readiness()
    readiness.result = result
    with pytest.raises(MossworkError) as refused:
        await _service(sessions, readiness).set_method(token, "sms")
    assert refused.value.status == 503
    assert _policy(sessions) is None


async def test_actual_unavailable_channel_refusal_is_preserved(sessions: _Environment) -> None:
    token = (await _login(sessions)).access_token
    readiness = _Readiness()

    def unavailable() -> None:
        raise MossworkError.localized(ErrorCode.IDENTITY_REQUEST_INVALID, status=400)

    readiness.on_call = unavailable
    with pytest.raises(MossworkError) as refused:
        await _service(sessions, readiness).set_method(token, "cas")
    assert refused.value.status == 400
    assert _policy(sessions) is None


async def test_actor_revoked_during_readiness_refuses_before_write(sessions: _Environment) -> None:
    token = (await _login(sessions)).access_token
    readiness = _Readiness()
    readiness.on_call = lambda: _write(
        sessions, "UPDATE users SET status='disabled' WHERE id=?", (_IDS["homeMember"],)
    )
    with pytest.raises(MossworkError) as denied:
        await _service(sessions, readiness).set_method(token, "sms")
    assert denied.value.status == 401
    assert _policy(sessions) is None


async def test_transaction_rechecks_actor_after_final_writer_await(sessions: _Environment) -> None:
    token = (await _login(sessions)).access_token
    sessions.writer.calls = 0

    def change_actor(call: int) -> None:
        if call == 2:
            _write(
                sessions, "UPDATE users SET membership_revision=9 WHERE id=?", (_IDS["homeMember"],)
            )

    sessions.writer.on_call = change_actor
    with pytest.raises(MossworkError) as denied:
        await _service(sessions, _Readiness()).set_method(token, "password")
    assert denied.value.status == 401
    assert _policy(sessions) is None


async def test_late_writer_loss_does_not_claim_rollback_or_retry(sessions: _Environment) -> None:
    token = (await _login(sessions)).access_token
    sessions.writer.calls = 0

    def withdraw(call: int) -> None:
        if call == 3:
            sessions.writer.is_allowed = False

    sessions.writer.on_call = withdraw
    with pytest.raises(MossworkError) as refused:
        await _service(sessions, _Readiness()).set_method(token, "password")
    assert refused.value.status == 503
    row = _policy(sessions)
    assert row is not None and json.loads(row[0])["loginMethod"] == 1
    assert sessions.writer.calls == 3


async def test_missing_original_table_is_not_initialized(sessions: _Environment) -> None:
    token = (await _login(sessions)).access_token
    _write(sessions, "DROP TABLE client_delivery_policies")
    with pytest.raises(MossworkError) as unavailable:
        await _service(sessions, _Readiness()).inherit(token)
    assert unavailable.value.status == 503
    with sqlite3.connect(sessions.path) as db:
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='client_delivery_policies'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("method", [False, 0, None, "", "SMS", " sms "])
async def test_invalid_method_never_reaches_writer(sessions: _Environment, method: Any) -> None:
    calls = sessions.writer.calls
    with pytest.raises(MossworkError) as invalid:
        await _service(sessions, _Readiness()).set_method("not-a-credential", method)
    assert invalid.value.status == 400
    assert sessions.writer.calls == calls
    assert _policy(sessions) is None


async def test_distinct_database_cannot_receive_the_verified_actor(
    sessions: _Environment, tmp_path: Path
) -> None:
    other = tmp_path / "unselected.sqlite"
    with pytest.raises(MossworkError) as unavailable:
        CanonicalOrganizationLoginPolicyService(
            other, sessions.service.snapshots, writer=sessions.writer, readiness=_Readiness()
        )
    assert unavailable.value.status == 503
    assert not other.exists()


async def test_admin_namespace_ceiling_stays_in_the_selected_organization(
    sessions: _Environment,
) -> None:
    token = _signed(await _login(sessions), scopes=["admin:*"])
    await _service(sessions, _Readiness()).inherit(token)
    assert _policy(sessions) is not None
    assert _policy(sessions, "platform") is None


async def test_broken_readiness_is_an_unavailable_dependency(sessions: _Environment) -> None:
    token = (await _login(sessions)).access_token
    readiness = _Readiness()

    def broken_provider() -> None:
        raise RuntimeError("fixture provider unavailable")

    readiness.on_call = broken_provider
    with pytest.raises(MossworkError) as unavailable:
        await _service(sessions, readiness).set_method(token, "cas")
    assert unavailable.value.status == 503
    assert _policy(sessions) is None
