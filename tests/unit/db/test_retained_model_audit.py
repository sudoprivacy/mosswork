"""Exercise actual retained-schema audit commits without touching a live store."""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from octop.infra.db.repos.retained_model_audit import (
    ModelAuditUnavailable,
    RetainedModelAuditRepo,
)
from octop.infra.db.repos.retained_ontology import RetainedOntologyBinding


def _binding() -> RetainedOntologyBinding:
    return RetainedOntologyBinding(
        "original-session",
        "original-org",
        "target-owner",
        "project",
        "binding",
        "authority",
        "worker",
        "model",
        ("source",),
        3,
    )


@pytest.fixture
def canonical(tmp_path: Path) -> Path:
    path = tmp_path / "original-canonical.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        # Only the referenced identity column is required for this fixture.
        # Audit table/index definitions are the unchanged published103 schema.
        connection.execute("CREATE TABLE mcp_servers (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO mcp_servers VALUES ('worker')")
        connection.executescript(
            (Path(__file__).parent / "fixtures/retained_mcp_audit_103.sql").read_text(
                encoding="utf-8"
            )
        )
        connection.execute("CREATE TABLE retained_business (original TEXT)")
        connection.execute("INSERT INTO retained_business VALUES ('do not change')")
        connection.commit()
    return path


def _rows(path: Path) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM mcp_audit_log ORDER BY created_at,id").fetchall()


@pytest.mark.parametrize("status", ["success", "error"])
@pytest.mark.parametrize(
    ("operation", "action", "tool"),
    [
        ("sudograph_context", "ontology.context.read", "sudograph_context"),
        ("sudograph_context_read", "ontology.context.read", "sudograph_context_read"),
        ("sudograph_model_contract", "ontology.model.contract", "sudograph_model_contract"),
        ("sudograph_model_apply", "ontology.model.apply", "sudograph_model_apply"),
        ("review", "ontology.review.read", "sudograph_context_read"),
    ],
)
def test_original_audit_schema_persists_only_sanitized_metadata(
    canonical: Path, operation: Any, action: str, tool: str, status: Any
) -> None:
    before = time.time_ns() // 1_000_000
    assert (
        RetainedModelAuditRepo(canonical).record_access(
            _binding(), actor_id="actual-actor", operation=operation, status=status
        )
        is None
    )
    # A fresh connection sees the committed row after the writer has closed.
    (row,) = _rows(canonical)
    assert UUID(row["id"]).version == 4
    assert row["org_id"] == "original-org" and row["session_id"] == "original-session"
    assert row["user_id"] == "actual-actor" and row["user_id"] != _binding().owner_id
    assert row["mcp_server_id"] == "worker"
    assert (row["action"], row["tool_name"], row["status"]) == (action, tool, status)
    assert before <= row["created_at"] <= time.time_ns() // 1_000_000
    for field in (
        "mcp_server_name",
        "user_name",
        "request_params_json",
        "response_summary",
        "error_message",
        "ip_address",
    ):
        assert row[field] is None
    with closing(sqlite3.connect(canonical)) as connection:
        assert connection.execute("SELECT original FROM retained_business").fetchall() == [
            ("do not change",)
        ]


def test_concurrent_audits_append_without_deduping_access_attempts(canonical: Path) -> None:
    repo = RetainedModelAuditRepo(canonical)

    def append(_: int) -> None:
        repo.record_access(_binding(), actor_id="actor", operation="review", status="error")

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(append, range(12)))
    rows = _rows(canonical)
    assert len(rows) == len({row["id"] for row in rows}) == 12


def test_missing_database_never_creates_empty_fallback(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite"
    with pytest.raises(ModelAuditUnavailable):
        RetainedModelAuditRepo(path).record_access(
            _binding(), actor_id="actor", operation="review", status="error"
        )
    assert not path.exists()


def test_missing_schema_does_not_initialize_tables(tmp_path: Path) -> None:
    path = tmp_path / "wrong.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE unrelated (id TEXT)")
    with pytest.raises(ModelAuditUnavailable):
        RetainedModelAuditRepo(path).record_access(
            _binding(), actor_id="actor", operation="review", status="error"
        )
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("unrelated",)]


def test_canonical_insert_failure_has_no_partial_row_or_raw_diagnostic(canonical: Path) -> None:
    with closing(sqlite3.connect(canonical)) as connection:
        connection.execute(
            """CREATE TRIGGER reject_audit BEFORE INSERT ON mcp_audit_log BEGIN
                SELECT RAISE(ABORT, 'PRIVATE-PATH-CREDENTIAL'); END"""
        )
    with pytest.raises(ModelAuditUnavailable) as caught:
        RetainedModelAuditRepo(canonical).record_access(
            _binding(), actor_id="actor", operation="sudograph_model_apply", status="success"
        )
    assert "PRIVATE" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert _rows(canonical) == []


def test_unknown_registered_worker_is_not_silently_written(canonical: Path) -> None:
    with closing(sqlite3.connect(canonical)) as connection:
        connection.execute("DELETE FROM mcp_servers")
        connection.commit()
    with pytest.raises(ModelAuditUnavailable):
        RetainedModelAuditRepo(canonical).record_access(
            _binding(), actor_id="actor", operation="review", status="error"
        )
    assert _rows(canonical) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"actor_id": ""},
        {"actor_id": "bad\x00actor"},
        {"operation": "unknown"},
        {"status": "delivered"},
        {"status": "unknown"},
        {"status": True},
    ],
)
def test_invalid_metadata_is_refused_before_any_write(canonical: Path, overrides: Any) -> None:
    parameters = {"actor_id": "actor", "operation": "review", "status": "error", **overrides}
    with pytest.raises(ValueError):
        RetainedModelAuditRepo(canonical).record_access(_binding(), **parameters)
    assert _rows(canonical) == []


def test_operation_content_has_no_parameter_in_audit_api(canonical: Path) -> None:
    with pytest.raises(TypeError):
        RetainedModelAuditRepo(canonical).record_access(
            _binding(),
            actor_id="actor",
            operation="review",
            status="success",
            request_params_json="private model text",  # type: ignore[call-arg]
        )
    assert _rows(canonical) == []


def test_relative_database_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="absolute"):
        RetainedModelAuditRepo(Path("relative.sqlite"))
