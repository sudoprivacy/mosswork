"""Update the retained platform default through current canonical authority.

The explicit platform operation requires a current super administrator and its
existing signed admin ceiling. It never promotes organization administration,
changes organization overrides, configures providers or creates credentials.
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


class CanonicalPlatformLoginMethodReadiness(Protocol):
    async def assert_platform_available(self, method: Literal["sms", "cas"]) -> None:
        """Check actual platform SMS/CAS availability; return None or raise.

        CAS uses platform policy and the original enabled provider inventory,
        not the selected organization's policy. SMS needs the actual configured
        runtime and credentials. This port neither configures nor probes login.
        """
        ...


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR,
        status=503,
        details={"dependency": "canonical_platform_login_policy"},
    )


def _require_platform_administrator(snapshot: CanonicalIdentitySnapshot) -> None:
    if snapshot.member.role != "super_admin" or not any(
        scope in snapshot.scopes for scope in ("*", "admin:*")
    ):
        raise MossworkError.localized(ErrorCode.FORBIDDEN)


class CanonicalPlatformLoginPolicyService:
    """Write only original platform/default loginMethod under the one writer.

    Organization selection still identifies the authenticated member, never a
    destination selector. Original organization rows and unrelated platform
    keys are retained. Admission must hold real exclusion through the write.
    Refusal after commit preserves an uncertain outcome; it is not rollback or
    permission to retry. No platform inheritance operation exists.
    """

    def __init__(
        self,
        database_path: Path,
        snapshots: CanonicalSnapshotService,
        *,
        writer: CanonicalWriterAdmission,
        readiness: CanonicalPlatformLoginMethodReadiness,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A trusted canonical SQLite Path is required")
        uri = database_path.absolute().as_uri()
        if not isinstance(snapshots, CanonicalSnapshotService) or snapshots._store._uri != (
            uri + "?mode=ro"
        ):
            raise _unavailable()
        if not callable(getattr(readiness, "assert_platform_available", None)):
            raise TypeError("The actual platform login-method readiness port is required")
        self._write_uri = uri + "?mode=rw"
        self._snapshots, self._writer = snapshots, writer
        self._readiness, self._clock = readiness, clock

    def _write(self, snapshot: CanonicalIdentitySnapshot, method: LoginMethod) -> None:
        try:
            with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                _require_platform_administrator(snapshot)
                assert_current_human(db, snapshot, self._clock)
                policy = _policy(db, "platform", "default")
                policy["loginMethod"] = {"sms": 0, "password": 1, "cas": 2}[method]
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
                    VALUES ('platform','default',?,?,?,?)
                    ON CONFLICT(scope_type,scope_id) DO UPDATE SET
                    policy_json=excluded.policy_json,updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at""",
                    (
                        json.dumps(policy, ensure_ascii=True, separators=(",", ":")),
                        snapshot.member.owner_id,
                        timestamp,
                        timestamp,
                    ),
                )
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    async def set_method(self, access_token: str, method: LoginMethod) -> None:
        """Set the platform default after fresh role, ceiling and readiness checks."""
        if not isinstance(method, str) or method not in ("sms", "password", "cas"):
            raise MossworkError.localized(
                ErrorCode.IDENTITY_REQUEST_INVALID, status=400, details={"field": "login_method"}
            )
        before = await self._snapshots.snapshot(access_token)
        _require_platform_administrator(before)
        await assert_canonical_writer(self._writer)
        if method in ("sms", "cas"):
            assert_available: Callable[[Literal["sms", "cas"]], Awaitable[object]] = (
                self._readiness.assert_platform_available
            )
            try:
                result = await assert_available(method)
            except MossworkError:
                raise
            except Exception:
                raise _unavailable() from None
            if result is not None:
                raise _unavailable()
        current = await self._snapshots.snapshot(access_token)
        _require_platform_administrator(current)
        _assert_same_actor(before, current)
        await assert_canonical_writer(self._writer)
        await asyncio.get_running_loop().run_in_executor(None, self._write, current, method)
        await assert_canonical_writer(self._writer)
        after = await self._snapshots.snapshot(access_token)
        _require_platform_administrator(after)
        _assert_same_actor(current, after)
