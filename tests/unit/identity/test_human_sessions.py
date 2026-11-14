"""Real retained SQLite phone login, memberships, JWT rotation and logout."""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from tests.unit.identity.test_human_tokens import legacy_db as legacy_db

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity import human_sessions
from octop.infra.identity.human_sessions import CanonicalHumanSessionService
from octop.infra.identity.login_policy import SqliteLoginPolicyReader, default_scopes_for_role
from octop.infra.identity.phone_auth import (
    CanonicalPhoneAuthService,
    PhoneCodeConfig,
    SqlitePhoneCodeStore,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_TOKENS = json.loads((_FIXTURES / "human_tokens_legacy.json").read_text(encoding="utf-8"))
_PHONE = json.loads((_FIXTURES / "phone_auth_legacy.json").read_text(encoding="utf-8"))
_POLICY = json.loads((_FIXTURES / "login_policy_legacy.json").read_text(encoding="utf-8"))
_IDS = _TOKENS["ids"]
_NOW = _TOKENS["verificationTime"]
_NUMBER = "13900000001"
_SECRET = _TOKENS["dummySecret"]

# Exact sudoprivacy/moss@d0b84d4 authCenter/db.ts DDL; fixture creation only.
# Provider tokens are keyed by user_id, not access JTI. Live minted service
# tokens survive logout; only expired rows are pruned by the original method.
_LOGOUT_SCHEMA = """
CREATE TABLE oauth_provider_tokens (
    user_id TEXT PRIMARY KEY,
    token_enc TEXT NOT NULL,
    token_iv TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE minted_service_tokens (
    user_id TEXT NOT NULL,
    config_item_id INTEGER NOT NULL,
    token_enc TEXT NOT NULL,
    token_iv TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, config_item_id)
);
"""


class _Writer:
    def __init__(self) -> None:
        self.calls = 0
        self.is_allowed = True
        self.on_call: Any = None

    async def assert_current_writer(self) -> None:
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        if not self.is_allowed:
            raise RuntimeError("No canonical writer admission")


class _Sender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, phone: str, code: str) -> None:
        self.sent.append((phone, code))


@dataclass
class _Environment:
    path: Path
    service: CanonicalHumanSessionService
    sender: _Sender
    writer: _Writer
    policy: SqliteLoginPolicyReader
    now: list[float]


@pytest.fixture
def sessions(legacy_db: Path) -> _Environment:
    with sqlite3.connect(legacy_db) as db:
        db.executescript(
            _PHONE["schema"]
            + _POLICY["schema"]["policies"]
            + ";"
            + _POLICY["schema"]["profiles"]
            + _LOGOUT_SCHEMA
        )
    sender, writer, now = _Sender(), _Writer(), [float(_NOW)]
    policy = SqliteLoginPolicyReader(legacy_db, default_method="sms")
    phone = CanonicalPhoneAuthService(
        SqlitePhoneCodeStore(legacy_db),
        PhoneCodeConfig(300, 60, 5, 3),
        secret=_SECRET,
        sms_sender=sender,
        writer=writer,
        clock=lambda: now[0],
    )
    service = CanonicalHumanSessionService(
        legacy_db,
        authority_id="retained-moss",
        secret=_SECRET,
        issuer=_TOKENS["issuer"],
        phone=phone,
        policy=policy,
        writer=writer,
        access_ttl_seconds=300,
        clock=lambda: now[0],
    )
    return _Environment(legacy_db, service, sender, writer, policy, now)


async def _login(environment: _Environment):
    result = await environment.service.send_code("+86 " + _NUMBER)
    assert result.next_send_in == 60
    assert environment.sender.sent[-1][0] == _NUMBER
    return await environment.service.login(_NUMBER, environment.sender.sent[-1][1])


def _claims(token: str) -> dict[str, Any]:
    encoded = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))


def _write(environment: _Environment, sql: str, params: tuple[Any, ...] = ()) -> None:
    with sqlite3.connect(environment.path) as db:
        db.execute(sql, params)


def _set_policy(environment: _Environment, method: str) -> None:
    _write(
        environment,
        "INSERT OR REPLACE INTO client_delivery_policies VALUES ('platform', 'default', ?, 'fixture', 1, 1)",
        (json.dumps({"loginMethod": method}),),
    )


