"""Original Node SQLite/TS oracle and actual canonical human profile lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from tests.support.app import mosswork_client, write_mosswork_config
from tests.unit.identity.test_human_sessions import (
    _IDS,
    _SECRET,
    _claims,
    _Environment,
    _login,
    _set_policy,
    _write,
)
from tests.unit.identity.test_human_sessions import legacy_db as legacy_db
from tests.unit.identity.test_human_sessions import sessions as sessions

from octop.api.app import build_app
from octop.infra.errors import MossworkError
from octop.infra.identity.canonical import CanonicalSnapshotService, SqliteCanonicalSnapshotStore
from octop.infra.identity.human_tokens import CanonicalHumanTokenVerifier
from octop.infra.identity.password_auth import (
    BcryptLegacyVerifier,
    CanonicalPasswordAuthService,
    _verify_password,
)
from octop.infra.identity.self_profile import CanonicalSelfProfileService

_ORACLE = json.loads(
    (Path(__file__).parent / "fixtures/self_profile_canonical_legacy.json").read_text(
        encoding="utf-8"
    )
)
_OLD = _ORACLE["oldPassword"]
_NEW = "ChangedPass123"


@pytest.fixture
def profiles(sessions: _Environment) -> CanonicalSelfProfileService:
    with sqlite3.connect(sessions.path) as db:
        db.executescript(_ORACLE["extension"])
        db.execute(
            "UPDATE users SET display_name='Original',password_hash=?,password_updated_at=NULL,local_auth=0,local_execution_allowed=0",
            (_ORACLE["passwordHash"],),
        )
    return CanonicalSelfProfileService(
        sessions.path,
        sessions.service.snapshots,
        writer=sessions.writer,
        clock=lambda: sessions.now[0],
    )


def _user(environment: _Environment, member: str | None = None) -> dict[str, Any]:
    with sqlite3.connect(environment.path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM users WHERE id=?", (member or _IDS["homeMember"],)
        ).fetchone()
        assert row is not None
        return dict(row)


def _identities(environment: _Environment) -> list[tuple[Any, ...]]:
    with sqlite3.connect(environment.path) as db:
        return db.execute(
            "SELECT org_id,user_id,provider,issuer,normalized_subject,metadata_json FROM user_auth_identities ORDER BY user_id"
        ).fetchall()


def _passwords(environment: _Environment) -> CanonicalPasswordAuthService:
    return CanonicalPasswordAuthService(
        environment.path, environment.policy, writer=environment.writer, issuer=environment.service
    )


def test_original_node_sqlite_source_fixture_provenance() -> None:
    assert _ORACLE["sourceCommit"] == "d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d"
    assert (
        _ORACLE["sourceHashes"]["src/server/auth/service.ts"]
        == "4670964f241ed457d3104c6b2b92abe020dea37b6cff393e9aab797309be71fa"
    )
    assert len(_ORACLE["nicknameCases"]) == 8 and len(_ORACLE["passwordCases"]) == 17


@pytest.mark.parametrize("case", _ORACLE["nicknameCases"], ids=lambda c: c["name"])
async def test_nickname_matches_original_js_utf16_trim_and_actual_sqlite(
    sessions: _Environment, profiles: CanonicalSelfProfileService, case: dict[str, Any]
) -> None:
    pair = await _login(sessions)
    before = _user(sessions)
    company = _user(sessions, _IDS["companyMember"])
    if case["status"] != 200:
        with pytest.raises(MossworkError) as error:
            await profiles.update_nickname(pair.access_token, case["nickname"])
        assert error.value.status == case["status"]
        assert _user(sessions) == before
    else:
        await profiles.update_nickname(pair.access_token, case["nickname"])
    profile = await profiles.profile(pair.access_token)
    assert profile.nickname == case["profile"]["nickname"]
    assert profile.login_name == before["name"] == "home"
    assert profile.account_id == _IDS["account"] and profile.member_id == _IDS["homeMember"]
    assert _user(sessions, _IDS["companyMember"]) == company
    assert {k: v for k, v in _user(sessions).items() if k != "display_name"} == {
        k: v for k, v in before.items() if k != "display_name"
    }
    assert _identities(sessions) == []


@pytest.mark.parametrize("case", _ORACLE["passwordCases"], ids=lambda c: c["name"])
async def test_password_change_matches_original_real_bcrypt_scrypt_and_js_rules(
    sessions: _Environment, profiles: CanonicalSelfProfileService, case: dict[str, Any]
) -> None:
    _write(
        sessions,
        "UPDATE users SET password_hash=? WHERE id=?",
        (case["oldHash"], _IDS["homeMember"]),
    )
    pair = await _login(sessions)
    before, company = _user(sessions), _user(sessions, _IDS["companyMember"])
    if case["status"] != 200:
        with pytest.raises(MossworkError) as error:
            await profiles.change_password(
                pair.access_token, case["oldPassword"], case["newPassword"]
            )
        assert error.value.status == case["status"]
        assert _user(sessions) == before and _identities(sessions) == []
    else:
        await profiles.change_password(pair.access_token, case["oldPassword"], case["newPassword"])
        row = _user(sessions)
        assert row["password_hash"].startswith("scrypt$")
        assert _verify_password(
            case["newPassword"], row["password_hash"], BcryptLegacyVerifier()
        ) == (True, None)
        # Opposite direction: consume hash actually generated by original TS.
        assert _verify_password(case["newPassword"], case["row"]["password_hash"], None) == (
            True,
            None,
        )
        assert (
            row["password_updated_at"]
            == case["row"]["password_updated_at"]
            == _ORACLE["now"] * 1000
        )
        assert row["local_auth"] == 1 and row["local_execution_allowed"] == 0
        assert _identities(sessions) == [
            tuple(
                case["identities"][0][k]
                for k in (
                    "org_id",
                    "user_id",
                    "provider",
                    "issuer",
                    "normalized_subject",
                    "metadata_json",
                )
            )
        ]
        assert {
            k: v
            for k, v in row.items()
            if k not in {"password_hash", "password_updated_at", "local_auth"}
        } == {
            k: v
            for k, v in before.items()
            if k not in {"password_hash", "password_updated_at", "local_auth"}
        }
        await sessions.service.snapshots.snapshot(pair.access_token)
        with sqlite3.connect(sessions.path) as db:
            assert db.execute("SELECT COUNT(*) FROM revoked_tokens").fetchone()[0] == 0
    assert _user(sessions, _IDS["companyMember"]) == company


async def test_profile_is_readonly_and_only_retained_normalized_avatar_is_returned(
    sessions: _Environment, profiles: CanonicalSelfProfileService
) -> None:
    pair = await _login(sessions)
    _write(
        sessions,
        "INSERT INTO user_profile_avatars VALUES (?,?)",
        (_IDS["homeMember"], _ORACLE["avatar"]),
    )
    before = hashlib.sha256(sessions.path.read_bytes()).hexdigest()
    profile = await profiles.profile(pair.access_token)
    assert profile.avatar == _ORACLE["profile"]["avatar"]
    assert profile.can_change_password is True
    assert hashlib.sha256(sessions.path.read_bytes()).hexdigest() == before
    _write(
        sessions,
        "UPDATE users SET password_hash=NULL,display_name=NULL WHERE id=?",
        (_IDS["homeMember"],),
    )
    profile = await profiles.profile(pair.access_token)
    assert profile.nickname == "home" and profile.can_change_password is False
    _write(
        sessions,
        "UPDATE user_profile_avatars SET data_url='data:image/svg+xml,<script>unsafe</script>'",
    )
    with pytest.raises(MossworkError) as error:
        await profiles.profile(pair.access_token)
    assert error.value.status == 503 and "<script>" not in str(error.value)


async def test_actual_company_member_changes_do_not_touch_shared_account_other_member(
    sessions: _Environment, profiles: CanonicalSelfProfileService
) -> None:
    home = await _login(sessions)
    company = await sessions.service.switch_organization(home.access_token, _IDS["companyOrg"])
    original = _user(sessions)
    await profiles.update_nickname(company.access_token, "Company name")
    await profiles.change_password(company.access_token, _OLD, _NEW)
    profile = await profiles.profile(company.access_token)
    assert profile.account_id == home.account_id and profile.member_id == _IDS["companyMember"]
    assert profile.organization_id == _IDS["companyOrg"] and profile.nickname == "Company name"
    assert _user(sessions) == original
    assert _identities(sessions)[0][1] == _IDS["companyMember"]


async def test_change_preserves_original_login_refresh_and_password_timestamp_semantics(
    sessions: _Environment, profiles: CanonicalSelfProfileService
) -> None:
    passwords = _passwords(sessions)
    # Moss password auth works with the original SMS org policy.
    moss = await passwords.login_moss(username="home", password=_OLD)
    original_access = _claims(moss.access_token)
    sessions.now[0] += 40
    await profiles.change_password(moss.access_token, _OLD, _NEW, moss_only=True)
    with pytest.raises(MossworkError):
        await passwords.login_moss(username="home", password=_OLD)
    changed = await passwords.login_moss(username="home", password=_NEW)
    assert changed.member_id == moss.member_id
    await sessions.service.snapshots.snapshot(moss.access_token)
    refreshed = await sessions.service.refresh(moss.refresh_token)
    assert _claims(refreshed.access_token)["scopes"] == original_access["scopes"]
    assert _user(sessions)["password_updated_at"] > original_access["iat"] * 1000
    assert _claims(refreshed.access_token)["auth_app"] == "moss"


async def test_ordinary_token_self_change_has_no_extra_login_policy_gate_or_business_scope(
    sessions: _Environment, profiles: CanonicalSelfProfileService
) -> None:
    _set_policy(sessions, "password")
    normal = await _passwords(sessions).login_sudowork(username="home", password=_OLD)
    _set_policy(sessions, "sms")
    claims = _claims(normal.access_token)
    claims["scopes"] = []
    narrow = jwt.encode(claims, _SECRET, algorithm="HS256")
    await profiles.update_nickname(narrow, "Self profile")
    await profiles.change_password(narrow, _OLD, _NEW)
    assert (await profiles.profile(narrow)).nickname == "Self profile"
    with pytest.raises(MossworkError) as error:
        await profiles.change_password(narrow, _NEW, "AnotherPass123", moss_only=True)
    assert error.value.status == 403
    with pytest.raises(MossworkError):
        await sessions.service.refresh(normal.refresh_token)


async def test_existing_password_identity_is_preserved_and_no_grants_or_sessions_created(
    sessions: _Environment, profiles: CanonicalSelfProfileService
) -> None:
    pair = await _login(sessions)
    _write(
        sessions,
        "INSERT INTO user_auth_identities VALUES ('d0c89f5c-b316-40d2-b76f-97e4fd755951',?,?,'password','moss','home','{\"retained\":true}',1,1)",
        (_IDS["homeOrg"], _IDS["homeMember"]),
    )
    before = _identities(sessions)
    await profiles.change_password(pair.access_token, _OLD, _NEW)
    assert _identities(sessions) == before
    with sqlite3.connect(sessions.path) as db:
        assert db.execute("SELECT COUNT(*) FROM human_accounts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM human_account_memberships").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM revoked_tokens").fetchone()[0] == 0


@pytest.mark.parametrize(
    "change",
    [
        "account",
        "member",
        "revision",
        "role",
        "revoke",
        "expiry",
        "hash",
        "timestamp",
        "login_name",
        "grant",
    ],
)
async def test_password_preparation_await_rechecks_current_actor_and_original_hash(
    sessions: _Environment,
    profiles: CanonicalSelfProfileService,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    pair = await _login(sessions)
    prepare = profiles._prepare_password

    def mutate(*args: Any):
        result = prepare(*args)
        if change == "expiry":
            sessions.now[0] += 400
        elif change == "revoke":
            _write(
                sessions,
                "INSERT INTO revoked_tokens VALUES (?,?)",
                (_claims(pair.access_token)["jti"], _claims(pair.access_token)["exp"]),
            )
        else:
            sql = {
                "account": "UPDATE human_accounts SET status='disabled'",
                "member": "UPDATE users SET status='disabled'",
                "revision": "UPDATE users SET membership_revision=membership_revision+1",
                "role": "UPDATE users SET role='user',membership_revision=membership_revision+1",
                "hash": "UPDATE users SET password_hash='changed-by-other-writer'",
                "timestamp": "UPDATE users SET password_updated_at=123",
                "login_name": "UPDATE users SET name='renamed-by-admin'",
                "grant": "INSERT INTO moss_member_access VALUES ('"
                + _IDS["homeOrg"]
                + "','"
                + _IDS["homeMember"]
                + "',0,'fixture',99)",
            }[change]
            _write(sessions, sql)
        return result

    monkeypatch.setattr(profiles, "_prepare_password", mutate)
    with pytest.raises(MossworkError):
        await profiles.change_password(pair.access_token, _OLD, _NEW)
    assert _identities(sessions) == [] and _user(sessions)["local_auth"] == 0


@pytest.mark.parametrize("action", ["nickname", "password"])
async def test_writer_await_rechecks_same_actor_and_writer_failure_is_closed(
    sessions: _Environment, profiles: CanonicalSelfProfileService, action: str
) -> None:
    pair = await _login(sessions)
    before = _user(sessions)

    def revoke(_: int) -> None:
        _write(
            sessions,
            "UPDATE users SET membership_revision=membership_revision+1 WHERE id=?",
            (_IDS["homeMember"],),
        )

    sessions.writer.on_call = revoke
    operation = (
        profiles.update_nickname(pair.access_token, "Rejected")
        if action == "nickname"
        else profiles.change_password(pair.access_token, _OLD, _NEW)
    )
    with pytest.raises(MossworkError):
        await operation
    row = _user(sessions)
    assert (
        row["display_name"] == before["display_name"]
        and row["password_hash"] == before["password_hash"]
    )


async def test_concurrent_old_password_changes_cannot_overwrite_new_password(
    sessions: _Environment, profiles: CanonicalSelfProfileService
) -> None:
    pair = await _login(sessions)
    results = await asyncio.gather(
        profiles.change_password(pair.access_token, _OLD, "FirstPass123"),
        profiles.change_password(pair.access_token, _OLD, "SecondPass123"),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, MossworkError) for result in results) == 1
    assert len(_identities(sessions)) == 1
    row = _user(sessions)
    assert (
        sum(
            _verify_password(p, row["password_hash"], None)[0]
            for p in ["FirstPass123", "SecondPass123"]
        )
        == 1
    )


async def test_password_identity_collision_rolls_back_hash_flags_and_timestamp(
    sessions: _Environment, profiles: CanonicalSelfProfileService
) -> None:
    pair = await _login(sessions)
    _write(
        sessions,
        "INSERT INTO user_auth_identities VALUES ('e1ad036b-b3b4-40fc-92c0-f463415a4bc3',?,?,'password','moss','home','{}',1,1)",
        (_IDS["companyOrg"], _IDS["companyMember"]),
    )
    before, identities = _user(sessions), _identities(sessions)
    with pytest.raises(MossworkError) as error:
        await profiles.change_password(pair.access_token, _OLD, _NEW)
    assert error.value.status == 503
    assert _user(sessions) == before and _identities(sessions) == identities


async def test_profile_read_rechecks_revocation_after_database_await(
    sessions: _Environment, profiles: CanonicalSelfProfileService, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair = await _login(sessions)
    read = profiles._read

    def revoke_after_read(snapshot: Any):
        result = read(snapshot)
        claims = _claims(pair.access_token)
        _write(sessions, "INSERT INTO revoked_tokens VALUES (?,?)", (claims["jti"], claims["exp"]))
        return result

    monkeypatch.setattr(profiles, "_read", revoke_after_read)
    with pytest.raises(MossworkError) as error:
        await profiles.profile(pair.access_token)
    assert error.value.status == 401


async def test_writer_refusal_preserves_data_and_profile_read_remains_readonly(
    sessions: _Environment, profiles: CanonicalSelfProfileService
) -> None:
    pair = await _login(sessions)
    before = _user(sessions)
    sessions.writer.is_allowed = False
    assert (await profiles.profile(pair.access_token)).member_id == _IDS["homeMember"]
    with pytest.raises(MossworkError) as error:
        await profiles.update_nickname(pair.access_token, "Rejected")
    assert error.value.status == 503
    with pytest.raises(MossworkError):
        await profiles.change_password(pair.access_token, _OLD, _NEW)
    assert _user(sessions) == before


async def test_postcommit_revocation_refuses_response_without_claiming_rollback(
    sessions: _Environment, profiles: CanonicalSelfProfileService
) -> None:
    pair = await _login(sessions)
    start = sessions.writer.calls

    def revoke_after_commit(call: int) -> None:
        if call == start + 2:
            _write(sessions, "UPDATE human_accounts SET status='disabled'")

    sessions.writer.on_call = revoke_after_commit
    with pytest.raises(MossworkError):
        await profiles.update_nickname(pair.access_token, "Committed before revocation")
    assert _user(sessions)["display_name"] == "Committed before revocation"


async def test_profile_self_service_needs_no_moss_or_role_grant(
    sessions: _Environment, profiles: CanonicalSelfProfileService
) -> None:
    _write(sessions, "UPDATE users SET role='user' WHERE id=?", (_IDS["homeMember"],))
    pair = await _login(sessions)
    snapshot = await sessions.service.snapshots.snapshot(pair.access_token)
    assert not snapshot.moss_access.is_enabled
    await profiles.update_nickname(pair.access_token, "Own profile")
    await profiles.change_password(pair.access_token, _OLD, _NEW)
    assert (await profiles.profile(pair.access_token)).nickname == "Own profile"


async def test_missing_store_is_503_and_never_created(
    sessions: _Environment, profiles: CanonicalSelfProfileService, tmp_path: Path
) -> None:
    pair = await _login(sessions)
    missing = tmp_path / "absent-canonical.sqlite"
    verifier = CanonicalHumanTokenVerifier(
        missing,
        authority_id="retained-moss",
        secret=_SECRET,
        issuer=sessions.service._issuer,
        expected_audience=None,
        clock=lambda: sessions.now[0],
    )
    snapshots = CanonicalSnapshotService(
        SqliteCanonicalSnapshotStore(missing, authority_id="retained-moss"),
        verifier,
        clock=lambda: sessions.now[0],
    )
    absent = CanonicalSelfProfileService(
        missing, snapshots, writer=sessions.writer, clock=lambda: sessions.now[0]
    )
    with pytest.raises(MossworkError) as error:
        await absent.profile(pair.access_token)
    assert error.value.status == 503 and not missing.exists()


async def test_real_machine_refresh_and_foreign_tokens_never_update_profile(
    sessions: _Environment, profiles: CanonicalSelfProfileService
) -> None:
    from octop.infra.identity.api_key_exchange import CanonicalApiKeyExchangeService
    from octop.infra.identity.canonical_api_keys import CanonicalApiKeyService

    oracle = json.loads(
        (Path(__file__).parent / "fixtures/api_key_exchange_canonical_legacy.json").read_text(
            encoding="utf-8"
        )
    )
    case = oracle["exchangeCases"][0]
    with sqlite3.connect(sessions.path) as db:
        db.executescript(oracle["schema"])
        db.execute(
            "INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,'active',1,NULL)",
            (
                oracle["keyId"],
                _IDS["homeOrg"],
                _IDS["homeMember"],
                "fixture",
                "moss_sk_fixture",
                case["secretHash"],
                case["scopesJson"],
            ),
        )
    machine = await CanonicalApiKeyExchangeService(
        CanonicalApiKeyService(sessions.path, sessions.service.snapshots, writer=sessions.writer),
        issuer=sessions.service,
    ).exchange(case["rawKey"])
    human = await _login(sessions)
    foreign = _claims(human.access_token)
    foreign["org_id"] = _IDS["companyOrg"]
    before = _user(sessions)
    for token in [
        machine.access_token,
        human.refresh_token,
        jwt.encode(foreign, _SECRET, algorithm="HS256"),
    ]:
        with pytest.raises(MossworkError):
            await profiles.update_nickname(token, "Rejected")
        with pytest.raises(MossworkError):
            await profiles.change_password(token, _OLD, _NEW)
    assert _user(sessions) == before


async def test_thin_http_no_store_sanitized_body_exact_injection_and_moss_guard(
    sessions: _Environment,
    profiles: CanonicalSelfProfileService,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home = tmp_path / "product"
    write_mosswork_config(home, enable_api_docs=True, enable_dashboard=False)
    phone = await _login(sessions)
    moss = await _passwords(sessions).login_moss(username="home", password=_OLD)
    headers = {"Authorization": "Bearer " + phone.access_token}
    async with mosswork_client(home) as (_, server):
        app = build_app(server, canonical_sessions=sessions.service, canonical_profile=profiles)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            path = "/api/identity/profile"
            assert (await client.get(path)).status_code == 401
            result = await client.get(path, headers=headers)
            assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
            assert (
                result.json()["member_id"] == _IDS["homeMember"]
                and "password_hash" not in result.text
            )
            nickname = await client.post(
                path + "/nickname", headers=headers, json={"nickname": "Changed"}
            )
            assert nickname.status_code == 200 and nickname.json() == {"success": True}
            bad = await client.post(
                path + "/nickname",
                headers=headers,
                json={
                    "nickname": "X",
                    "member_id": _IDS["companyMember"],
                    "secret": "sensitive-marker",
                },
            )
            assert bad.status_code == 422 and "sensitive-marker" not in bad.text
            wrong = await client.post(
                "/api/identity/password/change",
                headers=headers,
                json={"oldPassword": "wrong-secret-marker", "newPassword": _NEW},
            )
            assert wrong.status_code == 403 and "wrong-secret-marker" not in wrong.text
            rejected = await client.post(
                "/api/identity/moss/password/change",
                headers=headers,
                json={"oldPassword": _OLD, "newPassword": _NEW},
            )
            assert rejected.status_code == 403
            changed = await client.post(
                "/api/identity/moss/password/change",
                headers={"Authorization": "Bearer " + moss.access_token},
                json={"oldPassword": _OLD, "newPassword": _NEW},
            )
            assert changed.status_code == 200 and changed.json() == {"success": True}
            assert changed.headers["cache-control"] == "no-store"
            invalid = await client.post(
                "/api/identity/password/change",
                headers=headers,
                json={"oldPassword": {"nested": "secret-marker"}, "newPassword": _NEW},
            )
            assert invalid.status_code == 422 and "secret-marker" not in invalid.text
            cleared = await client.post(path + "/avatar", headers=headers, json={"avatar": None})
            assert cleared.status_code == 200 and cleared.json() == {"avatar": None}
            operation = app.openapi()["paths"][path]["get"]
            assert operation["security"] == [{"CanonicalHumanBearer": []}]
            assert app.openapi()["paths"]["/api/identity/password/change"]["post"]["requestBody"]
            assert server.user_manager is not None and server.user_manager.count() == 0
        absent = build_app(server, canonical_sessions=sessions.service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=absent), base_url="http://testserver"
        ) as client:
            assert (await client.get(path, headers=headers)).status_code in {401, 503}
    assert (
        _OLD not in caplog.text
        and _NEW not in caplog.text
        and "wrong-secret-marker" not in caplog.text
    )
