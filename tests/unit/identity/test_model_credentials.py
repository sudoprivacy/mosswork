"""Read retained writer/reader fixtures through real canonical JWT verification."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest
from tests.unit.identity.test_human_tokens import legacy_db as legacy_db

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity import model_credentials
from octop.infra.identity.canonical import (
    CanonicalIdentitySnapshot,
    CanonicalSnapshotService,
    SqliteCanonicalSnapshotStore,
    has_signed_scope,
)
from octop.infra.identity.human_tokens import CanonicalHumanTokenVerifier
from octop.infra.identity.model_credentials import (
    OwnerModelCredentialBinding,
    OwnerModelCredentialService,
    SqliteOwnerModelCredentialStore,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_ORACLE = json.loads((_FIXTURES / "model_credentials_legacy.json").read_text(encoding="utf-8"))
_HUMAN = json.loads((_FIXTURES / "human_tokens_legacy.json").read_text(encoding="utf-8"))
_IDS = _ORACLE["ids"]
_NOW = _HUMAN["verificationTime"]
_AUTHORITY = "retained-moss"


def _case(name: str) -> dict[str, Any]:
    return next(case for case in _ORACLE["cases"] if case["name"] == name)


def _token(name: str) -> str:
    return next(case["token"] for case in _HUMAN["cases"] if case["name"] == name)


def _self_token(case: dict[str, Any]) -> str:
    return _token(
        "sms_home" if case["member"] == _IDS["homeMember"] else "sms_company_actual_member"
    )


def _load(path: Path, case: dict[str, Any]) -> None:
    """Install test-only exact DDL/rows emitted by the original TS writers."""
    with sqlite3.connect(path) as connection:
        connection.executescript(_ORACLE["ddl"]["billing"] + ";" + _ORACLE["ddl"]["organization"])
        for table in (
            "billing_external_accounts",
            "organization_model_accounts",
            "organization_model_tokens",
        ):
            for row in case["tables"][table]:
                connection.execute(
                    f"INSERT INTO {table} ({', '.join(row)}) "
                    f"VALUES ({', '.join('?' for _ in row)})",
                    tuple(row.values()),
                )
        # Billing mutations are already captured in the oracle's final rows.
        # These identity mutations exercise the independent canonical boundary.
        for change in case["changes"]:
            if change["sql"].startswith(("UPDATE users ", "UPDATE human_accounts ")):
                connection.execute(change["sql"], change["params"])


def _snapshots(path: Path) -> CanonicalSnapshotService:
    return CanonicalSnapshotService(
        SqliteCanonicalSnapshotStore(path, authority_id=_AUTHORITY),
        CanonicalHumanTokenVerifier(
            path,
            authority_id=_AUTHORITY,
            secret=_HUMAN["dummySecret"],
            issuer=_HUMAN["issuer"],
            expected_audience=None,
            clock=lambda: _NOW,
        ),
        clock=lambda: _NOW,
    )


def _service(path: Path, *, binding_path: Path | None = None) -> OwnerModelCredentialService:
    return OwnerModelCredentialService(
        _snapshots(path),
        SqliteOwnerModelCredentialStore(binding_path or path, authority_id=_AUTHORITY),
    )


def test_oracle_executes_original_sources_and_provisioning_writers() -> None:
    assert _ORACLE["sourceCommit"] == "d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d"
    assert _ORACLE["sourceHashes"] == {
        "src/server/application/commandContext.ts": (
            "44cb61bdfde2d4e2e3bf8fc838e58c8f8293f1f5e89cfeefd17c136537074537"
        ),
        "src/server/billing/billingSchema.ts": (
            "9b3dd8f313f75d846e686a00367b954cd542aa29d92458158163820d1dd0bda6"
        ),
        "src/server/billing/organizationBillingSchema.ts": (
            "fedfee6405df77436846db110b205457dfb3e59c58f22f6620917ff7ef29f482"
        ),
        "src/server/billing/billingRepository.ts": (
            "cfbd05948d0a31fc394adc25a19b6c31fb1bdab66cf4a23f866c4c95f16a6c18"
        ),
        "src/server/billing/sudorouterAccountService.ts": (
            "159d80620a69b94f40ddd3b1d54a5e93e7341cff2829353d399c66f501451d52"
        ),
        "src/server/billing/organizationBillingService.ts": (
            "4d806d9b286cb124ebdc1b2b6d642beed83ef6044882de18b7b286336b7ece29"
        ),
        "src/server/auth/service.ts": (
            "4670964f241ed457d3104c6b2b92abe020dea37b6cff393e9aab797309be71fa"
        ),
    }
    assert len(_ORACLE["cases"]) == 22
    assert _ORACLE["ids"] == _HUMAN["ids"]
    assert _ORACLE["originalReaderTightenings"] == [
        "organization account/token router_user_id equality",
        "personal provisioning/migration namespace only",
    ]
    for name in (
        "original_personal_writer_and_reader",
        "original_organization_member_writer_and_reader",
    ):
        case = _case(name)
        assert case["original"]["available"]
        assert case["original"]["reads"][0] in case["writer"]["puts"]
        assert not case["original"]["plaintextFallbackAttempted"]


@pytest.mark.parametrize("case", _ORACLE["cases"], ids=lambda case: case["name"])
async def test_original_sqlite_writer_and_reader_cases(
    legacy_db: Path, case: dict[str, Any]
) -> None:
    _load(legacy_db, case)
    before = legacy_db.read_bytes()
    if case["name"] == "foreign_organization_member":
        # The public service takes only an opaque token, never a target owner.
        # A forged cross-org server snapshot also fails the exact DB relation.
        snapshot = await _snapshots(legacy_db).snapshot(_self_token(case))
        forged = replace(
            snapshot,
            organization_id=case["org"],
            member=replace(snapshot.member, organization_id=case["org"]),
        )
        with pytest.raises(MossworkError) as caught:
            SqliteOwnerModelCredentialStore(legacy_db, authority_id=_AUTHORITY)._read(forged)
        assert caught.value.code == ErrorCode.AUTH_FAILED
    elif case["expected"] in {"unauthorized", "unavailable"}:
        with pytest.raises(MossworkError) as caught:
            await _service(legacy_db).binding(_self_token(case))
        assert caught.value.status == (401 if case["expected"] == "unauthorized" else 503)
    else:
        binding = await _service(legacy_db).binding(_self_token(case))
        if case["expected"] == "missing":
            assert binding is None
        else:
            assert binding is not None
            assert binding.authority_id == _AUTHORITY
            assert binding.provider == "sudorouter"
            assert binding.organization_id == case["org"]
            assert binding.member_id == case["member"] != _IDS["account"]
            assert binding.external_account_id == case["original"]["externalAccountId"]
            assert {
                "namespace": binding.nexus.namespace,
                "key": binding.nexus.key,
                "subject": binding.nexus.subject,
            } == case["original"]["reads"][0]
            assert binding.nexus.subject == f"org:{case['org']}"
            if case["org"] == _IDS["companyOrg"]:
                assert binding.billing_mode == "organization_shared"
                token = next(
                    row
                    for row in case["tables"]["organization_model_tokens"]
                    if row["purpose"] == "member"
                )
                assert binding.token_binding_id == token["id"]
                assert binding.external_token_id == token["router_token_id"]
            else:
                assert binding.billing_mode == "personal"
                assert binding.token_binding_id is None
                assert binding.external_token_id is None
    assert legacy_db.read_bytes() == before


@pytest.mark.parametrize(
    "name",
    ["organization_router_account_mismatch", "personal_deployment_namespace_is_not_consumed"],
)
def test_approved_consumer_tightenings_are_not_claimed_reader_parity(name: str) -> None:
    case = _case(name)
    assert case["original"]["available"]
    assert case["original"]["reads"]
    assert case["expected"] == "unavailable"
    assert case["tightening"].startswith("Migration consumer")


async def test_default_repr_redacts_reference_and_binding_cannot_mutate(legacy_db: Path) -> None:
    _load(legacy_db, _case("original_organization_member_writer_and_reader"))
    binding = await _service(legacy_db).binding(_token("sms_company_actual_member"))
    assert binding is not None
    assert binding.nexus.uri == f"nexus://{binding.nexus.namespace}/{binding.nexus.key}"
    for value in (binding.nexus.key, binding.nexus.namespace, binding.nexus.subject):
        assert value not in repr(binding.nexus)
        assert value not in repr(binding)
    with pytest.raises(FrozenInstanceError):
        binding.member_id = _IDS["account"]  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        binding.nexus.key = "another-key"  # type: ignore[misc]


@pytest.mark.parametrize("member", ["homeMember", "companyMember"])
async def test_current_explicit_moss_revoke_denies_admin_and_user(
    legacy_db: Path, member: str
) -> None:
    _load(legacy_db, _case("original_personal_writer_and_reader"))
    org = _IDS["homeOrg" if member == "homeMember" else "companyOrg"]
    with sqlite3.connect(legacy_db) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO moss_member_access VALUES (?, ?, 0, ?, 3)",
            (org, _IDS[member], _IDS["homeMember"]),
        )
    token = _token("sms_home" if member == "homeMember" else "sms_company_actual_member")
    with pytest.raises(MossworkError) as caught:
        await _service(legacy_db).binding(token)
    assert caught.value.code == ErrorCode.MOSS_ACCESS_DENIED
    assert caught.value.status == 403


@pytest.mark.parametrize("mutation", ["grant", "account", "member", "role", "jti"])
async def test_revocation_after_snapshot_is_checked_in_binding_transaction(
    legacy_db: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    _load(legacy_db, _case("original_personal_writer_and_reader"))
    store = SqliteOwnerModelCredentialStore(legacy_db, authority_id=_AUTHORITY)
    original_read = store._read

    def revoke_then_read(snapshot: CanonicalIdentitySnapshot) -> OwnerModelCredentialBinding | None:
        # Runs after all verifier/snapshot awaits, before the credential read.
        with sqlite3.connect(legacy_db) as connection:
            if mutation == "grant":
                connection.execute(
                    "INSERT INTO moss_member_access VALUES (?, ?, 0, ?, 3)",
                    (snapshot.organization_id, snapshot.member.owner_id, snapshot.member.owner_id),
                )
            elif mutation == "account":
                connection.execute("UPDATE human_accounts SET status = 'disabled'")
            elif mutation == "member":
                connection.execute("UPDATE users SET status = 'disabled'")
            elif mutation == "role":
                connection.execute("UPDATE users SET role = 'user'")
            else:
                connection.execute(
                    "INSERT INTO revoked_tokens VALUES (?, ?)",
                    (snapshot.credential_id, snapshot.credential_expires_at),
                )
        return original_read(snapshot)

    monkeypatch.setattr(store, "_read", revoke_then_read)
    with pytest.raises(MossworkError) as caught:
        await OwnerModelCredentialService(_snapshots(legacy_db), store).binding(_token("sms_home"))
    assert caught.value.code == (
        ErrorCode.MOSS_ACCESS_DENIED if mutation == "grant" else ErrorCode.AUTH_FAILED
    )


@pytest.mark.parametrize("name", ["personal_missing_account", "personal_missing_reference"])
async def test_missing_binding_never_consumes_plaintext_legacy_user_key(
    legacy_db: Path, name: str
) -> None:
    case = _case(name)
    assert case["original"]["plaintextFallbackAttempted"]
    _load(legacy_db, case)
    with sqlite3.connect(legacy_db) as connection:
        connection.execute("ALTER TABLE users ADD COLUMN sudorouter_key TEXT")
        connection.execute("UPDATE users SET sudorouter_key = 'public-legacy-key-trap'")
    assert await _service(legacy_db).binding(_token("sms_home")) is None


@pytest.mark.parametrize("schema", ["missing_file", "empty_file", "canonical_without_billing"])
async def test_unavailable_store_is_not_missing_credential_and_creates_no_database(
    legacy_db: Path, tmp_path: Path, schema: str
) -> None:
    path = tmp_path / "unavailable.sqlite"
    if schema == "empty_file":
        path.touch()
    elif schema == "canonical_without_billing":
        path = legacy_db
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(MossworkError) as caught:
        await _service(legacy_db, binding_path=path).binding(_token("sms_home"))
    assert caught.value.status == 503
    assert caught.value.details == {"dependency": "canonical_model_credential_store"}
    if before is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == before


async def test_partial_personal_schema_does_not_prove_organization_billing_absent(
    legacy_db: Path,
) -> None:
    _load(legacy_db, _case("original_personal_writer_and_reader"))
    with sqlite3.connect(legacy_db) as connection:
        connection.execute("DROP TABLE organization_model_accounts")
    with pytest.raises(MossworkError) as caught:
        await _service(legacy_db).binding(_token("sms_home"))
    assert caught.value.status == 503


@pytest.mark.parametrize(
    "name",
    [
        "foreign_org_super_admin",
        "api_key_credential",
        "internal_channel_credential",
        "control_credential",
        "refresh_token",
    ],
)
async def test_foreign_or_machine_credential_never_returns_binding(
    legacy_db: Path, name: str
) -> None:
    _load(legacy_db, _case("original_personal_writer_and_reader"))
    with pytest.raises(MossworkError) as caught:
        await _service(legacy_db).binding(_token(name))
    assert caught.value.code == ErrorCode.AUTH_FAILED


async def test_request_fields_cannot_replace_opaque_authority_token(legacy_db: Path) -> None:
    _load(legacy_db, _case("original_personal_writer_and_reader"))
    with pytest.raises(MossworkError) as caught:
        await _service(legacy_db).binding("request-body-fields-are-not-a-verifier")
    assert caught.value.code == ErrorCode.AUTH_FAILED


async def test_binding_lookup_does_not_expand_narrow_signed_capabilities(legacy_db: Path) -> None:
    _load(legacy_db, _case("original_personal_writer_and_reader"))
    snapshots = _snapshots(legacy_db)
    token = _token("admin_with_narrow_signed_scopes")
    binding = await OwnerModelCredentialService(
        snapshots, SqliteOwnerModelCredentialStore(legacy_db, authority_id=_AUTHORITY)
    ).binding(token)
    assert binding is not None
    snapshot = await snapshots.snapshot(token)
    assert not has_signed_scope(snapshot.scopes, "models:invoke")
    assert not has_signed_scope(snapshot.scopes, "sessions:create")
    # A trusted consumer must still enforce these checks before resolving secrets.


async def test_database_reads_run_in_executor(
    legacy_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _load(legacy_db, _case("original_personal_writer_and_reader"))
    foreground_thread = threading.get_ident()
    observed: list[int] = []
    original_connect = sqlite3.connect

    def record_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        observed.append(threading.get_ident())
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(model_credentials.sqlite3, "connect", record_connect)
    assert await _service(legacy_db).binding(_token("sms_home")) is not None
    assert len(observed) >= 3
    assert foreground_thread not in observed


async def test_read_failure_is_not_converted_to_absent_binding(
    legacy_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _load(legacy_db, _case("original_personal_writer_and_reader"))
    snapshot = await _snapshots(legacy_db).snapshot(_token("sms_home"))

    def unavailable(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        raise sqlite3.OperationalError("local dependency unavailable")

    monkeypatch.setattr(model_credentials.sqlite3, "connect", unavailable)
    with pytest.raises(MossworkError) as caught:
        SqliteOwnerModelCredentialStore(legacy_db, authority_id=_AUTHORITY)._read(snapshot)
    assert caught.value.status == 503


def test_postgresql_is_explicitly_unsupported() -> None:
    with pytest.raises(TypeError, match="PostgreSQL is not supported"):
        SqliteOwnerModelCredentialStore("postgresql://not-supported", authority_id=_AUTHORITY)  # type: ignore[arg-type]
