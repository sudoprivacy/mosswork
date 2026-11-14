"""Real original-schema SQLite reads; immutable source and refusal controls."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from octop.infra.errors import MossworkError
from octop.infra.identity.sms_configuration import SqliteSavedSmsConfigurationReader

# Exact DDL from Moss103 platformIntegrationSettingsRepository.ts; fixture only.
_SCHEMA = """
CREATE TABLE platform_integration_settings (
    setting_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""
_SAVED = {
    "version": "original-version",
    "config": {
        "enabled": True,
        "mockDelivery": False,
        "sdkAppId": "fixture-app",
        "signName": "fixture-sign",
        "templateId": "fixture-template",
        "region": "ap-beijing",
        "templateParams": ["{code}", "{ttlMinutes}"],
        "codeTtlSec": 300,
        "resendCooldownSec": 60,
        "maxSendsPerHour": 5,
        "maxVerifyAttempts": 3,
    },
    "secretRefs": {
        "secretId": "sms.original-version.secretId",
        "secretKey": "sms.original-version.secretKey",
    },
    "updatedBy": "original-member-uuid",
    "updatedAt": 1800000000123,
}


def _put(path: Path, value: Any, key: str = "platform.v1.sms", raw: bool = False) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT OR REPLACE INTO platform_integration_settings VALUES (?,?,?,1,2)",
            (key, value if raw else json.dumps(value), "original-member-uuid"),
        )


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "original.db"
    with sqlite3.connect(path) as db:
        db.executescript(_SCHEMA)
    return path


async def test_reads_exact_saved_metadata_without_writing_or_revealing_credentials(
    database: Path,
) -> None:
    _put(database, _SAVED)
    before = database.read_bytes()
    reader = SqliteSavedSmsConfigurationReader(database)
    saved = await reader.read_saved()
    assert saved is not None
    assert saved.version == _SAVED["version"] and saved.updated_by == _SAVED["updatedBy"]
    assert saved.updated_at == _SAVED["updatedAt"]
    assert saved.configuration["templateParams"] == ("{code}", "{ttlMinutes}")
    assert saved.configuration["enabled"] is True and saved.configuration["mockDelivery"] is False
    assert dict(saved.secret_references) == _SAVED["secretRefs"]
    assert "original-version" not in repr(saved) and "secretId" not in repr(saved)
    await reader.assert_current(saved)
    assert database.read_bytes() == before


async def test_missing_managed_record_never_promotes_legacy_or_history_to_active(
    database: Path,
) -> None:
    _put(database, {"provider": "tencent", "sdkAppId": "legacy"}, "sudowork.sms")
    _put(database, _SAVED, "platform.history.sms.original-version")
    before = database.read_bytes()
    assert await SqliteSavedSmsConfigurationReader(database).read_saved() is None
    assert database.read_bytes() == before


async def test_configuration_and_reference_collections_are_immutable(database: Path) -> None:
    _put(database, _SAVED)
    saved = await SqliteSavedSmsConfigurationReader(database).read_saved()
    assert saved is not None
    configuration: Any = saved.configuration
    refs: Any = saved.secret_references
    with pytest.raises(TypeError):
        configuration["enabled"] = False
    with pytest.raises(TypeError):
        refs["secretKey"] = "foreign-reference"
    mutable_view: Any = saved
    with pytest.raises(FrozenInstanceError):
        mutable_view.version = "new-version"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", None),
        ("config", []),
        ("config", {"enabled": 1}),
        ("config", {"secretId": "forbidden-material"}),
        ("config", {"resendCooldownSec": True}),
        ("config", {"codeTtlSec": 1.5}),
        ("config", {"codeTtlSec": 2**53}),
        ("config", {"templateParams": ["{code}", 7]}),
        ("config", {"region": None}),
        ("secretRefs", []),
        ("secretRefs", {"secretKey": 7}),
        ("secretRefs", {"secretKey": "sms.new-version.secretKey"}),
        ("secretRefs", {"secretKey": "sudorouter.original-version.apiToken"}),
        ("secretRefs", {"apiToken": "sms.original-version.apiToken"}),
        ("updatedBy", 1),
        ("updatedAt", True),
        ("updatedAt", -1),
    ],
)
async def test_malformed_or_foreign_metadata_refuses_without_changes(
    database: Path, field: str, value: Any
) -> None:
    damaged = {**_SAVED, field: value}
    _put(database, damaged)
    before = database.read_bytes()
    with pytest.raises(MossworkError) as refused:
        await SqliteSavedSmsConfigurationReader(database).read_saved()
    assert refused.value.status == 503
    assert database.read_bytes() == before


