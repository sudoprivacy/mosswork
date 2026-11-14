"""Update the selected organization's retained login policy.

This ports only the organization-scoped login/inheritance operations from
Moss103. Platform configuration, provider setup and HTTP mounting remain with
their existing composition. No role is converted into platform authority.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from pathlib import Path
from typing import Literal, Protocol

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import CanonicalIdentitySnapshot, CanonicalSnapshotService
from octop.infra.identity.current_human import assert_current_human
from octop.infra.identity.login_policy import LoginMethod, _policy
from octop.infra.identity.organization_members import _assert_same_actor
from octop.infra.identity.write_authority import CanonicalWriterAdmission, assert_canonical_writer


class CanonicalLoginMethodReadiness(Protocol):
    async def assert_available(self, method: Literal["sms", "cas"], organization_id: str) -> None:
        """Check actual configured SMS/CAS availability; return None or raise."""
        ...


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR,
        status=503,
        details={"dependency": "canonical_organization_login_policy"},
    )


def _require_administrator(snapshot: CanonicalIdentitySnapshot) -> None:
    # A limited admin:users credential does not authorize login configuration.
    # Reuse the existing broad admin ceiling; introduce no new signed scope.
    if snapshot.member.role not in ("admin", "super_admin") or not any(
        scope in snapshot.scopes for scope in ("*", "admin:*")
    ):
        raise MossworkError.localized(ErrorCode.FORBIDDEN)


class CanonicalOrganizationLoginPolicyService:
    """Preserve original policy rows under the one canonical writer.

    The selected organization comes exclusively from the verified principal.
    Even super-admin credentials stay organization-scoped through this port.
    Writer admission must retain real exclusion across the operation. A final
    refusal after SQL commit does not imply rollback or permit an automatic
    retry. No schema, account, credential or native session is created here.
    """

    def __init__(
        self,
        database_path: Path,
        snapshots: CanonicalSnapshotService,
        *,
        writer: CanonicalWriterAdmission,
        readiness: CanonicalLoginMethodReadiness,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A trusted canonical SQLite Path is required")
        uri = database_path.absolute().as_uri()
        if not isinstance(snapshots, CanonicalSnapshotService) or snapshots._store._uri != (
            uri + "?mode=ro"
        ):
            raise _unavailable()
        self._write_uri = uri + "?mode=rw"
        self._snapshots, self._writer = snapshots, writer
        self._readiness, self._clock = readiness, clock

    def _write(self, snapshot: CanonicalIdentitySnapshot, method: LoginMethod | None) -> None:
        try:
            with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                _require_administrator(snapshot)
                assert_current_human(db, snapshot, self._clock)
                policy = _policy(db, "organization", snapshot.organization_id)
                if method is None:
                    policy.pop("loginMethod", None)
                    policy["loginMethodInherited"] = True
                else:
                    policy["loginMethod"] = {"sms": 0, "password": 1, "cas": 2}[method]
                    if _policy(db, "platform", "default").get("loginMethodInherited") is False:
                        policy.pop("loginMethodInherited", None)
                    else:
                        policy["loginMethodInherited"] = False
                now = self._clock()
                if (
                    isinstance(now, bool)
                    or not isinstance(now, (int, float))
                    or not math.isfinite(now)
                    or now < 0
                ):
                    raise _unavailable()
                timestamp = math.floor(now * 1000)
                db.execute(
                    """INSERT INTO client_delivery_policies
                    (scope_type,scope_id,policy_json,updated_by,created_at,updated_at)
                    VALUES ('organization',?,?,?,?,?)
                    ON CONFLICT(scope_type,scope_id) DO UPDATE SET
                    policy_json=excluded.policy_json,updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at""",
                    (
                        snapshot.organization_id,
                        json.dumps(policy, ensure_ascii=True, separators=(",", ":")),
                        snapshot.member.owner_id,
                        timestamp,
                        timestamp,
                    ),
                )
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    async def _update(self, access_token: str, method: LoginMethod | None) -> None:
        before = await self._snapshots.snapshot(access_token)
        _require_administrator(before)
        await assert_canonical_writer(self._writer)
        if method in ("sms", "cas"):
            assert_available: Callable[[Literal["sms", "cas"], str], Awaitable[object]] = (
                self._readiness.assert_available
            )
            try:
                result = await assert_available(method, before.organization_id)
            except MossworkError:
                raise
            except Exception:
                raise _unavailable() from None
            if result is not None:
                raise _unavailable()
        current = await self._snapshots.snapshot(access_token)
        _require_administrator(current)
        _assert_same_actor(before, current)
        await assert_canonical_writer(self._writer)
        await asyncio.get_running_loop().run_in_executor(None, self._write, current, method)
        await assert_canonical_writer(self._writer)
        after = await self._snapshots.snapshot(access_token)
        _require_administrator(after)
        _assert_same_actor(current, after)

    async def set_method(self, access_token: str, method: LoginMethod) -> None:
        """Set one original numeric login method after current readiness checks."""
        if not isinstance(method, str) or method not in ("sms", "password", "cas"):
            raise MossworkError.localized(
                ErrorCode.IDENTITY_REQUEST_INVALID, status=400, details={"field": "login_method"}
            )
        await self._update(access_token, method)

    async def inherit(self, access_token: str) -> None:
        """Remove only the org method and restore explicit platform inheritance."""
        await self._update(access_token, None)
