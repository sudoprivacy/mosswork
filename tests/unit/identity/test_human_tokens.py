"""Verify exact original TypeScript JWT fixtures against real canonical SQLite."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from tests.unit.identity.test_canonical import _FROZEN_SCHEMA

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity import canonical_jwt
from octop.infra.identity.canonical import (
    CanonicalSnapshotService,
    SqliteCanonicalSnapshotStore,
    VerifiedHumanPrincipal,
    has_signed_scope,
)
from octop.infra.identity.human_tokens import CanonicalHumanTokenVerifier

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "human_tokens_legacy.json"
_FIXTURE = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
_CASES = _FIXTURE["cases"]
_IDS = _FIXTURE["ids"]
_NOW = _FIXTURE["verificationTime"]


def _case(name: str) -> dict[str, Any]:
    return next(case for case in _CASES if case["name"] == name)


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    path = tmp_path / "original-canonical.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        # Original authCenter/db.ts also adds the verified phone column/index
        # after its base CREATE users DDL. This is fixture setup, not app migration.
        # Batch the original DDL and seed writes in this fixture's transaction.
        connection.executescript(
            "BEGIN;"
            + _FROZEN_SCHEMA
            + "ALTER TABLE users ADD COLUMN phone TEXT;"
            + "CREATE UNIQUE INDEX users_phone_uniq ON users (phone) WHERE phone IS NOT NULL;"
        )
        connection.executemany(
            "INSERT INTO organizations VALUES (?, ?, 1)",
            [(_IDS["homeOrg"], "Personal"), (_IDS["companyOrg"], "Company")],
        )
        connection.executemany(
            "INSERT INTO users (id, org_id, email, name, role, status, created_at, phone) "
            "VALUES (?, ?, ?, ?, ?, 'active', 1, ?)",
            [
                (
                    _IDS["homeMember"],
                    _IDS["homeOrg"],
                    "home@example.test",
                    "home",
                    "admin",
                    "13900000001",
                ),
                (
                    _IDS["companyMember"],
                    _IDS["companyOrg"],
                    "company@example.test",
                    "company-member",
                    "user",
                    None,
                ),
            ],
        )
        connection.execute(
            "INSERT INTO human_accounts VALUES (?, '13900000001', 'active', 1)", (_IDS["account"],)
        )
        connection.executemany(
            "INSERT INTO human_account_memberships VALUES (?, ?, ?, 1)",
            [
                (_IDS["account"], _IDS["homeOrg"], _IDS["homeMember"]),
                (_IDS["account"], _IDS["companyOrg"], _IDS["companyMember"]),
            ],
        )
        connection.execute(
            "INSERT INTO moss_member_access VALUES (?, ?, 1, ?, 2)",
            (_IDS["companyOrg"], _IDS["companyMember"], _IDS["homeMember"]),
        )
    return path


def _verifier(path: Path, *, audience: str | None = None) -> CanonicalHumanTokenVerifier:
    return CanonicalHumanTokenVerifier(
        path,
        authority_id="retained-moss",
        secret=_FIXTURE["dummySecret"],
        issuer=_FIXTURE["issuer"],
        expected_audience=audience,
        clock=lambda: _NOW,
    )


def test_fixture_was_generated_by_exact_original_typescript() -> None:
    assert _FIXTURE["sourceCommit"] == "d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d"
    assert _FIXTURE["sourcePath"] == "src/server/auth/token.ts"
    assert _FIXTURE["sourceSha256"] == (
        "825bc53f4037f7fdc90b8e46e1a4f1a73390e0070db1c6bd39f5691383f0bbb8"
    )
    assert len(_CASES) == 46
    assert "public-local-fixture-only" in _FIXTURE["dummySecret"]
    assert all(not case["humanAccepted"] for case in _CASES if not case["originalAccepted"])


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
async def test_original_typescript_tokens(legacy_db: Path, case: dict[str, Any]) -> None:
    before = legacy_db.read_bytes()
    principal = await _verifier(legacy_db, audience=case["expectedAudience"]).verify_human(
        case["token"]
    )
    if not case["humanAccepted"]:
        assert principal is None
    else:
        assert principal is not None
        original = case["original"]
        assert principal.account_id == _IDS["account"]
        assert principal.member_id == original["userId"]
        assert principal.organization_id == original["orgId"]
        assert principal.credential_role == original["role"]
        assert principal.auth_app == original["authApp"]
        assert principal.key_id == original["keyId"]
        assert principal.credential_id == original["jti"]
        assert principal.expires_at == math.ceil(original["exp"])
        assert principal.session_id is None
        assert principal.scopes == tuple(
            scope for scope in original["scopes"] if isinstance(scope, str)
        )
    assert legacy_db.read_bytes() == before


@pytest.mark.parametrize("case", _FIXTURE["scopeChecks"])
def test_signed_scope_matching_equals_original_typescript(case: dict[str, Any]) -> None:
    scopes = tuple(scope for scope in case["scopes"] if isinstance(scope, str))
    assert has_signed_scope(scopes, case["required"]) == case["allowed"]


@pytest.mark.parametrize(
    "case_name", ["admin_with_narrow_signed_scopes", "admin_with_empty_signed_scopes"]
)
async def test_admin_role_does_not_expand_signed_capabilities(
    legacy_db: Path, case_name: str
) -> None:
    service = CanonicalSnapshotService(
        SqliteCanonicalSnapshotStore(legacy_db, authority_id="retained-moss"),
        _verifier(legacy_db),
        clock=lambda: _NOW,
    )
    snapshot = await service.snapshot(_case(case_name)["token"])
    assert snapshot.member.role == "admin"
    assert snapshot.moss_access.is_enabled
    assert not has_signed_scope(snapshot.scopes, "sessions:create")
    assert not has_signed_scope(snapshot.scopes, "admin:users")
    assert has_signed_scope(snapshot.scopes, "sessions:list") == (
        case_name == "admin_with_narrow_signed_scopes"
    )
    assert isinstance(snapshot.scopes, tuple)


async def test_signed_scope_list_is_frozen_before_database_await(
    legacy_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scopes = ["sessions:list"]
    decoder = canonical_jwt._decode_object

    def decode(encoded: str) -> dict[str, Any]:
        decoded = decoder(encoded)
        if "scopes" in decoded:
            decoded["scopes"] = scopes
        return decoded

    verifier = _verifier(legacy_db)
    current_member = verifier._verify_current_member

    def mutate_during_database_work(claims: Any) -> VerifiedHumanPrincipal | None:
        scopes.append("*")
        return current_member(claims)

    monkeypatch.setattr(canonical_jwt, "_decode_object", decode)
    monkeypatch.setattr(verifier, "_verify_current_member", mutate_during_database_work)
    principal = await verifier.verify_human(_case("admin_with_narrow_signed_scopes")["token"])
    assert principal is not None
    assert scopes == ["sessions:list", "*"]
    assert principal.scopes == ("sessions:list",)
    assert not has_signed_scope(principal.scopes, "sessions:create")


async def test_real_verifier_can_drive_current_snapshot_without_new_identity(
    legacy_db: Path,
) -> None:
    before = legacy_db.read_bytes()
    service = CanonicalSnapshotService(
        SqliteCanonicalSnapshotStore(legacy_db, authority_id="retained-moss"),
        _verifier(legacy_db),
        clock=lambda: _NOW,
    )
    home = await service.snapshot(_case("sms_home")["token"])
    company = await service.snapshot(_case("sms_company_actual_member")["token"])
    assert home.account_id == company.account_id == _IDS["account"]
    assert home.member.owner_id != company.member.owner_id
    assert home.personal_agent is not None
    assert company.personal_agent is not None
    assert home.personal_agent.runtime_agent_name != company.personal_agent.runtime_agent_name
    assert legacy_db.read_bytes() == before


@pytest.mark.parametrize("target", ["account", "member", "role", "jti", "membership"])
async def test_current_authority_revocation_is_applied(legacy_db: Path, target: str) -> None:
    token = _case("sms_home")["token"]
    verifier = _verifier(legacy_db)
    principal = await verifier.verify_human(token)
    assert principal is not None
    with sqlite3.connect(legacy_db) as connection:
        if target == "account":
            connection.execute("UPDATE human_accounts SET status = 'disabled'")
        elif target == "member":
            connection.execute(
                "UPDATE users SET status = 'disabled' WHERE id = ?", (_IDS["homeMember"],)
            )
        elif target == "role":
            connection.execute("UPDATE users SET role = 'user' WHERE id = ?", (_IDS["homeMember"],))
        elif target == "jti":
            connection.execute(
                "INSERT INTO revoked_tokens VALUES (?, ?)",
                (principal.credential_id, principal.expires_at),
            )
        else:
            connection.execute(
                "DELETE FROM human_account_memberships WHERE user_id = ?", (_IDS["homeMember"],)
            )
    before = legacy_db.read_bytes()
    assert await verifier.verify_human(token) is None
    assert legacy_db.read_bytes() == before


async def test_signed_wildcard_scope_cannot_override_explicit_moss_revoke(legacy_db: Path) -> None:
    token = _case("sms_home")["token"]
    with sqlite3.connect(legacy_db) as connection:
        connection.execute(
            "INSERT INTO moss_member_access VALUES (?, ?, 0, ?, 3)",
            (_IDS["homeOrg"], _IDS["homeMember"], _IDS["homeMember"]),
        )
    verifier = _verifier(legacy_db)
    # Login proof remains valid; current Moss use is independently revoked.
    assert await verifier.verify_human(token) is not None
    service = CanonicalSnapshotService(
        SqliteCanonicalSnapshotStore(legacy_db, authority_id="retained-moss"),
        verifier,
        clock=lambda: _NOW,
    )
    snapshot = await service.snapshot(token)
    assert snapshot.moss_access.source == "explicit"
    assert not snapshot.moss_access.is_enabled
    assert snapshot.personal_agent is None


async def test_legacy_phone_proof_uses_current_real_phone(legacy_db: Path) -> None:
    verifier = _verifier(legacy_db)
    token = _case("legacy_phone_without_account_claim")["token"]
    assert await verifier.verify_human(token) is not None
    with sqlite3.connect(legacy_db) as connection:
        connection.execute(
            "UPDATE users SET phone = '13900000002' WHERE id = ?", (_IDS["homeMember"],)
        )
    assert await verifier.verify_human(token) is None


async def test_account_bearing_phone_token_still_requires_a_phone_identity(legacy_db: Path) -> None:
    with sqlite3.connect(legacy_db) as connection:
        connection.execute("UPDATE human_accounts SET phone = NULL")
    assert await _verifier(legacy_db).verify_human(_case("sms_home")["token"]) is None


async def test_missing_store_is_503_and_is_never_created(tmp_path: Path) -> None:
    path = tmp_path / "nonexistent.sqlite"
    with pytest.raises(MossworkError) as caught:
        await _verifier(path).verify_human(_case("sms_home")["token"])
    assert caught.value.status == 503
    assert not path.exists()


async def test_unknown_schema_is_unavailable_instead_of_missing_identity(tmp_path: Path) -> None:
    path = tmp_path / "integer-users.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    before = path.read_bytes()
    with pytest.raises(MossworkError) as caught:
        await _verifier(path).verify_human(_case("sms_home")["token"])
    assert caught.value.code == ErrorCode.INTERNAL_ERROR
    assert caught.value.status == 503
    assert path.read_bytes() == before


async def test_invalid_signature_does_not_open_or_create_store(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite"
    assert await _verifier(path).verify_human(_case("tampered_signature")["token"]) is None
    assert not path.exists()


async def test_all_database_reads_run_off_event_loop(
    legacy_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_loop_thread = threading.get_ident()
    observed: list[int] = []
    original = sqlite3.connect

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        observed.append(threading.get_ident())
        return original(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert await _verifier(legacy_db).verify_human(_case("sms_home")["token"]) is not None
    assert observed and all(thread != event_loop_thread for thread in observed)


@pytest.mark.parametrize("field", ["secret", "issuer", "authority_id", "expected_audience"])
def test_missing_controlled_configuration_fails_closed(tmp_path: Path, field: str) -> None:
    configuration = {
        "authority_id": "retained-moss",
        "secret": _FIXTURE["dummySecret"],
        "issuer": _FIXTURE["issuer"],
        "expected_audience": None,
    }
    configuration[field] = ""
    with pytest.raises(MossworkError) as caught:
        CanonicalHumanTokenVerifier(tmp_path / "not-opened.sqlite", **configuration)
    assert caught.value.status == 503
    assert caught.value.details == {"dependency": "canonical_authority_configuration"}
    assert not (tmp_path / "not-opened.sqlite").exists()


async def test_expiry_is_checked_again_after_database_work(legacy_db: Path) -> None:
    clock_values = iter([_NOW, _NOW + 301])
    verifier = CanonicalHumanTokenVerifier(
        legacy_db,
        authority_id="retained-moss",
        secret=_FIXTURE["dummySecret"],
        issuer=_FIXTURE["issuer"],
        expected_audience=None,
        clock=lambda: next(clock_values),
    )
    assert await verifier.verify_human(_case("sms_home")["token"]) is None