async def test_real_phone_login_company_switch_refresh_and_logout(sessions: _Environment) -> None:
    home = await _login(sessions)
    home_snapshot = await sessions.service.snapshots.snapshot(home.access_token)
    assert home.account_id == home_snapshot.account_id == _IDS["account"]
    assert home.member_id == _IDS["homeMember"]
    assert home_snapshot.personal_agent is not None
    assert home_snapshot.scopes == ("*",)
    assert home_snapshot.session_id is None
    memberships = await sessions.service.snapshots.organizations(home.access_token)
    assert {(m.organization_id, m.member_id) for m in memberships} == {
        (_IDS["homeOrg"], _IDS["homeMember"]),
        (_IDS["companyOrg"], _IDS["companyMember"]),
    }
    company = await sessions.service.switch_organization(home.access_token, _IDS["companyOrg"])
    company_snapshot = await sessions.service.snapshots.snapshot(company.access_token)
    assert company.account_id == home.account_id
    assert company.member_id == _IDS["companyMember"] != home.member_id
    assert company.organization_id == _IDS["companyOrg"]
    assert company_snapshot.personal_agent != home_snapshot.personal_agent
    assert company_snapshot.scopes == default_scopes_for_role("user")
    sessions.now[0] += 1
    refreshed = await sessions.service.refresh(company.refresh_token)
    assert refreshed.member_id == company.member_id
    assert refreshed.access_token != company.access_token
    assert (
        _claims(refreshed.refresh_token)["exp"] - _claims(refreshed.refresh_token)["iat"] == 604800
    )
    assert await sessions.service.snapshots.snapshot(refreshed.access_token)
    await sessions.service.logout(refreshed.access_token, refreshed.refresh_token)
    with pytest.raises(MossworkError) as access_denied:
        await sessions.service.snapshots.snapshot(refreshed.access_token)
    assert access_denied.value.status == 401
    with pytest.raises(MossworkError) as refresh_denied:
        await sessions.service.refresh(refreshed.refresh_token)
    assert refresh_denied.value.status == 401
    # Logout is for the supplied pair, not all historical signed credentials.
    assert await sessions.service.snapshots.snapshot(home.access_token)
    with sqlite3.connect(sessions.path) as db:
        assert db.execute(
            "SELECT last_login_at FROM users WHERE id = ?", (home.member_id,)
        ).fetchone() == (_NOW * 1000,)


