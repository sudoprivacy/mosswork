"""Original managed-SMS compatibility, transactional loss and authority races."""

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
from tests.unit.identity.test_platform_login_policy import _super_token
from tests.unit.identity.test_sms_configuration import _SCHEMA

from octop.infra.errors import MossworkError
from octop.infra.identity.sms_configuration import SqliteSavedSmsConfigurationReader
from octop.infra.identity.sms_configuration_updates import CanonicalManagedSmsConfigurationService

_ORACLE = json.loads(
    (Path(__file__).parent / "fixtures/sms_configuration_updates_legacy.json").read_bytes()
)


class _Vault:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {
            "sms.original-version.secretId": "fixture-original-id",
            "sms.original-version.secretKey": "fixture-original-key",
        }
        self.calls: list[tuple[str, str]] = []
        self.on_call: Any = None
        self.put_result: Any = None
        self.bad_verification = False

    async def get_secret(self, namespace: str, key: str) -> Any:
        assert namespace == "moss:platform-config"
        self.calls.append(("get", key))
        if self.on_call:
            self.on_call("get", key)
        if self.bad_verification and "frozen-next-version" in key:
            return "incorrect-fixture"
        return self.values.get(key)

    async def put_secret(self, namespace: str, key: str, value: str) -> Any:
        assert namespace == "moss:platform-config"
        self.calls.append(("put", key))
        self.values[key] = value
        if self.on_call:
            self.on_call("put", key)
        return self.put_result


def _rows(environment: _Environment) -> dict[str, tuple[Any, ...]]:
    with sqlite3.connect(environment.path) as db:
        return {
            row[0]: row[1:]
            for row in db.execute(
                "SELECT * FROM platform_integration_settings ORDER BY setting_key"
            )
        }


async def _prepared(
    environment: _Environment, monkeypatch: pytest.MonkeyPatch, *, seed: bool = True
) -> tuple[CanonicalManagedSmsConfigurationService, _Vault, str]:
    environment.now[0] = _ORACLE["nowMs"] / 1000
    token = await _super_token(environment)
    with sqlite3.connect(environment.path) as db:
        db.executescript(_SCHEMA)
        if seed:
            db.execute(
                "INSERT INTO platform_integration_settings VALUES (?,?,?,7,8)",
                ("platform.v1.sms", json.dumps(_ORACLE["original"]), "original-writer"),
            )
        db.execute(
            "INSERT INTO platform_integration_settings VALUES (?,?,?,9,10)",
            ("unrelated", '{"keep":true}', "unrelated-writer"),
        )
    monkeypatch.setattr(
        "octop.infra.identity.sms_configuration_updates.uuid4", lambda: "frozen-next-version"
    )
    vault = _Vault()
    service = CanonicalManagedSmsConfigurationService(
        environment.path,
        environment.service.snapshots,
        writer=environment.writer,
        vault=vault,
        clock=lambda: environment.now[0],
    )
    return service, vault, token


async def _save(service: CanonicalManagedSmsConfigurationService, token: str, **kwargs: Any) -> Any:
    return await service.save(
        token,
        expected_version=kwargs.pop("expected_version", "original-version"),
        configuration=kwargs.pop("configuration", {}),
        secrets=kwargs.pop("secrets", {}),
    )


