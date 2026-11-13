"""Preservation checks for destructive PRODUCT operations, independent of access grants."""

from typing import Any


class RetainedResourceError(RuntimeError):
    """A local deletion would remove retained canonical identity or history links."""


def assert_agent_deletable(conn: Any, agent_id: str) -> None:
    # Read raw evidence, not the access reader: revocation, disabled users and a
    # failed original-store recheck must never turn retained rows into garbage.
    retained = conn.execute(
        """SELECT 1 FROM retained_session_projections WHERE agent_id = ?
        UNION ALL
        SELECT 1 FROM agents a JOIN canonical_user_projections p ON p.local_user_id = a.user_id
        WHERE a.agent_id = ?
        UNION ALL
        SELECT 1 FROM threads t JOIN canonical_user_projections p ON p.local_user_id = t.user_id
        WHERE t.agent_id = ? LIMIT 1""",
        (agent_id, agent_id, agent_id),
    ).fetchone()
    if retained is not None:
        raise RetainedResourceError("Canonical resource and history must be retained")


def assert_thread_deletable(conn: Any, thread_id: str) -> None:
    # Include the parent's retained evidence and canonical owner before a
    # reference exists, so an in-flight mapping cannot race checkpoint cleanup.
    retained = conn.execute(
        """SELECT 1 FROM retained_session_projections
        WHERE thread_id = ? OR agent_id = (SELECT agent_id FROM threads WHERE thread_id = ?)
        UNION ALL
        SELECT 1 FROM threads t JOIN agents a ON a.agent_id = t.agent_id
        JOIN canonical_user_projections p ON p.local_user_id IN (t.user_id, a.user_id)
        WHERE t.thread_id = ? LIMIT 1""",
        (thread_id, thread_id, thread_id),
    ).fetchone()
    if retained is not None:
        raise RetainedResourceError("Canonical resource and history must be retained")


def assert_user_deletable(conn: Any, user_id: int) -> None:
    """Prevent local account removal from bypassing resource preservation by cascade."""
    if (
        conn.execute(
            "SELECT 1 FROM canonical_user_projections WHERE local_user_id = ?", (user_id,)
        ).fetchone()
        is not None
    ):
        raise RetainedResourceError("Canonical resource and history must be retained")
    for row in conn.execute("SELECT agent_id FROM agents WHERE user_id = ?", (user_id,)).fetchall():
        assert_agent_deletable(conn, str(row["agent_id"]))
    for row in conn.execute(
        "SELECT thread_id FROM threads WHERE user_id = ?", (user_id,)
    ).fetchall():
        assert_thread_deletable(conn, str(row["thread_id"]))
