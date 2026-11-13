"""Independent project audit uses the original nullable-session SQL schema."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from tests.unit.db.test_retained_model_audit import canonical as canonical

from octop.infra.db.repos.retained_model_audit import ModelAuditUnavailable, RetainedModelAuditRepo
from octop.infra.db.repos.retained_ontology_project import RetainedOntologyProject


def project() -> RetainedOntologyProject:
    # Original metadata only; this fixture supplies no human/graph/writer grant.
    return RetainedOntologyProject(
        "project", "original-org", "authority", "worker", "model", ("source",), 3
    )


def rows(path: Path) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM mcp_audit_log").fetchall()


@pytest.mark.parametrize("status", ["success", "error"])
def test_project_audit_commits_null_session_and_original_actor(
    canonical: Path, status: Any
) -> None:
    assert (
        RetainedModelAuditRepo(canonical).record_project_access(
            project(), actor_id="original-human", operation="review", status=status
        )
        is None
    )
    (row,) = rows(canonical)
    assert (row["org_id"], row["mcp_server_id"], row["user_id"]) == (
        "original-org",
        "worker",
        "original-human",
    )
    assert row["session_id"] is None
    assert (row["action"], row["tool_name"], row["status"]) == (
        "ontology.review.read",
        "sudograph_context_read",
        status,
    )
    assert all(
        row[name] is None
        for name in (
            "mcp_server_name",
            "user_name",
            "request_params_json",
            "response_summary",
            "error_message",
            "ip_address",
        )
    )
    with closing(sqlite3.connect(canonical)) as db:
        assert db.execute("SELECT original FROM retained_business").fetchall() == [
            ("do not change",)
        ]
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='sessions'").fetchall()


def test_session_audit_does_not_accept_project_inventory(canonical: Path) -> None:
    with pytest.raises(ValueError, match="original registered model binding"):
        RetainedModelAuditRepo(canonical).record_access(
            project(),
            actor_id="original-human",
            operation="review",
            status="error",  # type: ignore[arg-type]
        )
    assert rows(canonical) == []


@pytest.mark.parametrize(
    "override", [{"actor_id": ""}, {"operation": "sudograph_model_apply"}, {"status": "ack"}]
)
def test_project_audit_refuses_invalid_metadata_without_writing(
    canonical: Path, override: dict[str, Any]
) -> None:
    parameters = {
        "actor_id": "original-human",
        "operation": "review",
        "status": "error",
        **override,
    }
    with pytest.raises(ValueError):
        RetainedModelAuditRepo(canonical).record_project_access(project(), **parameters)
    assert rows(canonical) == []


def test_project_audit_requires_original_worker(canonical: Path) -> None:
    with closing(sqlite3.connect(canonical)) as db, db:
        db.execute("DELETE FROM mcp_servers")
    with pytest.raises(ModelAuditUnavailable):
        RetainedModelAuditRepo(canonical).record_project_access(
            project(), actor_id="original-human", operation="review", status="error"
        )
    assert rows(canonical) == []


def test_project_audit_does_not_create_missing_database(tmp_path: Path) -> None:
    missing = tmp_path / "private-missing.sqlite"
    with pytest.raises(ModelAuditUnavailable) as error:
        RetainedModelAuditRepo(missing).record_project_access(
            project(), actor_id="original-human", operation="review", status="error"
        )
    assert "private-missing" not in str(error.value)
    assert not missing.exists()


def test_project_audit_does_not_initialize_missing_schema(tmp_path: Path) -> None:
    empty = tmp_path / "empty.sqlite"
    with closing(sqlite3.connect(empty)):
        pass
    before = empty.read_bytes()
    with pytest.raises(ModelAuditUnavailable):
        RetainedModelAuditRepo(empty).record_project_access(
            project(), actor_id="original-human", operation="review", status="error"
        )
    assert empty.read_bytes() == before


def test_project_audit_insert_failure_preserves_sanitized_error(canonical: Path) -> None:
    with closing(sqlite3.connect(canonical)) as db:
        db.execute(
            "CREATE TRIGGER refuse_project_audit BEFORE INSERT ON mcp_audit_log BEGIN "
            "SELECT RAISE(ABORT,'PRIVATE-PATH-CREDENTIAL'); END"
        )
    with pytest.raises(ModelAuditUnavailable) as error:
        RetainedModelAuditRepo(canonical).record_project_access(
            project(), actor_id="original-human", operation="review", status="success"
        )
    assert "PRIVATE" not in str(error.value)
    assert rows(canonical) == []


def test_project_audit_has_no_payload_argument(canonical: Path) -> None:
    with pytest.raises(TypeError):
        RetainedModelAuditRepo(canonical).record_project_access(
            project(),
            actor_id="original-human",
            operation="review",
            status="success",
            html="private document",  # type: ignore[call-arg]
        )
    assert rows(canonical) == []