@pytest.mark.parametrize("case", _ORACLE["cases"], ids=lambda case: case["name"])
async def test_exact_original_managed_save_and_sqlite_history(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch, case: dict[str, Any]
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    vault.bad_verification = case.get("badVerification", False)
    before = _rows(sessions)
    if case["actualStatus"] == 200:
        result = await _save(
            service,
            token,
            configuration=case["config"],
            secrets=case.get("secrets", {}),
            expected_version=case.get("expectedVersion", "original-version"),
        )
        assert result.version == case["result"]["version"] and result.restart_required is True
        assert not hasattr(result, "active_version") and not hasattr(result, "ready")
        actual = _rows(sessions)
        for original in case["rows"]:
            row = actual[original["setting_key"]]
            expected = dict(original["value_json"])
            if original["setting_key"] == "platform.v1.sms":
                expected["updatedBy"] = _IDS["homeMember"]
            assert json.loads(row[0]) == expected
            assert row[1] == (
                _IDS["homeMember"] if original["setting_key"] != "unrelated" else "unrelated-writer"
            )
            assert row[2:] == (original["created_at"], original["updated_at"])
        saved = await SqliteSavedSmsConfigurationReader(sessions.path).read_saved()
        assert saved is not None and saved.version == result.version
    else:
        with pytest.raises(MossworkError) as refused:
            await _save(
                service,
                token,
                configuration=case["config"],
                secrets=case.get("secrets", {}),
                expected_version=case.get("expectedVersion", "original-version"),
            )
        assert refused.value.status == case["actualStatus"]
        assert _rows(sessions) == before
    assert [key for op, key in vault.calls if op == "put"] == case["stagedReferences"]
    assert {key: vault.values[key] for key in case["stagedReferences"]} == case[
        "stagedFixtureValues"
    ]


@pytest.mark.parametrize(
    ("role", "scopes"),
    [("admin", ["*"]), ("user", ["*"]), ("super_admin", ["admin:users"]), ("super_admin", [])],
)
async def test_platform_role_and_signed_ceiling_required_before_secret_access(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch, role: str, scopes: list[str]
) -> None:
    service, vault, _ = await _prepared(sessions, monkeypatch)
    _write(sessions, "UPDATE users SET role=? WHERE id=?", (role, _IDS["homeMember"]))
    token = _signed(await _login(sessions), scopes=scopes)
    before = _rows(sessions)
    with pytest.raises(MossworkError) as refused:
        await _save(service, token)
    assert refused.value.status == 403 and vault.calls == [] and _rows(sessions) == before


@pytest.mark.parametrize("operation", ["get", "put"])
async def test_revocation_while_waiting_on_vault_never_publishes(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    before = _rows(sessions)

    def revoke(op: str, key: str) -> None:
        if op == operation:
            _write(
                sessions, "UPDATE users SET membership_revision=9 WHERE id=?", (_IDS["homeMember"],)
            )

    vault.on_call = revoke
    with pytest.raises(MossworkError) as refused:
        await _save(service, token)
    assert refused.value.status == 401 and _rows(sessions) == before
    assert sum(op == "put" for op, key in vault.calls) == (1 if operation == "put" else 0)


async def test_competing_saved_record_change_during_staging_is_not_overwritten(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)

    def compete(op: str, key: str) -> None:
        if op == "put":
            value = dict(_ORACLE["original"]) | {"version": "other-writer-version"}
            _write(
                sessions,
                "UPDATE platform_integration_settings SET value_json=? WHERE setting_key='platform.v1.sms'",
                (json.dumps(value),),
            )

    vault.on_call = compete
    with pytest.raises(MossworkError):
        await _save(service, token)
    assert json.loads(_rows(sessions)["platform.v1.sms"][0])["version"] == "other-writer-version"
    assert "platform.history.sms.original-version" not in _rows(sessions)
    assert sum(op == "put" for op, key in vault.calls) == 1


@pytest.mark.parametrize("result", [False, True, 0, "saved", {}])
async def test_malformed_vault_put_completion_never_acknowledges_or_publishes(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch, result: Any
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    vault.put_result = result
    before = _rows(sessions)
    with pytest.raises(MossworkError) as refused:
        await _save(service, token)
    assert refused.value.status == 503 and _rows(sessions) == before
    assert sum(op == "put" for op, key in vault.calls) == 1


async def test_uncertain_vault_write_keeps_staged_identity_without_retry_or_delete(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    before = _rows(sessions)

    def fail_after_write(op: str, key: str) -> None:
        if op == "put":
            raise RuntimeError("fixture-original-key must never reach error details")

    vault.on_call = fail_after_write
    with pytest.raises(MossworkError) as refused:
        await _save(service, token)
    assert "fixture-original-key" not in str(refused.value)
    assert _rows(sessions) == before and sum(op == "put" for op, key in vault.calls) == 1
    assert vault.values["sms.frozen-next-version.secretId"] == "fixture-original-id"


@pytest.mark.parametrize("key", ["platform.history.sms.original-version", "platform.v1.sms"])
async def test_history_and_current_publish_are_one_sqlite_transaction(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    before = _rows(sessions)
    with sqlite3.connect(sessions.path) as db:
        db.execute(
            "CREATE TRIGGER deny_sms BEFORE INSERT ON platform_integration_settings "
            f"WHEN NEW.setting_key='{key}' BEGIN SELECT RAISE(ABORT,'fixture refusal'); END"
        )
    with pytest.raises(MossworkError) as refused:
        await _save(service, token)
    assert refused.value.status == 503 and _rows(sessions) == before
    assert len([key for key in vault.values if "frozen-next-version" in key]) == 2


async def test_last_actor_change_inside_write_window_refuses_staged_publication(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    before = _rows(sessions)
    original = service._publish

    def demote(*args: Any) -> None:
        _write(sessions, "UPDATE users SET role='admin' WHERE id=?", (_IDS["homeMember"],))
        original(*args)

    monkeypatch.setattr(service, "_publish", demote)
    with pytest.raises(MossworkError) as refused:
        await _save(service, token)
    assert refused.value.status == 401 and _rows(sessions) == before


async def test_withdrawal_after_commit_preserves_uncertain_committed_result(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    original = service._publish

    def withdraw(*args: Any) -> None:
        original(*args)
        sessions.writer.is_allowed = False

    monkeypatch.setattr(service, "_publish", withdraw)
    with pytest.raises(MossworkError) as refused:
        await _save(service, token)
    assert refused.value.status == 503
    assert json.loads(_rows(sessions)["platform.v1.sms"][0])["version"] == "frozen-next-version"
    assert "platform.history.sms.original-version" in _rows(sessions)
    assert sum(op == "put" for op, key in vault.calls) == 2


async def test_missing_managed_record_refuses_first_takeover_without_legacy_import(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch, seed=False)
    before = _rows(sessions)
    with pytest.raises(MossworkError) as refused:
        await _save(service, token, expected_version=None)
    assert refused.value.status == 503 and vault.calls == [] and _rows(sessions) == before


async def test_current_writer_refusal_happens_before_vault_access(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    before = _rows(sessions)
    sessions.writer.is_allowed = False
    with pytest.raises(MossworkError) as refused:
        await _save(service, token)
    assert refused.value.status == 503 and vault.calls == [] and _rows(sessions) == before


@pytest.mark.parametrize("value", [10**1000, float("inf"), float("nan"), True])
async def test_unsafe_numeric_input_refuses_before_secret_access(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch, value: Any
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    before = _rows(sessions)
    with pytest.raises(MossworkError) as refused:
        await _save(service, token, configuration={"codeTtlSec": value})
    assert refused.value.status == 400 and vault.calls == [] and _rows(sessions) == before


async def test_same_version_metadata_change_at_sql_boundary_is_not_lost(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    original = service._publish

    def compete(*args: Any) -> None:
        value = dict(_ORACLE["original"]) | {"updatedBy": "other-writer"}
        _write(
            sessions,
            "UPDATE platform_integration_settings SET value_json=? WHERE setting_key='platform.v1.sms'",
            (json.dumps(value),),
        )
        original(*args)

    monkeypatch.setattr(service, "_publish", compete)
    with pytest.raises(MossworkError) as refused:
        await _save(service, token)
    assert refused.value.status == 409
    assert json.loads(_rows(sessions)["platform.v1.sms"][0])["updatedBy"] == "other-writer"
    assert "platform.history.sms.original-version" not in _rows(sessions)
