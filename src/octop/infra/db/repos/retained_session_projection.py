"""Immutable PRODUCT references to original cohost rows, never native authority."""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, now_ts
from octop.infra.db.repos.canonical_identity import CanonicalIdentityProjectionRepo
from octop.infra.db.repos.retained_cohost import RetainedCohostBinding


class SessionProjectionUnavailable(LookupError):
    """No unchanged canonical resource/thread projection is available."""


@dataclass(frozen=True, slots=True, repr=False)
class RetainedSessionProjection:
    authority_id: str
    agent_id: str
    thread_id: str
    original: RetainedCohostBinding


def _projection(row: DbRow) -> RetainedSessionProjection:
    return RetainedSessionProjection(
        row["authority_id"],
        row["agent_id"],
        row["thread_id"],
        RetainedCohostBinding(
            row["application_session_id"],
            row["organization_id"],
            row["owner_id"],
            row["native_agent_id"],
            row["durable_session_id"],
            row["repository_path"],
        ),
    )


_SELECT = """SELECT p.*, q.account_id, q.local_user_id
FROM retained_session_projections p
JOIN threads t ON t.thread_id=p.thread_id AND t.agent_id=p.agent_id
JOIN agents a ON a.agent_id=p.agent_id AND a.user_id=t.user_id
JOIN canonical_user_projections q ON q.authority_id=p.authority_id
    AND q.organization_id=p.organization_id AND q.member_id=p.owner_id
    AND q.local_user_id=t.user_id
WHERE p.thread_id=? AND p.agent_id=? AND p.authority_id=?"""


class RetainedSessionProjectionRepo:
    """No pool/schema initialization, account allocation, native start or updates.

    record_existing is private migration/composition I/O, not an HTTP grant.
    It accepts only already-created product rows and canonical owner mappings.
    Every execution must still reread the ORIGINAL store through its provider.
    Refused old host/k8s/unbound/deleted data remains in the preserved source;
    this table is not a migration deletion or preservation filter.
    """

    def __init__(self, product_db: DatabasePool) -> None:
        self._db = product_db
        self._identities = CanonicalIdentityProjectionRepo(product_db)

    def get(self, authority_id: str, agent_id: str, thread_id: str) -> RetainedSessionProjection:
        with self._db.connect() as conn:
            row = conn.execute(_SELECT, (thread_id, agent_id, authority_id)).fetchone()
        if row is None:
            raise SessionProjectionUnavailable("Original product session projection is unavailable")
        identity = self._identities.get(
            authority_id=authority_id,
            account_id=row["account_id"],
            organization_id=row["organization_id"],
            member_id=row["owner_id"],
        )
        if identity is None or identity.local_user_id != row["local_user_id"]:
            raise SessionProjectionUnavailable("Canonical product owner changed")
        return _projection(row)

    def record_existing(self, projection: RetainedSessionProjection) -> None:
        if (
            type(projection) is not RetainedSessionProjection
            or type(projection.original) is not RetainedCohostBinding
        ):
            raise ValueError("Require the exact original session projection")
        original = projection.original
        values = (
            projection.thread_id,
            projection.agent_id,
            projection.authority_id,
            original.organization_id,
            original.owner_id,
            original.application_session_id,
            original.native_agent_id,
            original.durable_session_id,
            original.repository_path,
        )
        if any(not isinstance(value, str) or not value or "\x00" in value for value in values):
            raise ValueError("Require explicit original and product identities")
        with self._db.transaction() as conn:
            owner = conn.execute(
                """SELECT q.local_user_id FROM canonical_user_projections q
                JOIN threads t ON t.user_id=q.local_user_id AND t.thread_id=? AND t.agent_id=?
                JOIN agents a ON a.agent_id=t.agent_id AND a.user_id=t.user_id
                WHERE q.authority_id=? AND q.organization_id=? AND q.member_id=?""",
                (*values[:3], original.organization_id, original.owner_id),
            ).fetchone()
            if owner is None:
                raise SessionProjectionUnavailable("Require the original canonical product owner")
            previous = conn.execute(
                "SELECT * FROM retained_session_projections WHERE thread_id=?",
                (projection.thread_id,),
            ).fetchone()
            if previous is not None:
                if _projection(previous) != projection:
                    raise SessionProjectionUnavailable("Original session projection is immutable")
                return
            conn.execute(
                """INSERT INTO retained_session_projections
                (thread_id,agent_id,authority_id,organization_id,owner_id,application_session_id,
                 native_agent_id,durable_session_id,repository_path,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (*values, now_ts()),
            )
