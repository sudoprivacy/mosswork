"""Original project inventory needs no session, while conveying no access grant."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from octop.infra.db.repos.retained_cohost import RetainedStoreUnavailable
from octop.infra.db.repos.retained_ontology import RetainedModelInvalid, RetainedModelUnavailable
from octop.infra.db.repos.retained_ontology_project import (
    RetainedOntologyProject,
    RetainedOntologyProjectRepo,
)


@pytest.fixture
def project_store(tmp_path: Path) -> Path:
    path = tmp_path / "original-canonical.sqlite"
    schema = (Path(__file__).parent / "fixtures/retained_ontology_103.sql").read_text(
        encoding="utf-8"
    )
    # Exact original project DDL only; there are no users, sessions, bindings,
    # or fake rows with which an execution-only reader could pass this test.
    project_schema = schema.split("CREATE TABLE IF NOT EXISTS ontology_session_bindings")[0]
    with closing(sqlite3.connect(path)) as db:
        db.executescript(project_schema)
        db.execute(
            """INSERT INTO ontology_projects
            (project_id,org_id,authority_ref,worker_ref,model_ref,source_refs_json,generation,created_at)
            VALUES ('project-original','org','authority','worker','model',?,7,1)""",
            (json.dumps(["source-a", "source-b"]),),
        )
        db.commit()
    return path


def _read(repo: RetainedOntologyProjectRepo) -> RetainedOntologyProject:
    return repo.get(organization_id="org", project_id="project-original")


def _change(path: Path, sql: str, arguments: tuple[object, ...] = ()) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute(sql, arguments)
        db.commit()


def test_project_inventory_requires_no_session_and_never_writes(project_store: Path) -> None:
    before = project_store.read_bytes()
    repo = RetainedOntologyProjectRepo(project_store)
    project = _read(repo)
    assert project == RetainedOntologyProject(
        "project-original", "org", "authority", "worker", "model", ("source-a", "source-b"), 7
    )
    assert project.resource_ref == "project-original"
    assert not hasattr(project, "application_session_id")
    assert not hasattr(project, "owner_id")
    assert repo.assert_current(project) is None
    with pytest.raises(FrozenInstanceError):
        project.generation = 8  # type: ignore[misc]
    with closing(sqlite3.connect(project_store)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"ontology_projects"}
    assert project_store.read_bytes() == before


@pytest.mark.parametrize("org,project", [("foreign-org", "project-original"), ("org", "unknown")])
def test_unknown_or_cross_org_selection_does_not_reveal_inventory(
    project_store: Path, org: str, project: str
) -> None:
    with pytest.raises(RetainedModelUnavailable):
        RetainedOntologyProjectRepo(project_store).get(organization_id=org, project_id=project)


@pytest.mark.parametrize("value", ["x' OR 1=1 --", "../project", "https://worker", "", "x" * 129])
def test_unregistered_selector_refuses_before_read(project_store: Path, value: str) -> None:
    with pytest.raises(RetainedModelInvalid):
        RetainedOntologyProjectRepo(project_store).get(organization_id="org", project_id=value)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE ontology_projects SET enabled=0",
        "DELETE FROM ontology_projects",
        "UPDATE ontology_projects SET org_id='foreign-org'",
        "UPDATE ontology_projects SET project_id='other-project'",
    ],
)
def test_disabled_deleted_or_moved_project_never_falls_back(project_store: Path, sql: str) -> None:
    repo = RetainedOntologyProjectRepo(project_store)
    original = _read(repo)
    _change(project_store, sql)
    with pytest.raises(RetainedModelUnavailable):
        repo.assert_current(original)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE ontology_projects SET authority_ref='other-authority'",
        "UPDATE ontology_projects SET worker_ref='other-worker'",
        "UPDATE ontology_projects SET model_ref='other-model'",
        "UPDATE ontology_projects SET source_refs_json='[\"source-c\"]'",
        "UPDATE ontology_projects SET generation=generation+1",
    ],
)
def test_changed_original_tuple_or_generation_refuses(project_store: Path, sql: str) -> None:
    repo = RetainedOntologyProjectRepo(project_store)
    original = _read(repo)
    _change(project_store, sql)
    with pytest.raises(RetainedModelInvalid):
        repo.assert_current(original)


def test_disable_reenable_cannot_restore_old_generation(project_store: Path) -> None:
    repo = RetainedOntologyProjectRepo(project_store)
    original = _read(repo)
    _change(project_store, "UPDATE ontology_projects SET enabled=0,generation=generation+1")
    _change(project_store, "UPDATE ontology_projects SET enabled=1,generation=generation+1")
    assert _read(repo).generation == 9
    with pytest.raises(RetainedModelInvalid):
        repo.assert_current(original)


@pytest.mark.parametrize("generation", [0, -1, 1.5, 9007199254740992])
def test_invalid_original_generation_refuses(project_store: Path, generation: object) -> None:
    _change(project_store, "UPDATE ontology_projects SET generation=?", (generation,))
    with pytest.raises(RetainedModelInvalid):
        _read(RetainedOntologyProjectRepo(project_store))


@pytest.mark.parametrize(
    "sources",
    [
        "null",
        "{}",
        '"source-a"',
        '["source-b","source-a"]',
        '["source-a","source-a"]',
        "[1]",
        "not json",
        '["../source"]',
        json.dumps([f"source-{i}" for i in range(1001)]),
    ],
)
def test_invalid_or_noncanonical_original_sources_refuse(project_store: Path, sources: str) -> None:
    _change(project_store, "UPDATE ontology_projects SET source_refs_json=?", (sources,))
    with pytest.raises(RetainedModelInvalid):
        _read(RetainedOntologyProjectRepo(project_store))


@pytest.mark.parametrize("column", ["authority_ref", "worker_ref", "model_ref"])
def test_original_path_or_credential_is_not_a_reference(project_store: Path, column: str) -> None:
    _change(project_store, f"UPDATE ontology_projects SET {column}=?", ("https://private/secret",))
    with pytest.raises(RetainedModelInvalid) as error:
        _read(RetainedOntologyProjectRepo(project_store))
    assert "private" not in str(error.value)


def test_missing_store_is_not_created_and_errors_are_sanitized(tmp_path: Path) -> None:
    missing = tmp_path / "private-database.sqlite"
    with pytest.raises(RetainedStoreUnavailable) as error:
        _read(RetainedOntologyProjectRepo(missing))
    assert not missing.exists()
    assert "private-database" not in str(error.value)


def test_missing_original_table_never_initializes_schema(tmp_path: Path) -> None:
    path = tmp_path / "uninitialized.sqlite"
    with closing(sqlite3.connect(path)):
        pass
    before = path.read_bytes()
    with pytest.raises(RetainedStoreUnavailable):
        _read(RetainedOntologyProjectRepo(path))
    assert path.read_bytes() == before


def test_relative_path_and_nonproject_expected_refuse(project_store: Path) -> None:
    with pytest.raises(ValueError):
        RetainedOntologyProjectRepo(Path("relative.sqlite"))
    with pytest.raises(RetainedModelInvalid):
        RetainedOntologyProjectRepo(project_store).assert_current(object())  # type: ignore[arg-type]
