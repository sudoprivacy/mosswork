"""Manage retained Moss member grants through the existing human authority.

Management requires a current same-organization administrator and the signed
admin:users ceiling. A grant decision never authorizes a runtime operation;
consumers still recheck current identity, resources and native admission.
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from octop.infra.agents.personal_moss_access import (
    MossAccessDecision,
    MossMemberSnapshot,
    decide_moss_access,
)
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import (
    CanonicalIdentitySnapshot,
    CanonicalSnapshotService,
    has_signed_scope,
)
from octop.infra.identity.current_human import assert_current_human
from octop.infra.identity.write_authority import CanonicalWriterAdmission, assert_canonical_writer


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR, status=503, details={"dependency": "canonical_moss_entitlements"}
    )


def _target_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise MossworkError.localized(ErrorCode.MOSS_IDENTITY_INVALID)
    return value


def _require_administrator(snapshot: CanonicalIdentitySnapshot) -> None:
    if snapshot.member.role not in ("admin", "super_admin") or not has_signed_scope(
        snapshot.scopes, "admin:users"
    ):
        raise MossworkError.localized(ErrorCode.FORBIDDEN)


class CanonicalMossEntitlementService:
    """Use only the original canonical tables; never initialize or repair them.

    The writer assertion is not original-writer takeover or a cross-process
    lease. Trusted composition must establish and retain actual exclusion.
    Final admission refusal returns 503 even if the already authorized SQL
    transaction committed; it does not report a rollback that did not occur.
    """

    def __init__(
        self,
        database_path: Path,
        snapshots: CanonicalSnapshotService,
        *,
        writer: CanonicalWriterAdmission,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A trusted local canonical SQLite Path is required")
        self._read_uri = database_path.absolute().as_uri() + "?mode=ro"
        self._write_uri = database_path.absolute().as_uri() + "?mode=rw"
        if (
            not isinstance(snapshots, CanonicalSnapshotService)
            or snapshots._store._uri != self._read_uri
        ):
            raise _unavailable()
        self._snapshots, self._writer, self._clock = snapshots, writer, clock

    def _current_actor(self, db: sqlite3.Connection, snapshot: CanonicalIdentitySnapshot) -> None:
        """Recheck the verified actor in the consumer's existing transaction."""
        _require_administrator(snapshot)
        assert_current_human(db, snapshot, self._clock)

    @staticmethod
    def _access(
        db: sqlite3.Connection, organization_id: str, target_member_id: str
    ) -> MossAccessDecision:
        row = db.execute(
            """SELECT u.role, u.status, g.user_id AS grant_member_id, g.enabled
            FROM users u
            LEFT JOIN moss_member_access g ON g.org_id = u.org_id AND g.user_id = u.id
            WHERE u.id = ? AND u.org_id = ?""",
            (target_member_id, organization_id),
        ).fetchone()
        if row is None:
            raise MossworkError.localized(ErrorCode.NOT_FOUND)
        if not isinstance(row["role"], str) or not isinstance(row["status"], str):
            raise _unavailable()
        grant = None
        if row["grant_member_id"] is not None:
            enabled = row["enabled"]
            if type(enabled) is not int or enabled not in (0, 1):
                raise _unavailable()
            grant = enabled == 1
        return decide_moss_access(
            MossMemberSnapshot(organization_id, target_member_id, row["role"], row["status"]), grant
        )

    def _read_access(
        self, snapshot: CanonicalIdentitySnapshot, target_member_id: str
    ) -> MossAccessDecision:
        try:
            with closing(sqlite3.connect(self._read_uri, uri=True, timeout=1.0)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only = ON")
                db.execute("BEGIN")
                self._current_actor(db, snapshot)
                return self._access(db, snapshot.organization_id, target_member_id)
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    def _set_access(
        self, snapshot: CanonicalIdentitySnapshot, target_member_id: str, is_enabled: bool
    ) -> MossAccessDecision:
        try:
            with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                self._current_actor(db, snapshot)
                self._access(db, snapshot.organization_id, target_member_id)
                now = self._clock()
                if not math.isfinite(now) or now < 0:
                    raise _unavailable()
                db.execute(
                    """INSERT INTO moss_member_access
                    (org_id, user_id, enabled, updated_by, updated_at) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(org_id, user_id) DO UPDATE SET enabled = excluded.enabled,
                    updated_by = excluded.updated_by, updated_at = excluded.updated_at""",
                    (
                        snapshot.organization_id,
                        target_member_id,
                        int(is_enabled),
                        snapshot.member.owner_id,
                        math.floor(now * 1000),
                    ),
                )
                return self._access(db, snapshot.organization_id, target_member_id)
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    async def get_access(self, access_token: str, target_member_id: str) -> MossAccessDecision:
        target = _target_id(target_member_id)
        before = await self._snapshots.snapshot(access_token)
        _require_administrator(before)
        result = await asyncio.get_running_loop().run_in_executor(
            None, self._read_access, before, target
        )
        after = await self._snapshots.snapshot(access_token)
        _require_administrator(after)
        if (
            before.account_id != after.account_id
            or before.organization_id != after.organization_id
            or before.member != after.member
            or before.membership_revision != after.membership_revision
            or before.credential_id != after.credential_id
        ):
            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
        return result

    async def set_access(
        self, access_token: str, target_member_id: str, is_enabled: bool
    ) -> MossAccessDecision:
        target = _target_id(target_member_id)
        if type(is_enabled) is not bool:
            raise MossworkError.localized(
                ErrorCode.MOSS_IDENTITY_INVALID, details={"field": "is_enabled"}
            )
        snapshot = await self._snapshots.snapshot(access_token)
        _require_administrator(snapshot)
        await assert_canonical_writer(self._writer)
        result = await asyncio.get_running_loop().run_in_executor(
            None, self._set_access, snapshot, target, is_enabled
        )
        await assert_canonical_writer(self._writer)
        return result
