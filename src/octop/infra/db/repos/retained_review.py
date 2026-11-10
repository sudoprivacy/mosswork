"""Immutable served documents in the original canonical review value store.

Uses server-v0.1.103 ontology_review_values, without schema initialization or a
second content store. Current grants and domain document validation belong to
the host. SQL persistence does not authenticate comments or acknowledge input.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from octop.infra.db.repos.retained_cohost import RetainedCohostBinding
from octop.infra.db.repos.retained_ontology import RetainedOntologyBinding


class ReviewDocumentUnavailable(RuntimeError):
    """No verified persisted document; no current-head or empty-store fallback."""


@dataclass(frozen=True, slots=True, repr=False)
class StoredReviewDocument:
    record: dict[str, Any]
    html: str
    account_id: str
    actor_id: str


def _key(endpoint: str) -> str:
    if not isinstance(endpoint, str) or re.fullmatch(r"/review/[a-f0-9]{32}/v1", endpoint) is None:
        raise ValueError("Require the original served document endpoint")
    return "served_review/v1:" + endpoint


def _encode(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _scope(original: RetainedCohostBinding, registered: RetainedOntologyBinding) -> dict[str, Any]:
    if (
        type(original) is not RetainedCohostBinding
        or type(registered) is not RetainedOntologyBinding
        or registered.application_session_id != original.application_session_id
        or registered.organization_id != original.organization_id
        or registered.owner_id != original.owner_id
    ):
        raise ValueError("Require the original session and registered model")
    # Decode the tuple representation once to compare the exact stored JSON,
    # including original source references and registry generation.
    scope: dict[str, Any] = json.loads(
        _encode({"session": asdict(original), "registration": asdict(registered)})
    )
    return scope


class RetainedReviewDocumentRepo:
    """Commit immutable host-owned snapshots before the frame is published.

    Use the same existing canonical database and single writer role as the
    other retained repositories. Async consumers offload these synchronous
    methods. No table, directory, database, session or document ID is created.
    The private endpoint is host-selected, never an authority from a page.
    """

    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path) or not database_path.is_absolute():
            raise ValueError("Require an explicit absolute canonical SQLite path")
        self._uri = database_path.as_uri()

    def save(
        self,
        original: RetainedCohostBinding,
        registered: RetainedOntologyBinding,
        endpoint: str,
        *,
        record: dict[str, Any],
        html: str,
        account_id: str,
        actor_id: str,
    ) -> None:
        """Insert once or confirm identical persisted bytes, never overwrite.

        The account/member identify the authenticated capturing actor, distinct
        from the selected Agent owner. They are provenance, not read grants.
        An exception or lost/cancelled wait cannot establish whether a commit
        occurred; the immutable original endpoint remains the recovery key.
        """
        key, scope = _key(endpoint), _scope(original, registered)
        if (
            not isinstance(record, dict)
            or record.get("endpoint") != endpoint
            or not isinstance(html, str)
            or not html.strip()
            or any(not isinstance(v, str) or not v or "\x00" in v for v in (account_id, actor_id))
        ):
            raise ValueError("Require a complete host-owned served document")
        try:
            value = _encode(
                {
                    "format": "mosswork-served-review/v1",
                    **scope,
                    "account_id": account_id,
                    "actor_id": actor_id,
                    "record": record,
                    "html": html,
                }
            )
            with closing(sqlite3.connect(self._uri + "?mode=rw", uri=True, timeout=1.0)) as db:
                db.execute("PRAGMA synchronous = FULL")
                db.execute("PRAGMA foreign_keys = ON")
                with db:
                    db.execute(
                        """INSERT INTO ontology_review_values (org_id, key, value_json)
                        VALUES (?, ?, ?) ON CONFLICT (org_id, key) DO NOTHING""",
                        (original.organization_id, key, value),
                    )
                    row = db.execute(
                        "SELECT value_json FROM ontology_review_values WHERE org_id=? AND key=?",
                        (original.organization_id, key),
                    ).fetchone()
                    if row is None or row[0] != value:
                        raise ReviewDocumentUnavailable(
                            "Original review document cannot be rebound"
                        )
        except (sqlite3.Error, OSError, UnicodeError, TypeError, ValueError):
            raise ReviewDocumentUnavailable(
                "Canonical review persistence did not complete"
            ) from None

    def load(
        self,
        original: RetainedCohostBinding,
        registered: RetainedOntologyBinding,
        endpoint: str,
    ) -> StoredReviewDocument:
        """Read exact original metadata/HTML without a worker or head lookup.

        Legacy revision-only review_binding values are never recovery inputs.
        Domain restore must additionally check the stored pointer/checksums;
        these bytes alone confer neither actor provenance nor current access.
        """
        key, scope = _key(endpoint), _scope(original, registered)
        try:
            with closing(sqlite3.connect(self._uri + "?mode=ro", uri=True, timeout=1.0)) as db:
                db.execute("PRAGMA query_only = ON")
                row = db.execute(
                    "SELECT value_json FROM ontology_review_values WHERE org_id=? AND key=?",
                    (original.organization_id, key),
                ).fetchone()
                if row is None:
                    raise ReviewDocumentUnavailable("Original served document is unavailable")
                value = json.loads(row[0])
            if (
                not isinstance(value, dict)
                or set(value) != {"format", *scope, "account_id", "actor_id", "record", "html"}
                or value["format"] != "mosswork-served-review/v1"
                or any(value[name] != expected for name, expected in scope.items())
                or not isinstance(value["record"], dict)
                or value["record"].get("endpoint") != endpoint
                or not isinstance(value["html"], str)
                or not value["html"].strip()
                or any(
                    not isinstance(value[name], str) or not value[name] or "\x00" in value[name]
                    for name in ("account_id", "actor_id")
                )
            ):
                raise ReviewDocumentUnavailable("Original served document binding is invalid")
            return StoredReviewDocument(
                value["record"], value["html"], value["account_id"], value["actor_id"]
            )
        except (sqlite3.Error, OSError, UnicodeError, TypeError, ValueError):
            raise ReviewDocumentUnavailable("Canonical review recovery is unavailable") from None
