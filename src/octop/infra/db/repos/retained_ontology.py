"""Resolve original registered model/session facts without another registry.

Read-only counterpart of Moss server-v0.1.103 OntologyProjectRegistry. This
repository owns no registrar, access grant, connection, audit or review queue.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from octop.infra.db.repos.retained_cohost import RetainedStoreUnavailable

_REFERENCE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}")


class RetainedModelUnavailable(LookupError):
    """No enabled original project binding matches the selected session/owner."""


class RetainedModelInvalid(RuntimeError):
    """Original project/session facts are inconsistent or changed."""


def _reference(value: object) -> str:
    if not isinstance(value, str) or _REFERENCE.fullmatch(value) is None:
        raise RetainedModelInvalid("Invalid registered model binding")
    return value


def _original_id(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RetainedModelInvalid("Invalid original session identity")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class RetainedOntologyBinding:
    application_session_id: str
    organization_id: str
    owner_id: str
    project_id: str
    binding_id: str
    authority_ref: str
    worker_ref: str
    model_ref: str
    source_refs: tuple[str, ...]
    generation: int

    @property
    def resource_ref(self) -> str:
        """The original review resource is its registered project ID."""
        return self.project_id


def _binding(row: sqlite3.Row) -> RetainedOntologyBinding:
    if row["binding_id"] is None or row["project_id"] is None or row["enabled"] != 1:
        raise RetainedModelUnavailable("Original registered model is unavailable")
    if (
        row["binding_org_id"] != row["org_id"]
        or row["project_org_id"] != row["org_id"]
        or row["binding_user_id"] != row["user_id"]
        or type(row["generation"]) is not int
        or not 1 <= row["generation"] <= 2**53 - 1
    ):
        raise RetainedModelInvalid("Invalid registered model binding")
    try:
        sources: object = json.loads(row["source_refs_json"])
        if not isinstance(sources, list) or len(sources) > 1000:
            raise RetainedModelInvalid("Invalid registered model sources")
        references = tuple(_reference(source) for source in sources)
        if references != tuple(sorted(set(references))):
            raise RetainedModelInvalid("Invalid registered model sources")
    except (TypeError, ValueError):
        raise RetainedModelInvalid("Invalid registered model sources") from None
    return RetainedOntologyBinding(
        _original_id(row["session_id"]),
        _reference(row["org_id"]),
        _original_id(row["user_id"]),
        _reference(row["project_id"]),
        _reference(row["binding_id"]),
        _reference(row["authority_ref"]),
        _reference(row["worker_ref"]),
        _reference(row["model_ref"]),
        references,
        row["generation"],
    )


class RetainedOntologyBindingRepo:
    """Read only the original canonical SQLite schema, never the product pool.

    Parameters are exact original references from trusted projection, not a
    current caller or model grant. Async composition must offload this SQL and
    authorize the full model/source/resource and exact operation around I/O.
    Registry generation fences operator changes; it is not native incarnation.
    """

    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path) or not database_path.is_absolute():
            raise ValueError("Require an explicit absolute retained SQLite path")
        self._uri = database_path.as_uri() + "?mode=ro"

    def resolve_original(
        self, application_session_id: str, *, organization_id: str, owner_id: str
    ) -> RetainedOntologyBinding:
        """Recover only an existing bound project; missing/stale bindings refuse.

        No ordinary-session fallback, latest-project guess, registration, new
        session or binding publication exists. Original model context and human
        review require their distinct current grants after this lookup.
        """
        _original_id(application_session_id)
        _original_id(owner_id)
        _reference(organization_id)
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                rows = connection.execute(
                    """SELECT s.session_id, s.org_id, s.user_id,
                        b.binding_id, b.org_id AS binding_org_id,
                        b.user_id AS binding_user_id,
                        p.project_id, p.org_id AS project_org_id, p.authority_ref,
                        p.worker_ref, p.model_ref, p.source_refs_json, p.generation, p.enabled
                    FROM sessions s
                    JOIN users u ON u.id = s.user_id
                    LEFT JOIN ontology_session_bindings b ON b.session_id = s.session_id
                    LEFT JOIN ontology_projects p ON p.project_id = b.project_id
                    WHERE s.session_id = ? AND s.org_id = ? AND s.user_id = ?
                        AND u.org_id = ? AND u.status = 'active' AND s.deleted_at IS NULL
                    LIMIT 2""",
                    (application_session_id, organization_id, owner_id, organization_id),
                ).fetchall()
                if not rows:
                    raise RetainedModelUnavailable("Original registered model is unavailable")
                if len(rows) != 1:
                    raise RetainedModelInvalid("Ambiguous registered model binding")
                return _binding(rows[0])
        except (sqlite3.Error, OSError, UnicodeError):
            raise RetainedStoreUnavailable("Retained model store is unavailable") from None

    def assert_current(self, expected: RetainedOntologyBinding) -> None:
        """Re-read the complete original binding, including its registry generation."""
        if type(expected) is not RetainedOntologyBinding:
            raise RetainedModelInvalid("Require the original model binding")
        current = self.resolve_original(
            expected.application_session_id,
            organization_id=expected.organization_id,
            owner_id=expected.owner_id,
        )
        if current != expected:
            raise RetainedModelInvalid("Original registered model binding changed")
