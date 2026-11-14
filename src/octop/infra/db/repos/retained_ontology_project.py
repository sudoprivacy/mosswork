"""Read original registered projects without requiring an Agent/session binding.

Counterpart of server-v0.1.103 OntologyProjectRegistry.get. This is inventory,
not human authentication, a model grant, review admission or a registrar.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from octop.infra.db.repos.retained_cohost import RetainedStoreUnavailable
from octop.infra.db.repos.retained_ontology import RetainedModelInvalid, RetainedModelUnavailable

_REFERENCE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}")


def _reference(value: object) -> str:
    if not isinstance(value, str) or _REFERENCE.fullmatch(value) is None:
        raise RetainedModelInvalid("Invalid registered project reference")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class RetainedOntologyProject:
    project_id: str
    organization_id: str
    authority_ref: str
    worker_ref: str
    model_ref: str
    source_refs: tuple[str, ...]
    generation: int

    @property
    def resource_ref(self) -> str:
        return self.project_id


def _project(row: sqlite3.Row) -> RetainedOntologyProject:
    if row["enabled"] != 1:
        raise RetainedModelUnavailable("Original registered project is unavailable")
    generation = row["generation"]
    if type(generation) is not int or not 1 <= generation <= 2**53 - 1:
        raise RetainedModelInvalid("Invalid registered project generation")
    try:
        sources: object = json.loads(row["source_refs_json"])
        if not isinstance(sources, list) or len(sources) > 1000:
            raise RetainedModelInvalid("Invalid registered project sources")
        references = tuple(_reference(source) for source in sources)
        if references != tuple(sorted(set(references))):
            raise RetainedModelInvalid("Invalid registered project sources")
    except (TypeError, ValueError):
        raise RetainedModelInvalid("Invalid registered project sources") from None
    return RetainedOntologyProject(
        _reference(row["project_id"]),
        _reference(row["org_id"]),
        _reference(row["authority_ref"]),
        _reference(row["worker_ref"]),
        _reference(row["model_ref"]),
        references,
        generation,
    )


class RetainedOntologyProjectRepo:
    """Read only original ontology_projects, without touching sessions or users.

    Trusted composition must separately hold and revalidate the real human
    principal and full model/source/document grant around offloaded I/O. A
    returned project is not proof of access. Never publish raw inventory or
    derive a fake session/binding to satisfy an execution-only consumer.
    """

    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path) or not database_path.is_absolute():
            raise ValueError("Require an explicit absolute retained SQLite path")
        self._uri = database_path.as_uri() + "?mode=ro"

    def get(self, *, organization_id: str, project_id: str) -> RetainedOntologyProject:
        """Recover one exact enabled project, with no registration or fallback."""
        _reference(organization_id)
        _reference(project_id)
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                row = connection.execute(
                    """SELECT project_id, org_id, authority_ref, worker_ref, model_ref,
                        source_refs_json, generation, enabled
                    FROM ontology_projects
                    WHERE org_id = ? AND project_id = ? AND enabled = 1""",
                    (organization_id, project_id),
                ).fetchone()
                if row is None:
                    raise RetainedModelUnavailable("Original registered project is unavailable")
                return _project(row)
        except (sqlite3.Error, OSError, UnicodeError):
            raise RetainedStoreUnavailable("Retained project store is unavailable") from None

    def assert_current(self, expected: RetainedOntologyProject) -> None:
        """Refuse every tuple or generation change; never substitute latest facts."""
        if type(expected) is not RetainedOntologyProject:
            raise RetainedModelInvalid("Require the original registered project")
        current = self.get(organization_id=expected.organization_id, project_id=expected.project_id)
        if current != expected:
            raise RetainedModelInvalid("Original registered project changed")
