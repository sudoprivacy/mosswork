"""Retained organization directory and management using canonical authority.

Only an existing active human account may join another organization. Its other
memberships and credentials remain unchanged. This module does not backfill
legacy accounts, deliver welcome events, mutate roles/status or initialize SQL.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict
from uuid import uuid4

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import (
    CanonicalIdentitySnapshot,
    CanonicalSnapshotService,
    has_signed_scope,
)
from octop.infra.identity.current_human import assert_current_human
from octop.infra.identity.password_auth import _scrypt_record
from octop.infra.identity.phone_auth import _JS_WHITESPACE, normalize_phone
from octop.infra.identity.write_authority import CanonicalWriterAdmission, assert_canonical_writer


@dataclass(frozen=True)
class OrganizationMember:
    account_id: str | None
    organization_id: str
    member_id: str
    username: str
    display_name: str | None
    role: str
    status: str
    membership_revision: int
    created_at: int
    legacy_id: int | None
    department_id: str | None = None


@dataclass(frozen=True)
class OrganizationDepartment:
    department_id: str
    organization_id: str
    parent_id: str | None
    name: str
    created_at: int
    updated_at: int
    user_count: int


@dataclass(frozen=True)
class OrganizationDepartmentDetails:
    department_id: str
    organization_id: str
    parent_id: str | None
    name: str
    external_id: str | None
    created_at: int
    updated_at: int
    user_count: int


class DepartmentUpdate(TypedDict, total=False):
    name: str
    parent_id: str | None
    external_id: str | None


@dataclass(frozen=True)
class _Directory:
    members: tuple[OrganizationMember, ...]
    departments: tuple[OrganizationDepartment, ...]


@dataclass(frozen=True)
class OrganizationMemberAddition:
    member: OrganizationMember
    created: bool


@dataclass(frozen=True)
class OrganizationInvitation:
    invitation_id: str
    organization_id: str
    code: str = field(repr=False)
    status: str
    initial_credit_units: int | float
    legacy_initial_quota_usd: float | None
    used_by_member_id: str | None
    created_at: int
    used_at: int | None


@dataclass(frozen=True)
class OrganizationInvitationPage:
    items: tuple[OrganizationInvitation, ...]
    total: int


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR,
        status=503,
        details={"dependency": "canonical_organization_members"},
    )


def _require_administrator(snapshot: CanonicalIdentitySnapshot) -> None:
    if snapshot.member.role not in ("admin", "super_admin") or not has_signed_scope(
        snapshot.scopes, "admin:users"
    ):
        raise MossworkError.localized(ErrorCode.FORBIDDEN)


def _require_directory_reader(snapshot: CanonicalIdentitySnapshot) -> None:
    if snapshot.member.role not in ("admin", "super_admin", "dept_admin") or not has_signed_scope(
        snapshot.scopes, "admin:users"
    ):
        raise MossworkError.localized(ErrorCode.FORBIDDEN)


def _assert_same_actor(before: CanonicalIdentitySnapshot, after: CanonicalIdentitySnapshot) -> None:
    # Personal Moss entitlement is independent of organization administration.
    def identity(snapshot: CanonicalIdentitySnapshot) -> tuple[object, ...]:
        return (
            snapshot.authority_id,
            snapshot.account_id,
            snapshot.organization_id,
            snapshot.member,
            snapshot.membership_revision,
            snapshot.auth_app,
            snapshot.key_id,
            snapshot.credential_id,
            snapshot.credential_expires_at,
            snapshot.session_id,
            snapshot.scopes,
        )

    if identity(before) != identity(after):
        raise MossworkError.localized(ErrorCode.AUTH_FAILED)


def _invalid_department(status: int = 400) -> MossworkError:
    return MossworkError.localized(
        ErrorCode.IDENTITY_REQUEST_INVALID, status=status, details={"field": "department"}
    )


def _department_scope(
    db: sqlite3.Connection,
    snapshot: CanonicalIdentitySnapshot,
    departments: list[sqlite3.Row],
) -> set[str] | None:
    if snapshot.member.role != "dept_admin":
        return None
    actor = db.execute(
        "SELECT department_id FROM users WHERE id = ? AND org_id = ?",
        (snapshot.member.owner_id, snapshot.organization_id),
    ).fetchone()
    visible: set[str] = set()
    pending = [actor[0]] if actor and actor[0] else []
    children: dict[str | None, list[str]] = {}
    for department in departments:
        children.setdefault(department["parent_id"], []).append(department["id"])
    while pending:
        current = pending.pop()
        if current in visible:
            continue
        visible.add(current)
        pending.extend(children.get(current, ()))
    return visible


def _assert_department_scope(visible: set[str] | None, target: str | None) -> None:
    if visible is not None and (target is None or target not in visible):
        raise MossworkError.localized(ErrorCode.FORBIDDEN)


def _require_department_external_index(db: sqlite3.Connection) -> None:
    definition = db.execute(
        """SELECT sql FROM sqlite_master WHERE type='index'
        AND tbl_name='departments' AND name='departments_ext_uniq'"""
    ).fetchone()
    index = next(
        (
            row
            for row in db.execute("PRAGMA index_list(departments)")
            if row["name"] == "departments_ext_uniq"
        ),
        None,
    )
    columns = [row["name"] for row in db.execute("PRAGMA index_info(departments_ext_uniq)")]
    predicate = (
        "".join((definition[0] or "").lower().split()).partition("where")[2] if definition else ""
    )
    if (
        index is None
        or not index["unique"]
        or not index["partial"]
        or columns != ["org_id", "ext_dept_id"]
        or predicate != "ext_dept_idisnotnull"
    ):
        raise _unavailable()


_MEMBERS = """SELECT u.id, u.org_id, u.name, u.display_name, u.role, u.status, u.department_id,
    u.membership_revision, u.created_at, m.account_id, a.legacy_id
    FROM users u
    LEFT JOIN human_account_memberships m ON m.user_id = u.id AND m.org_id = u.org_id
    LEFT JOIN resource_numeric_aliases a ON a.namespace = 'user'
        AND a.resource_id = u.id AND a.org_id = u.org_id"""


def _member(row: sqlite3.Row) -> OrganizationMember:
    return OrganizationMember(
        account_id=row["account_id"],
        organization_id=row["org_id"],
        member_id=row["id"],
        username=row["name"],
        display_name=row["display_name"],
        role=row["role"],
        status=row["status"],
        membership_revision=row["membership_revision"],
        created_at=row["created_at"],
        legacy_id=row["legacy_id"],
        department_id=row["department_id"],
    )


class CanonicalOrganizationMemberService:
    """Admin membership operations in the original store, without schema repair.

    The directory contains safe current organization/subtree facts, without a
    billing projection or an effect grant. Writer admission must
    be backed by actual original-writer exclusion in trusted composition; this
    protocol cannot establish that lease. A final refusal can follow a committed
    authorized transaction and never implies that SQL was rolled back.
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

    def _current_admin(self, db: sqlite3.Connection, snapshot: CanonicalIdentitySnapshot) -> None:
        _require_administrator(snapshot)
        assert_current_human(db, snapshot, self._clock)

    def _assert_password_target(
        self, db: sqlite3.Connection, snapshot: CanonicalIdentitySnapshot, member_id: str
    ) -> None:
        _require_directory_reader(snapshot)
        assert_current_human(db, snapshot, self._clock)
        target = db.execute(
            "SELECT role,department_id FROM users WHERE id=? AND org_id=?",
            (member_id, snapshot.organization_id),
        ).fetchone()
        if target is None:
            raise MossworkError.localized(ErrorCode.NOT_FOUND)
        if snapshot.member.role == "dept_admin":
            departments = db.execute(
                "SELECT id,parent_id FROM departments WHERE org_id=?",
                (snapshot.organization_id,),
            ).fetchall()
            if target["role"] not in ("user", "dept_admin"):
                raise MossworkError.localized(ErrorCode.FORBIDDEN)
            _assert_department_scope(
                _department_scope(db, snapshot, departments), target["department_id"]
            )

    def _prepare_password_reset(
        self, snapshot: CanonicalIdentitySnapshot, member_id: str, password: str
    ) -> str:
        try:
            with closing(sqlite3.connect(self._read_uri, uri=True, timeout=1.0)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                self._assert_password_target(db, snapshot, member_id)
            if not isinstance(password, str) or not password:
                raise MossworkError.localized(
                    ErrorCode.IDENTITY_REQUEST_INVALID, status=400, details={"field": "password"}
                )
            return _scrypt_record(password)
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    def _write_password_reset(
        self, snapshot: CanonicalIdentitySnapshot, member_id: str, password_hash: str
    ) -> None:
        try:
            with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                self._assert_password_target(db, snapshot, member_id)
                now = self._clock()
                if (
                    isinstance(now, bool)
                    or not isinstance(now, (int, float))
                    or not math.isfinite(now)
                    or now < 0
                ):
                    raise _unavailable()
                db.execute(
                    "UPDATE users SET password_hash=?,password_updated_at=? WHERE id=? AND org_id=?",
                    (password_hash, math.floor(now * 1000), member_id, snapshot.organization_id),
                )
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    async def set_member_password(self, access_token: str, member_id: str, password: str) -> None:
        """Reset an authorized original member's password without issuing credentials.

        The original admin operation accepts any nonempty password. It leaves
        local-auth flags, identities, membership revisions and issued tokens
        unchanged. Current target scope is checked again in the write transaction.
        A refusal after commit does not imply rollback or authorize replay.
        """
        before = await self._snapshots.snapshot(access_token)
        _require_directory_reader(before)
        await assert_canonical_writer(self._writer)
        loop = asyncio.get_running_loop()
        password_hash = await loop.run_in_executor(
            None, self._prepare_password_reset, before, member_id, password
        )
        await assert_canonical_writer(self._writer)
        await loop.run_in_executor(
            None, self._write_password_reset, before, member_id, password_hash
        )
        await assert_canonical_writer(self._writer)
        after = await self._snapshots.snapshot(access_token)
        _require_directory_reader(after)
        _assert_same_actor(before, after)

    def _list(self, snapshot: CanonicalIdentitySnapshot) -> tuple[OrganizationMember, ...]:
        return self._directory(snapshot).members

    def _directory(self, snapshot: CanonicalIdentitySnapshot) -> _Directory:
        try:
            with closing(sqlite3.connect(self._read_uri, uri=True, timeout=1.0)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only = ON")
                db.execute("BEGIN")
                _require_directory_reader(snapshot)
                assert_current_human(db, snapshot, self._clock)
                departments = db.execute(
                    """SELECT id, org_id, parent_id, name, created_at, updated_at
                    FROM departments WHERE org_id = ? ORDER BY created_at ASC""",
                    (snapshot.organization_id,),
                ).fetchall()
                visible = _department_scope(db, snapshot, departments)
                members = tuple(
                    _member(row)
                    for row in db.execute(
                        _MEMBERS + " WHERE u.org_id = ? ORDER BY u.created_at ASC",
                        (snapshot.organization_id,),
                    )
                    if visible is None
                    or (row["role"] in ("user", "dept_admin") and row["department_id"] in visible)
                )
                counts: dict[str, int] = {}
                for member in members:
                    if member.department_id is not None:
                        counts[member.department_id] = counts.get(member.department_id, 0) + 1
                return _Directory(
                    members,
                    tuple(
                        OrganizationDepartment(
                            department_id=row["id"],
                            organization_id=row["org_id"],
                            parent_id=row["parent_id"],
                            name=row["name"],
                            created_at=row["created_at"],
                            updated_at=row["updated_at"],
                            user_count=counts.get(row["id"], 0),
                        )
                        for row in departments
                        if visible is None or row["id"] in visible
                    ),
                )
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    def _verify_members(
        self, snapshot: CanonicalIdentitySnapshot, expected: tuple[OrganizationMember, ...]
    ) -> None:
        if self._directory(snapshot).members != expected:
            raise MossworkError.localized(ErrorCode.FORBIDDEN)

    def _verify_departments(
        self, snapshot: CanonicalIdentitySnapshot, expected: tuple[OrganizationDepartment, ...]
    ) -> None:
        if self._directory(snapshot).departments != expected:
            raise MossworkError.localized(ErrorCode.FORBIDDEN)

    def _write_department(
        self,
        snapshot: CanonicalIdentitySnapshot,
        action: Literal["create", "update", "delete"],
        department_id: str | None,
        changes: DepartmentUpdate,
    ) -> OrganizationDepartmentDetails | None:
        try:
            with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys = ON")
                db.execute("BEGIN IMMEDIATE")
                _require_directory_reader(snapshot)
                assert_current_human(db, snapshot, self._clock)
                org_id = snapshot.organization_id
                _require_department_external_index(db)
                departments = db.execute(
                    """SELECT id, org_id, parent_id, name, ext_dept_id, created_at, updated_at
                    FROM departments WHERE org_id = ?""",
                    (org_id,),
                ).fetchall()
                by_id = {row["id"]: row for row in departments}
                visible = _department_scope(db, snapshot, departments)
                current = by_id.get(department_id)
                if action != "create":
                    if current is None:
                        raise MossworkError.localized(ErrorCode.NOT_FOUND)
                    _assert_department_scope(visible, department_id)
                if action == "delete":
                    if (
                        any(row["parent_id"] == department_id for row in departments)
                        or db.execute(
                            "SELECT 1 FROM users WHERE org_id = ? AND department_id = ? LIMIT 1",
                            (org_id, department_id),
                        ).fetchone()
                    ):
                        raise _invalid_department(409)
                    db.execute(
                        "DELETE FROM departments WHERE id = ? AND org_id = ?",
                        (department_id, org_id),
                    )
                    return None

                if not changes or set(changes) - {"name", "parent_id", "external_id"}:
                    raise _invalid_department()
                name = changes.get("name", current["name"] if current else None)
                if not isinstance(name, str) or not (name := name.strip(_JS_WHITESPACE)):
                    raise _invalid_department()
                parent = changes.get("parent_id", current["parent_id"] if current else None)
                external = changes.get("external_id", current["ext_dept_id"] if current else None)
                if (parent is not None and not isinstance(parent, str)) or (
                    external is not None and not isinstance(external, str)
                ):
                    raise _invalid_department()
                parent = parent.strip(_JS_WHITESPACE) or None if parent is not None else None
                external = external.strip(_JS_WHITESPACE) or None if external is not None else None
                if action == "create" or "parent_id" in changes:
                    if parent is not None and parent not in by_id:
                        raise _invalid_department()
                    if action == "update":
                        cursor, visited = parent, set()
                        while cursor is not None and cursor in by_id:
                            if cursor == department_id or cursor in visited:
                                raise _invalid_department()
                            visited.add(cursor)
                            cursor = by_id[cursor]["parent_id"]
                    _assert_department_scope(visible, parent)
                if any(
                    row["parent_id"] == parent
                    and row["name"] == name
                    and row["id"] != department_id
                    for row in departments
                ):
                    raise _invalid_department(409)
                now = self._clock()
                if (
                    isinstance(now, bool)
                    or not isinstance(now, (int, float))
                    or not math.isfinite(now)
                    or now < 0
                ):
                    raise _unavailable()
                timestamp = math.floor(now * 1000)
                if action == "create":
                    department_id = str(uuid4())
                    db.execute(
                        """INSERT INTO departments
                        (id, org_id, parent_id, name, ext_dept_id, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (department_id, org_id, parent, name, external, timestamp, timestamp),
                    )
                else:
                    db.execute(
                        """UPDATE departments SET name = ?, parent_id = ?, ext_dept_id = ?,
                        updated_at = ? WHERE id = ? AND org_id = ?""",
                        (name, parent, external, timestamp, department_id, org_id),
                    )
                row = db.execute(
                    """SELECT d.*, (SELECT COUNT(*) FROM users u WHERE u.org_id=d.org_id
                    AND u.department_id=d.id) AS user_count FROM departments d
                    WHERE d.id = ? AND d.org_id = ?""",
                    (department_id, org_id),
                ).fetchone()
                if row is None:
                    raise _unavailable()
                return OrganizationDepartmentDetails(
                    department_id=row["id"],
                    organization_id=row["org_id"],
                    parent_id=row["parent_id"],
                    name=row["name"],
                    external_id=row["ext_dept_id"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    user_count=row["user_count"],
                )
        except sqlite3.IntegrityError as error:
            if getattr(error, "sqlite_errorname", "") in {
                "SQLITE_CONSTRAINT_UNIQUE",
                "SQLITE_CONSTRAINT_FOREIGNKEY",
            }:
                raise _invalid_department(409) from None
            raise _unavailable() from None
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    async def _mutate_department(
        self,
        access_token: str,
        action: Literal["create", "update", "delete"],
        department_id: str | None,
        changes: DepartmentUpdate,
    ) -> OrganizationDepartmentDetails | None:
        frozen_changes = changes.copy()
        before = await self._snapshots.snapshot(access_token)
        _require_directory_reader(before)
        await assert_canonical_writer(self._writer)
        result = await asyncio.get_running_loop().run_in_executor(
            None, self._write_department, before, action, department_id, frozen_changes
        )
        await assert_canonical_writer(self._writer)
        after = await self._snapshots.snapshot(access_token)
        _require_directory_reader(after)
        _assert_same_actor(before, after)
        return result

    async def create_department(
        self,
        access_token: str,
        name: str,
        *,
        parent_id: str | None = None,
        external_id: str | None = None,
    ) -> OrganizationDepartmentDetails:
        result = await self._mutate_department(
            access_token,
            "create",
            None,
            {"name": name, "parent_id": parent_id, "external_id": external_id},
        )
        if result is None:
            raise _unavailable()
        return result

    async def update_department(
        self,
        access_token: str,
        department_id: str,
        changes: DepartmentUpdate,
    ) -> OrganizationDepartmentDetails:
        result = await self._mutate_department(access_token, "update", department_id, changes)
        if result is None:
            raise _unavailable()
        return result

    async def delete_department(self, access_token: str, department_id: str) -> None:
        await self._mutate_department(access_token, "delete", department_id, {})

    def _add(self, snapshot: CanonicalIdentitySnapshot, phone: str) -> OrganizationMemberAddition:
        try:
            with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys = ON")
                db.execute("BEGIN IMMEDIATE")
                self._current_admin(db, snapshot)
                account = db.execute(
                    "SELECT id, status FROM human_accounts WHERE phone = ?", (phone,)
                ).fetchone()
                if account is None or account["status"] != "active":
                    raise MossworkError.localized(ErrorCode.NOT_FOUND)
                org_id, account_id = snapshot.organization_id, account["id"]
                existing = db.execute(
                    "SELECT user_id FROM human_account_memberships WHERE account_id = ? AND org_id = ?",
                    (account_id, org_id),
                ).fetchone()
                if existing is not None:
                    row = db.execute(
                        _MEMBERS + " WHERE u.id = ? AND u.org_id = ?", (existing[0], org_id)
                    ).fetchone()
                    if row is None:
                        raise MossworkError.localized(ErrorCode.INTERNAL_ERROR, status=409)
                    return OrganizationMemberAddition(_member(row), created=False)

                # Original legacy-phone profile wins, then the earliest active
                # membership; neither source account nor credentials are changed.
                profile = db.execute(
                    "SELECT display_name, name FROM users WHERE phone = ? LIMIT 1", (phone,)
                ).fetchone()
                if profile is None:
                    profile = db.execute(
                        """SELECT u.display_name, u.name FROM human_account_memberships m
                        JOIN users u ON u.id = m.user_id AND u.org_id = m.org_id
                        JOIN human_accounts a ON a.id = m.account_id
                        WHERE m.account_id = ? AND a.status = 'active' AND u.status = 'active'
                        ORDER BY m.created_at, m.org_id LIMIT 1""",
                        (account_id,),
                    ).fetchone()
                display_name = ((profile[0] or profile[1] or phone) if profile else phone).strip(
                    _JS_WHITESPACE
                ) or None
                username, command_key = (
                    "member-" + str(uuid4()),
                    "account-membership:" + str(uuid4()),
                )
                member_id, event_id = str(uuid4()), str(uuid4())
                if db.execute("SELECT 1 FROM users WHERE name = ?", (username,)).fetchone():
                    raise _unavailable()
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
                    """INSERT INTO users
                    (id, org_id, email, name, display_name, department_id, role, status, local_auth,
                    token_limit, password_hash, password_updated_at, last_login_at, created_at,
                    ext_user_id, phone, local_execution_allowed)
                    VALUES (?, ?, ?, ?, ?, NULL, 'user', 'active', 0,
                        NULL, NULL, NULL, NULL, ?, NULL, NULL, 1)""",
                    (
                        member_id,
                        org_id,
                        member_id + "@users.internal.moss",
                        username,
                        display_name,
                        timestamp,
                    ),
                )
                db.execute(
                    """INSERT INTO human_account_memberships (account_id, org_id, user_id, created_at)
                    VALUES (?, ?, ?, ?)""",
                    (account_id, org_id, member_id, timestamp),
                )
                counter = db.execute(
                    """INSERT INTO compatibility_id_counters (counter_key, last_value)
                    VALUES ('resource_numeric_aliases:user', 2000000000)
                    ON CONFLICT(counter_key) DO UPDATE SET
                        last_value = compatibility_id_counters.last_value + 1 RETURNING last_value"""
                ).fetchone()
                if counter is None:
                    raise _unavailable()
                legacy_id = counter[0]
                db.execute(
                    """INSERT INTO resource_numeric_aliases
                    (namespace, legacy_id, resource_id, org_id, migration_run_id, created_at)
                    VALUES ('user', ?, ?, ?, NULL, ?) ON CONFLICT DO NOTHING""",
                    (legacy_id, member_id, org_id, timestamp),
                )
                alias = db.execute(
                    "SELECT legacy_id FROM resource_numeric_aliases WHERE namespace = 'user' AND resource_id = ?",
                    (member_id,),
                ).fetchone()
                if alias is None:
                    raise _unavailable()
                legacy_id = alias[0]
                db.execute(
                    """INSERT INTO wallets
                    (owner_type, owner_id, balance_units, version, created_at, updated_at)
                    VALUES ('user', ?, 0, 0, ?, ?)""",
                    (member_id, timestamp, timestamp),
                )
                db.execute(
                    """INSERT INTO outbox_events
                    (id, event_type, aggregate_type, aggregate_id, payload_json, status,
                    context_source, idempotency_key, suppress_reason, created_at)
                    VALUES (?, 'user.welcome', 'user', ?, ?, 'pending', 'online', ?, NULL, ?)""",
                    (
                        event_id,
                        member_id,
                        json.dumps({"userId": member_id, "orgId": org_id}, separators=(",", ":")),
                        "welcome:" + command_key,
                        timestamp,
                    ),
                )
                db.execute(
                    """INSERT INTO command_executions
                    (command_type, idempotency_key, context_source, result_json, created_at)
                    VALUES ('identity.create_user', ?, 'online', ?, ?)""",
                    (
                        command_key,
                        json.dumps(
                            {"userId": member_id, "legacyUserId": legacy_id}, separators=(",", ":")
                        ),
                        timestamp,
                    ),
                )
                row = db.execute(
                    _MEMBERS + " WHERE u.id = ? AND u.org_id = ?", (member_id, org_id)
                ).fetchone()
                if row is None:
                    raise _unavailable()
                return OrganizationMemberAddition(_member(row), created=True)
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    async def list_members(self, access_token: str) -> tuple[OrganizationMember, ...]:
        before = await self._snapshots.snapshot(access_token)
        _require_directory_reader(before)
        result = await asyncio.get_running_loop().run_in_executor(None, self._list, before)
        after = await self._snapshots.snapshot(access_token)
        _require_directory_reader(after)
        _assert_same_actor(before, after)
        await asyncio.get_running_loop().run_in_executor(None, self._verify_members, after, result)
        return result

    async def list_departments(self, access_token: str) -> tuple[OrganizationDepartment, ...]:
        before = await self._snapshots.snapshot(access_token)
        _require_directory_reader(before)
        result = await asyncio.get_running_loop().run_in_executor(None, self._directory, before)
        after = await self._snapshots.snapshot(access_token)
        _require_directory_reader(after)
        _assert_same_actor(before, after)
        await asyncio.get_running_loop().run_in_executor(
            None, self._verify_departments, after, result.departments
        )
        return result.departments

    def _read_invitation_page(
        self,
        snapshot: CanonicalIdentitySnapshot,
        status: str | None,
        limit: int,
        offset: int,
    ) -> OrganizationInvitationPage:
        try:
            with closing(sqlite3.connect(self._read_uri, uri=True, timeout=1.0)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                self._current_admin(db, snapshot)
                where = "org_id=?" + (" AND status=?" if status is not None else "")
                params = (
                    (snapshot.organization_id, status)
                    if status is not None
                    else (snapshot.organization_id,)
                )
                total = db.execute(
                    "SELECT COUNT(*) FROM invitations WHERE " + where, params
                ).fetchone()[0]
                rows = db.execute(
                    "SELECT * FROM invitations WHERE "
                    + where
                    + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                    (*params, limit, offset),
                ).fetchall()
                return OrganizationInvitationPage(
                    items=tuple(
                        OrganizationInvitation(
                            invitation_id=row["id"],
                            organization_id=row["org_id"],
                            code=row["code"],
                            status=row["status"],
                            initial_credit_units=row["initial_credit_units"],
                            legacy_initial_quota_usd=row["legacy_initial_quota_usd"],
                            used_by_member_id=row["used_by_user_id"],
                            created_at=row["created_at"],
                            used_at=row["used_at"],
                        )
                        for row in rows
                    ),
                    total=total,
                )
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    def _verify_invitation_access(self, snapshot: CanonicalIdentitySnapshot) -> None:
        try:
            with closing(sqlite3.connect(self._read_uri, uri=True, timeout=1.0)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                self._current_admin(db, snapshot)
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    async def list_invitations(
        self,
        access_token: str,
        *,
        status: Literal["pending", "used", "revoked"] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> OrganizationInvitationPage:
        """Read retained invitation facts within the current selected organization."""
        before = await self._snapshots.snapshot(access_token)
        _require_administrator(before)
        if (
            status not in (None, "pending", "used", "revoked")
            or type(limit) is not int
            or type(offset) is not int
        ):
            raise MossworkError.localized(
                ErrorCode.IDENTITY_REQUEST_INVALID, status=400, details={"field": "invitations"}
            )
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            self._read_invitation_page,
            before,
            status,
            max(1, min(limit, 100)),
            max(0, offset),
        )
        after = await self._snapshots.snapshot(access_token)
        _require_administrator(after)
        _assert_same_actor(before, after)
        await asyncio.get_running_loop().run_in_executor(
            None, self._verify_invitation_access, after
        )
        return result

    async def add_registered_account(
        self, access_token: str, phone: str
    ) -> OrganizationMemberAddition:
        normalized = normalize_phone(phone)
        if normalized is None:
            raise MossworkError.localized(
                ErrorCode.MOSS_IDENTITY_INVALID, details={"field": "phone"}
            )
        snapshot = await self._snapshots.snapshot(access_token)
        _require_administrator(snapshot)
        await assert_canonical_writer(self._writer)
        result = await asyncio.get_running_loop().run_in_executor(
            None, self._add, snapshot, normalized
        )
        await assert_canonical_writer(self._writer)
        return result