async def test_issued_access_is_exact_retained_typescript_bytes(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = next(case for case in _TOKENS["cases"] if case["name"] == "sms_home")
    ids = iter([UUID(original["original"]["jti"]), UUID("12345678-1234-4234-8234-123456789abc")])
    monkeypatch.setattr(human_sessions, "uuid4", lambda: next(ids))
    issued = await _login(sessions)
    assert issued.access_token == original["token"]
    assert issued.access_token not in repr(issued)
    assert issued.refresh_token not in repr(issued)


@pytest.mark.parametrize("kind", ["access", "refresh"])
async def test_other_token_type_cannot_enter_authentication_path(
    sessions: _Environment, kind: str
) -> None:
    pair = await _login(sessions)
    with pytest.raises(MossworkError) as raised:
        if kind == "access":
            await sessions.service.refresh(pair.access_token)
        else:
            await sessions.service.snapshots.snapshot(pair.refresh_token)
    assert raised.value.status == 401


async def test_refresh_reads_current_role_instead_of_expanding_consumer_scope(
    sessions: _Environment,
) -> None:
    pair = await _login(sessions)
    _write(
        sessions,
        "UPDATE users SET role = 'user', membership_revision = 1 WHERE id = ?",
        (_IDS["homeMember"],),
    )
    with pytest.raises(MossworkError):
        await sessions.service.snapshots.snapshot(pair.access_token)
    refreshed = await sessions.service.refresh(pair.refresh_token)
    snapshot = await sessions.service.snapshots.snapshot(refreshed.access_token)
    assert snapshot.member.role == "user"
    assert snapshot.scopes == default_scopes_for_role("user")
    assert snapshot.personal_agent is None


@pytest.mark.parametrize("operation", ["login", "refresh", "switch"])
async def test_current_policy_blocks_phone_issuer(sessions: _Environment, operation: str) -> None:
    if operation == "login":
        await sessions.service.send_code(_NUMBER)
        pair = None
    else:
        pair = await _login(sessions)
    _set_policy(sessions, "password")
    with pytest.raises(MossworkError) as raised:
        if operation == "login":
            await sessions.service.login(_NUMBER, sessions.sender.sent[-1][1])
        elif operation == "refresh":
            await sessions.service.refresh(pair.refresh_token)
        else:
            await sessions.service.switch_organization(pair.access_token, _IDS["companyOrg"])
    assert raised.value.status == 403


async def test_policy_change_at_last_writer_await_cannot_issue_token(
    sessions: _Environment,
) -> None:
    pair = await _login(sessions)
    sessions.writer.calls = 0

    def change_policy(call: int) -> None:
        if call == 2:
            _set_policy(sessions, "password")

    sessions.writer.on_call = change_policy
    with pytest.raises(MossworkError) as raised:
        await sessions.service.switch_organization(pair.access_token, _IDS["companyOrg"])
    assert raised.value.status == 403


@pytest.mark.parametrize("operation", ["refresh", "switch"])
async def test_original_credential_revoked_during_await_is_not_replaced(
    sessions: _Environment, operation: str
) -> None:
    pair = await _login(sessions)
    token = pair.refresh_token if operation == "refresh" else pair.access_token
    claims = _claims(token)
    sessions.writer.calls = 0

    def revoke(call: int) -> None:
        if call == 2:
            _write(
                sessions, "INSERT INTO revoked_tokens VALUES (?, ?)", (claims["jti"], claims["exp"])
            )

    sessions.writer.on_call = revoke
    with pytest.raises(MossworkError) as raised:
        if operation == "refresh":
            await sessions.service.refresh(token)
        else:
            await sessions.service.switch_organization(token, _IDS["companyOrg"])
    assert raised.value.status == 401


@pytest.mark.parametrize("operation", ["send", "login", "refresh", "switch", "logout"])
async def test_writer_denial_prevents_canonical_mutation(
    sessions: _Environment, operation: str
) -> None:
    pair = await _login(sessions)
    before = sessions.path.read_bytes()
    sent = list(sessions.sender.sent)
    sessions.writer.is_allowed = False
    with pytest.raises(MossworkError) as raised:
        if operation == "send":
            await sessions.service.send_code(_NUMBER)
        elif operation == "login":
            await sessions.service.login(_NUMBER, "123456")
        elif operation == "refresh":
            await sessions.service.refresh(pair.refresh_token)
        elif operation == "switch":
            await sessions.service.switch_organization(pair.access_token, _IDS["companyOrg"])
        else:
            await sessions.service.logout(pair.access_token, pair.refresh_token)
    assert raised.value.status == 503
    assert sessions.path.read_bytes() == before
    assert sessions.sender.sent == sent


@pytest.mark.parametrize("column,value", [("status", "disabled"), ("role", "user")])
async def test_actor_changes_during_switch_are_rechecked(
    sessions: _Environment, column: str, value: str
) -> None:
    pair = await _login(sessions)
    sessions.writer.calls = 0

    def mutate(call: int) -> None:
        if call == 2:
            _write(
                sessions, f"UPDATE users SET {column} = ? WHERE id = ?", (value, _IDS["homeMember"])
            )

    sessions.writer.on_call = mutate
    with pytest.raises(MossworkError) as raised:
        await sessions.service.switch_organization(pair.access_token, _IDS["companyOrg"])
    assert raised.value.status in (401, 403)


async def test_foreign_account_org_cannot_be_selected_by_admin(sessions: _Environment) -> None:
    pair = await _login(sessions)
    with sqlite3.connect(sessions.path) as db:
        db.execute("INSERT INTO organizations VALUES ('foreign-org', 'Foreign', 1)")
        db.execute(
            "INSERT INTO human_accounts VALUES ('foreign-account', '13900000002', 'active', 1)"
        )
        db.execute(
            "INSERT INTO users (id,org_id,email,name,role,status,created_at) VALUES ('foreign-member','foreign-org','foreign@example.test','foreign','admin','active',1)"
        )
        db.execute(
            "INSERT INTO human_account_memberships VALUES ('foreign-account','foreign-org','foreign-member',1)"
        )
    with pytest.raises(MossworkError) as raised:
        await sessions.service.switch_organization(pair.access_token, "foreign-org")
    assert raised.value.status == 403
    assert len(await sessions.service.snapshots.organizations(pair.access_token)) == 2


async def test_logout_uses_original_provider_and_minted_token_lifecycle(
    sessions: _Environment,
) -> None:
    pair = await _login(sessions)
    with sqlite3.connect(sessions.path) as db:
        db.executemany(
            "INSERT INTO oauth_provider_tokens VALUES (?, 'dummy-enc', 'dummy-iv', ?)",
            [
                (_IDS["homeMember"], _NOW + 300),
                (_IDS["companyMember"], _NOW + 300),
                ("expired-fixture-member", _NOW - 1),
            ],
        )
        db.executemany(
            "INSERT INTO minted_service_tokens VALUES (?, ?, 'dummy-enc', 'dummy-iv', ?)",
            [
                (_IDS["homeMember"], 1, _NOW + 300),
                (_IDS["homeMember"], 2, _NOW - 1),
                (_IDS["homeMember"], 3, _NOW),
            ],
        )
    await sessions.service.logout(pair.access_token, pair.refresh_token)
    with sqlite3.connect(sessions.path) as db:
        assert db.execute("SELECT user_id FROM oauth_provider_tokens").fetchall() == [
            (_IDS["companyMember"],)
        ]
        assert db.execute(
            "SELECT config_item_id FROM minted_service_tokens ORDER BY config_item_id"
        ).fetchall() == [(1,), (3,)]
        assert len(db.execute("SELECT * FROM revoked_tokens").fetchall()) == 2


async def test_disabled_account_can_logout_its_valid_signed_pair(sessions: _Environment) -> None:
    pair = await _login(sessions)
    _write(
        sessions, "UPDATE human_accounts SET status = 'disabled' WHERE id = ?", (_IDS["account"],)
    )
    await sessions.service.logout(pair.access_token, pair.refresh_token)
    with sqlite3.connect(sessions.path) as db:
        assert len(db.execute("SELECT * FROM revoked_tokens").fetchall()) == 2


@pytest.mark.parametrize(
    "case_name", ["password_human", "control_credential", "wrong_account_claim"]
)
async def test_non_phone_or_invalid_identity_cannot_switch(
    sessions: _Environment, case_name: str
) -> None:
    token = next(case["token"] for case in _TOKENS["cases"] if case["name"] == case_name)
    with pytest.raises(MossworkError) as raised:
        await sessions.service.switch_organization(token, _IDS["companyOrg"])
    assert raised.value.status in (401, 403)


async def test_wrong_code_does_not_update_last_login(sessions: _Environment) -> None:
    await sessions.service.send_code(_NUMBER)
    with pytest.raises(MossworkError) as raised:
        await sessions.service.login(_NUMBER, "incorrect")
    assert raised.value.code == ErrorCode.AUTH_FAILED
    with sqlite3.connect(sessions.path) as db:
        assert db.execute(
            "SELECT last_login_at FROM users WHERE id = ?", (_IDS["homeMember"],)
        ).fetchone() == (None,)


async def test_final_policy_uses_canonical_transaction_despite_separate_reader_path(
    sessions: _Environment, tmp_path: Path
) -> None:
    pair = await _login(sessions)
    alternate = tmp_path / "alternate-policy-only.sqlite"
    with sqlite3.connect(alternate) as db:
        db.executescript(_POLICY["schema"]["policies"] + ";" + _POLICY["schema"]["profiles"])
    # Even a trusted composition's mistaken read path cannot replace current
    # policy in the real canonical signing transaction.
    sessions.service._policy = SqliteLoginPolicyReader(alternate, default_method="sms")
    _set_policy(sessions, "password")
    with pytest.raises(MossworkError) as raised:
        await sessions.service.refresh(pair.refresh_token)
    assert raised.value.status == 403


def test_transaction_policy_guard_refuses_unguarded_connection(sessions: _Environment) -> None:
    with sqlite3.connect(sessions.path) as db:
        assert not db.in_transaction
        with pytest.raises(MossworkError) as raised:
            sessions.policy.require_sms_in_transaction(db, _IDS["homeOrg"])
        assert raised.value.status == 503


@pytest.mark.parametrize("change", ["disabled_account", "removed_target", "expired_credential"])
async def test_current_account_target_and_expiry_are_checked_at_signing(
    sessions: _Environment, change: str
) -> None:
    pair = await _login(sessions)
    sessions.writer.calls = 0

    def mutate(call: int) -> None:
        if call != 2:
            return
        if change == "disabled_account":
            _write(
                sessions,
                "UPDATE human_accounts SET status = 'disabled' WHERE id = ?",
                (_IDS["account"],),
            )
        elif change == "removed_target":
            _write(
                sessions,
                "DELETE FROM human_account_memberships WHERE org_id = ?",
                (_IDS["companyOrg"],),
            )
        else:
            sessions.now[0] += 300

    sessions.writer.on_call = mutate
    with pytest.raises(MossworkError) as raised:
        await sessions.service.switch_organization(pair.access_token, _IDS["companyOrg"])
    assert raised.value.status in (401, 403)


async def test_logout_preserves_original_independent_bearer_revocation(
    sessions: _Environment,
) -> None:
    home = await _login(sessions)
    with sqlite3.connect(sessions.path) as db:
        db.execute("INSERT INTO organizations VALUES ('foreign-org', 'Foreign', 1)")
        db.execute(
            "INSERT INTO human_accounts VALUES ('foreign-account', '13900000002', 'active', 1)"
        )
        db.execute(
            "INSERT INTO users (id,org_id,email,name,role,status,created_at,phone) VALUES ('foreign-member','foreign-org','foreign@example.test','foreign','admin','active',1,'13900000002')"
        )
        db.execute(
            "INSERT INTO human_account_memberships VALUES ('foreign-account','foreign-org','foreign-member',1)"
        )
    await sessions.service.send_code("13900000002")
    foreign = await sessions.service.login("13900000002", sessions.sender.sent[-1][1])
    # The original method independently verifies each supplied bearer. This
    # deliberately preserves its behavior rather than inventing pair coupling.
    await sessions.service.logout(home.access_token, foreign.refresh_token)
    with pytest.raises(MossworkError):
        await sessions.service.snapshots.snapshot(home.access_token)
    with pytest.raises(MossworkError):
        await sessions.service.refresh(foreign.refresh_token)
    assert await sessions.service.snapshots.snapshot(foreign.access_token)
    assert await sessions.service.refresh(home.refresh_token)


async def test_logout_invalid_access_still_revokes_valid_refresh(sessions: _Environment) -> None:
    pair = await _login(sessions)
    await sessions.service.logout("not-a-signed-access-token", pair.refresh_token)
    with pytest.raises(MossworkError):
        await sessions.service.refresh(pair.refresh_token)
    assert await sessions.service.snapshots.snapshot(pair.access_token)


async def test_logout_invalid_bearers_are_noop_except_original_expiry_cleanup(
    sessions: _Environment,
) -> None:
    _write(sessions, "INSERT INTO revoked_tokens VALUES ('expired-fixture', ?)", (_NOW - 1,))
    assert await sessions.service.logout("invalid", "also-invalid") is None
    with sqlite3.connect(sessions.path) as db:
        assert db.execute("SELECT * FROM revoked_tokens").fetchall() == []


async def test_unregistered_phone_never_creates_or_backfills_identity(
    sessions: _Environment,
) -> None:
    await sessions.service.send_code("13900000009")
    with pytest.raises(MossworkError) as raised:
        await sessions.service.login("13900000009", sessions.sender.sent[-1][1])
    assert raised.value.status == 404
    assert raised.value.details == {"registration_required": True}
    with sqlite3.connect(sessions.path) as db:
        assert db.execute("SELECT COUNT(*) FROM human_accounts").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM users").fetchone() == (2,)
        assert db.execute("SELECT COUNT(*) FROM human_account_memberships").fetchone() == (2,)
