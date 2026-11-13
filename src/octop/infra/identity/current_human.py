"""Recheck verified human identity facts in a consumer's canonical transaction.

This is not a credential verifier or permission issuer. Consumers must obtain
the snapshot through CanonicalSnapshotService, bind the connection to that same
canonical store, and open the required transaction. Writes still require real
writer admission and BEGIN IMMEDIATE; a transaction alone proves no takeover.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import CanonicalIdentitySnapshot


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR, status=503, details={"dependency": "canonical_current_human"}
    )


def assert_current_human(
    db: sqlite3.Connection, snapshot: CanonicalIdentitySnapshot, clock: Callable[[], float]
) -> None:
    """Assert current account/member/org facts without granting an operation.

    Callers keep their own signed scope, resource, Moss and native checks. This
    function neither parses raw claims nor creates/revises any authority row.
    ``snapshot`` is trusted verifier output, never a request-body principal.
    """
    if not isinstance(snapshot, CanonicalIdentitySnapshot):
        raise MossworkError.localized(ErrorCode.AUTH_FAILED)
    if not db.in_transaction:
        raise _unavailable()
    try:
        now = clock()
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or now < 0
        ):
            raise _unavailable()
    except MossworkError:
        raise
    except Exception:
        raise _unavailable() from None
    try:
        row = db.execute(
            """SELECT a.status, u.status, u.role, u.membership_revision,
                EXISTS (SELECT 1 FROM revoked_tokens WHERE jti = ?)
            FROM human_account_memberships m
            JOIN human_accounts a ON a.id = m.account_id
            JOIN users u ON u.id = m.user_id AND u.org_id = m.org_id
            JOIN organizations o ON o.id = m.org_id
            WHERE m.account_id = ? AND m.org_id = ? AND m.user_id = ?""",
            (
                snapshot.credential_id,
                snapshot.account_id,
                snapshot.organization_id,
                snapshot.member.owner_id,
            ),
        ).fetchone()
    except (sqlite3.Error, OSError):
        raise _unavailable() from None
    if (
        row is None
        or row[0] != "active"
        or row[1] != "active"
        or row[2] != snapshot.member.role
        or row[3] != snapshot.membership_revision
        or row[4]
        or snapshot.credential_expires_at <= now
    ):
        raise MossworkError.localized(ErrorCode.AUTH_FAILED)