@pytest.mark.parametrize("raw", ["{bad", "[]", "null", '{"version":NaN}', '{"version":Infinity}'])
async def test_non_record_or_invalid_json_remains_unavailable(database: Path, raw: str) -> None:
    _put(database, raw, raw=True)
    before = database.read_bytes()
    with pytest.raises(MossworkError) as refused:
        await SqliteSavedSmsConfigurationReader(database).read_saved()
    assert refused.value.status == 503 and database.read_bytes() == before


@pytest.mark.parametrize(
    "change", ["withdraw", "new_version", "same_version_ref_change", "configuration"]
)
async def test_revalidation_retains_original_version_instead_of_loading_newer_metadata(
    database: Path, change: str
) -> None:
    _put(database, _SAVED)
    reader = SqliteSavedSmsConfigurationReader(database)
    saved = await reader.read_saved()
    assert saved is not None
    if change == "withdraw":
        with sqlite3.connect(database) as db:
            db.execute(
                "DELETE FROM platform_integration_settings WHERE setting_key='platform.v1.sms'"
            )
    else:
        value = json.loads(json.dumps(_SAVED))
        if change == "new_version":
            value["version"] = "new-version"
            value["secretRefs"] = {}
        elif change == "same_version_ref_change":
            value["secretRefs"]["secretKey"] = "sms.original-version.other-original-reference"
        else:
            value["config"]["region"] = "ap-shanghai"
        _put(database, value)
    before = database.read_bytes()
    with pytest.raises(MossworkError) as refused:
        await reader.assert_current(saved)
    assert refused.value.status == 503 and database.read_bytes() == before


async def test_other_reader_or_database_does_not_inherit_the_metadata_custody(
    database: Path, tmp_path: Path
) -> None:
    _put(database, _SAVED)
    reader = SqliteSavedSmsConfigurationReader(database)
    saved = await reader.read_saved()
    assert saved is not None
    for other in [
        SqliteSavedSmsConfigurationReader(database),
        SqliteSavedSmsConfigurationReader(tmp_path / "missing.db"),
    ]:
        with pytest.raises(MossworkError) as refused:
            await other.assert_current(saved)
        assert refused.value.status == 503
    assert not (tmp_path / "missing.db").exists()


async def test_modified_saved_view_cannot_borrow_revalidation_from_original(database: Path) -> None:
    _put(database, _SAVED)
    reader = SqliteSavedSmsConfigurationReader(database)
    saved = await reader.read_saved()
    assert saved is not None
    modified = replace(saved, secret_references={"secretKey": "foreign-reference"})
    with pytest.raises(MossworkError) as refused:
        await reader.assert_current(modified)
    assert refused.value.status == 503


@pytest.mark.parametrize("missing", ["database", "schema"])
async def test_missing_original_storage_is_never_created_or_initialized(
    tmp_path: Path, missing: str
) -> None:
    path = tmp_path / "missing.db"
    if missing == "schema":
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE untouched (value TEXT)")
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(MossworkError) as refused:
        await SqliteSavedSmsConfigurationReader(path).read_saved()
    assert refused.value.status == 503
    assert (path.read_bytes() if path.exists() else None) == before
