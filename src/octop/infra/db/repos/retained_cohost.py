"""Read the original published cohost binding without initializing its store.

The schema and binding validation follow archived Moss server-v0.1.103
(d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d). Product projections and current
action grants are supplied separately; a stored binding is not native Ready.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


class RetainedSessionUnavailable(LookupError):
    """No resumable cohost row matches the original session, organization and owner."""


class RetainedBindingMissing(RetainedSessionUnavailable):
    """The original cohost session has no published durable binding; never create one."""


class RetainedBindingInvalid(RuntimeError):
    """Retained facts disagree or fail the original binding schema."""


class RetainedStoreUnavailable(RuntimeError):
    """The original store cannot be read; no empty-store fallback is allowed."""


@dataclass(frozen=True, slots=True, repr=False)
class RetainedCohostBinding:
    application_session_id: str
    organization_id: str
    owner_id: str
    native_agent_id: str
    durable_session_id: str
    repository_path: str


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RetainedBindingInvalid("Invalid original binding")
    return value


def _binding(row: sqlite3.Row) -> RetainedCohostBinding:
    if row["binding_session_id"] is None:
        raise RetainedBindingMissing("Original durable binding is not published")
    owner = _text(row["owner_user_id"])
    agent = _text(row["native_agent_id"])
    durable = _text(row["durable_session_id"])
    repository = _text(row["repository_path"])
    try:
        # The original Zod max counts JavaScript UTF-16 code units.
        agent_length = len(agent.encode("utf-16-le")) // 2
    except UnicodeError:
        raise RetainedBindingInvalid("Invalid original binding") from None
    if (
        owner != row["user_id"]
        or agent_length > 256
        or re.search(r"[/\\\x00]", agent)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", durable) is None
        or not repository.startswith("/")
        or "\\" in repository
        or ".." in repository.split("/")
    ):
        raise RetainedBindingInvalid("Invalid original binding")
    return RetainedCohostBinding(
        _text(row["session_id"]),
        _text(row["org_id"]),
        owner,
        agent,
        durable,
        repository,
    )


class RetainedCohostBindingRepo:
    """Synchronous read-only SQL; callers offload I/O from the async event loop.

    Use the retained canonical database, never the product integer-user pool.
    Parameters are exact selectors from trusted projection, not authorization.
    This reader does not allocate projections, sessions, attempts or native IDs.
    Current principal/resource grants and native fences remain mandatory.
    """

    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path) or not database_path.is_absolute():
            raise ValueError("Require an explicit absolute retained SQLite path")
        self._uri = database_path.as_uri() + "?mode=ro"

    def get_for_resume(
        self, application_session_id: str, *, organization_id: str, owner_id: str
    ) -> RetainedCohostBinding:
        """Read one exact original binding; unknown/missing bindings refuse resume.

        Active ownership, undeleted cohost type and both original organization
        columns must agree. Host/k8s and missing-binding rows cannot become a
        fabricated cohost/native session. Ended cohost rows may retain a valid
        durable binding; stored attempt state is not native writer completion.
        """
        for value in (application_session_id, organization_id, owner_id):
            _text(value)
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                rows = connection.execute(
                    """SELECT s.session_id, s.org_id, s.user_id,
                        b.session_id AS binding_session_id, b.owner_user_id,
                        b.native_agent_id, b.durable_session_id, b.repository_path
                    FROM sessions s
                    JOIN users u ON u.id = s.user_id
                    LEFT JOIN cohost_sessions b ON b.session_id = s.session_id
                    WHERE s.session_id = ? AND s.org_id = ? AND s.user_id = ?
                        AND u.org_id = ? AND u.status = 'active'
                        AND s.runtime_type = 'cohost' AND s.deleted_at IS NULL
                    LIMIT 2""",
                    (application_session_id, organization_id, owner_id, organization_id),
                ).fetchall()
                if not rows:
                    raise RetainedSessionUnavailable("Original cohost session is unavailable")
                if len(rows) != 1:
                    raise RetainedBindingInvalid("Ambiguous original binding")
                return _binding(rows[0])
        except (sqlite3.Error, OSError, UnicodeError):
            raise RetainedStoreUnavailable("Retained session store is unavailable") from None

    def assert_current(self, expected: RetainedCohostBinding) -> None:
        """Re-read the original mapping; this does not establish native incarnation."""
        if type(expected) is not RetainedCohostBinding:
            raise RetainedBindingInvalid("Require the original binding")
        current = self.get_for_resume(
            expected.application_session_id,
            organization_id=expected.organization_id,
            owner_id=expected.owner_id,
        )
        if current != expected:
            raise RetainedBindingInvalid("Original binding changed")
