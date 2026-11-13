"""Recover the original registered model using its actual public SQLite schema."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from tests.unit.db.test_retained_cohost import _change
from tests.unit.db.test_retained_cohost import retained as retained

from octop.infra.db.repos.retained_cohost import RetainedStoreUnavailable
from octop.infra.db.repos.retained_ontology import (
    RetainedModelInvalid,
    RetainedModelUnavailable,
    RetainedOntologyBindingRepo,
)


@pytest.fixture
def registered(retained: Path) -> Path:
    with closing(sqlite3.connect(retained)) as connection:
        connection.executescript(
            (Path(__file__).parent / "fixtures/retained_ontology_103.sql").read_text(
                encoding="utf-8"
            )
        )
        connection.execute(
            """INSERT INTO ontology_projects
                (project_id, org_id, authority_ref, worker_ref, model_ref,
                 source_refs_json, generation, created_at)
                VALUES ('project-original', 'org', 'authority', 'worker', 'model',
                '["source-a","source-b"]', 7, 1)"""
        )
        connection.execute(
            """INSERT INTO ontology_session_bindings VALUES
                ('app-original', 'project-original', 'org', 'owner', 'binding-original', 1)"""
        )
        connection.commit()
    return retained


def _read(repo: RetainedOntologyBindingRepo):
    return repo.resolve_original("app-original", organization_id="org", owner_id="owner")


def test_recovered_full_model_facts_are_original_and_reads_never_write(registered: Path) -> None:
    before = registered.read_bytes()
    repo = RetainedOntologyBindingRepo(registered)
    binding = _read(repo)
    assert binding.application_session_id == "app-original"
    assert binding.organization_id == "org" and binding.owner_id == "owner"
    assert binding.project_id == binding.resource_ref == "project-original"
    assert binding.binding_id == "binding-original"
    assert binding.authority_ref == "authority"
    assert binding.worker_ref == "worker" and binding.model_ref == "model"
    assert binding.source_refs == ("source-a", "source-b")
    assert binding.generation == 7
    assert repo.assert_current(binding) is None
    assert "owner" not in repr(binding)
    assert registered.read_bytes() == before


@pytest.mark.parametrize(
    ("session", "org", "owner"),
    [
        ("unknown", "org", "owner"),
        ("app-original", "foreign-org", "owner"),
        ("app-original", "org", "foreign-owner"),
        ("app-original' OR 1=1 --", "org", "owner"),
    ],
)
def test_foreign_selection_never_discovers_other_registered_models(
    registered: Path, session: str, org: str, owner: str
) -> None:
    with pytest.raises(RetainedModelUnavailable):
        RetainedOntologyBindingRepo(registered).resolve_original(
            session, organization_id=org, owner_id=owner
        )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE users SET status = 'disabled'",
        "UPDATE users SET status = 'locked'",
        "UPDATE users SET org_id = 'other'",
        "UPDATE sessions SET org_id = 'other'",
        "UPDATE sessions SET user_id = 'other'",
        "UPDATE sessions SET deleted_at = 1",
        "UPDATE ontology_projects SET enabled = 0",
        "DELETE FROM sessions",
        "DELETE FROM ontology_projects",
        "DELETE FROM ontology_session_bindings",
    ],
)
def test_missing_disabled_or_foreign_facts_refuse_without_ordinary_session_fallback(
    registered: Path, sql: str
) -> None:
    repo = RetainedOntologyBindingRepo(registered)
    original = _read(repo)
    _change(registered, sql)
    before = registered.read_bytes()
    with pytest.raises(RetainedModelUnavailable):
        _read(repo)
    with pytest.raises(RetainedModelUnavailable):
        repo.assert_current(original)
    assert registered.read_bytes() == before


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE ontology_session_bindings SET user_id = 'foreign-owner'",
        "UPDATE ontology_session_bindings SET org_id = 'foreign-org'",
        "UPDATE ontology_projects SET org_id = 'foreign-org'",
        "UPDATE ontology_projects SET generation = 0",
        "UPDATE ontology_projects SET generation = 1.5",
        "UPDATE ontology_projects SET generation = 9007199254740992",
        "UPDATE ontology_projects SET authority_ref = 'https://unregistered.test'",
        "UPDATE ontology_projects SET worker_ref = '/unregistered/worker'",
        "UPDATE ontology_projects SET source_refs_json = '{}'",
        "UPDATE ontology_projects SET source_refs_json = 'not JSON'",
        "UPDATE ontology_projects SET source_refs_json = '[1]'",
        'UPDATE ontology_projects SET source_refs_json = \'["source-b","source-a"]\'',
        'UPDATE ontology_projects SET source_refs_json = \'["source-a","source-a"]\'',
    ],
)
def test_inconsistent_original_model_binding_is_not_a_registered_grant(
    registered: Path, sql: str
) -> None:
    _change(registered, sql)
    with pytest.raises(RetainedModelInvalid):
        _read(RetainedOntologyBindingRepo(registered))


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("ontology_projects", "worker_ref", "other-worker"),
        ("ontology_projects", "model_ref", "other-model"),
        ("ontology_projects", "authority_ref", "other-authority"),
        ("ontology_projects", "source_refs_json", '["other-source"]'),
        ("ontology_projects", "generation", 8),
        ("ontology_session_bindings", "binding_id", "other-binding"),
    ],
)
def test_original_binding_revalidation_refuses_retargeted_model_or_generation(
    registered: Path, table: str, column: str, value: str | int
) -> None:
    repo = RetainedOntologyBindingRepo(registered)
    original = _read(repo)
    assert table in {"ontology_projects", "ontology_session_bindings"}
    assert column in {
        "worker_ref",
        "model_ref",
        "authority_ref",
        "source_refs_json",
        "generation",
        "binding_id",
    }
    _change(registered, f"UPDATE {table} SET {column} = ?", (value,))
    with pytest.raises(RetainedModelInvalid, match="binding changed"):
        repo.assert_current(original)


def test_disable_reenable_keeps_generation_fence(registered: Path) -> None:
    repo = RetainedOntologyBindingRepo(registered)
    original = _read(repo)
    _change(registered, "UPDATE ontology_projects SET enabled = 0, generation = generation + 1")
    _change(registered, "UPDATE ontology_projects SET enabled = 1, generation = generation + 1")
    assert _read(repo).generation == 9
    with pytest.raises(RetainedModelInvalid, match="binding changed"):
        repo.assert_current(original)


def test_registry_lookup_never_initializes_missing_database_or_schema(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(RetainedStoreUnavailable):
        _read(RetainedOntologyBindingRepo(missing))
    assert not missing.exists()
    with closing(sqlite3.connect(missing)) as connection:
        connection.execute("CREATE TABLE product_users (id INTEGER PRIMARY KEY)")
        connection.commit()
    before = missing.read_bytes()
    with pytest.raises(RetainedStoreUnavailable):
        _read(RetainedOntologyBindingRepo(missing))
    assert missing.read_bytes() == before
