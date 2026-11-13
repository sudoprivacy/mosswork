"""Retained canonical API-key lifecycle, separate from human and native identity.

The same canonical SQLite rows, UUIDs, hash format and issuer scope policy are
preserved. This module does not issue JWTs or authorize a resource/session/native
operation. Trusted composition must retain real exclusive writer admission.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import (
    CanonicalIdentitySnapshot,
    CanonicalSnapshotService,
    has_signed_scope,
)
from octop.infra.identity.canonical_jwt import node_utf8
from octop.infra.identity.current_human import assert_current_human
from octop.infra.identity.login_policy import default_scopes_for_role
from octop.infra.identity.phone_auth import _JS_WHITESPACE
from octop.infra.identity.write_authority import CanonicalWriterAdmission, assert_canonical_writer


@dataclass(frozen=True)
class CanonicalApiKey:
    id: str
    organization_id: str
    member_id: str
    name: str
    prefix: str
    scopes: tuple[str, ...]
    status: Literal["active", "revoked"]
    created_at: int
    last_used_at: int | None


@dataclass(frozen=True, repr=False)
class CreatedCanonicalApiKey:
    api_key: CanonicalApiKey
    plain_text_key: str


@dataclass(frozen=True)
class CanonicalApiKeyFacts:
    """Verified machine-key facts, never a human or native-operation grant.

    Consumers still authorize the current resource/session and exact requested
    operation. A browser DTO is not trusted proof; composition consumes only a
    successful verify_existing_key call, not a request-constructed instance.
    """

    key_id: str
    organization_id: str
    member_id: str
    current_role: str
    scopes: tuple[str, ...]


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR, status=503, details={"dependency": "canonical_api_keys"}
    )


def _require_management_scope(snapshot: CanonicalIdentitySnapshot) -> None:
    if not has_signed_scope(snapshot.scopes, "admin:api_keys"):
        raise MossworkError.localized(ErrorCode.FORBIDDEN)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise MossworkError.localized(ErrorCode.MOSS_IDENTITY_INVALID)
    return value


def _invalid_json_constant(value: str) -> None:
    raise ValueError("Non-JSON numeric constant")


def _scopes(value: object) -> tuple[str, ...]:
    # Original parseJsonArray preserves only actual string array entries.
    if not isinstance(value, str):
        return ()
    try:
        parsed = json.loads(value, parse_constant=_invalid_json_constant)
    except (ValueError, TypeError):
        return ()
    return (
        tuple(item for item in parsed if isinstance(item, str)) if isinstance(parsed, list) else ()
    )


def _safe_key(row: sqlite3.Row) -> CanonicalApiKey:
    status = row["status"]
    if status not in ("active", "revoked"):
        raise _unavailable()
    return CanonicalApiKey(
        id=row["id"],
        organization_id=row["org_id"],
        member_id=row["user_id"],
        name=row["name"],
        prefix=row["prefix"],
        scopes=_scopes(row["scopes_json"]),
        status=status,
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


class CanonicalApiKeyService:
    """Manage original keys using current human admin/department visibility.

    The signed admin:api_keys scope permits entry to the issuer operation.
    Original full admins may choose any nonempty key scopes; department admins
    may choose only the original exact user-role scope list. A consumer cannot
    use this issuer rule to expand an existing credential's signed scopes.

    No schema initialization/backfill, secret logging, JWT issue, owner-mTLS or
    local integer authority lives here. Final writer refusal may follow a
    committed authorized transaction and does not claim a fictitious rollback.
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

    def _now(self) -> int:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise _unavailable()
        return math.floor(value * 1000)

    def _visibility(
        self, db: sqlite3.Connection, snapshot: CanonicalIdentitySnapshot
    ) -> set[str] | None:
        _require_management_scope(snapshot)
        assert_current_human(db, snapshot, self._clock)
        actor = db.execute(
            "SELECT role, department_id FROM users WHERE id = ? AND org_id = ?",
            (snapshot.member.owner_id, snapshot.organization_id),
        ).fetchone()
        if actor is None:
            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
        if actor["role"] in ("admin", "super_admin"):
            return None
        if actor["role"] != "dept_admin" or not actor["department_id"]:
            return set()
        children: dict[str, list[str]] = {}
        for row in db.execute(
            "SELECT id, parent_id FROM departments WHERE org_id = ?", (snapshot.organization_id,)
        ):
            children.setdefault(row["parent_id"], []).append(row["id"])
        visible: set[str] = set()
        pending = [actor["department_id"]]
        while pending:
            current = pending.pop()
            if current in visible:
                continue
            visible.add(current)
            pending.extend(children.get(current, ()))
        return visible

    @staticmethod
    def _target(
        db: sqlite3.Connection,
        snapshot: CanonicalIdentitySnapshot,
        visible: set[str] | None,
        member_id: str,
    ) -> None:
        row = db.execute(
            "SELECT role, department_id FROM users WHERE id = ? AND org_id = ?",
            (member_id, snapshot.organization_id),
        ).fetchone()
        if row is None:
            raise MossworkError.localized(ErrorCode.NOT_FOUND)
        if visible is not None and (
            row["role"] not in ("user", "dept_admin")
            or row["department_id"] is None
            or row["department_id"] not in visible
        ):
            raise MossworkError.localized(ErrorCode.FORBIDDEN)

    def _list(self, snapshot: CanonicalIdentitySnapshot) -> tuple[CanonicalApiKey, ...]:
        try:
            with closing(sqlite3.connect(self._read_uri, uri=True, timeout=1.0)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only = ON")
                db.execute("BEGIN")
                visible = self._visibility(db, snapshot)
                rows = db.execute(
                    """SELECT k.*, u.role AS owner_role, u.department_id FROM api_keys k
                    JOIN users u ON u.id = k.user_id AND u.org_id = k.org_id
                    WHERE k.org_id = ? ORDER BY k.created_at ASC""",
                    (snapshot.organization_id,),
                )
                return tuple(
                    _safe_key(row)
                    for row in rows
                    if visible is None
                    or (
                        row["owner_role"] in ("user", "dept_admin")
                        and row["department_id"] in visible
                    )
                )
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    def _create(
        self,
        snapshot: CanonicalIdentitySnapshot,
        target_member_id: str,
        name: str,
        scopes: tuple[str, ...],
    ) -> CreatedCanonicalApiKey:
        try:
            with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys = ON")
                db.execute("BEGIN IMMEDIATE")
                visible = self._visibility(db, snapshot)
                self._target(db, snapshot, visible, target_member_id)
                if snapshot.member.role not in ("admin", "super_admin") and any(
                    scope not in default_scopes_for_role("user") for scope in scopes
                ):
                    raise MossworkError.localized(ErrorCode.FORBIDDEN)
                key_id = str(uuid4())
                secret = (
                    base64.urlsafe_b64encode(secrets.token_bytes(24)).decode("ascii").rstrip("=")
                )
                plain_text_key = f"moss_sk_{key_id}.{secret}"
                db.execute(
                    """INSERT INTO api_keys (id, org_id, user_id, name, prefix, secret_hash,
                    scopes_json, status, created_at, last_used_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL)""",
                    (
                        key_id,
                        snapshot.organization_id,
                        target_member_id,
                        name,
                        plain_text_key[:16],
                        hashlib.sha256(secret.encode()).hexdigest(),
                        json.dumps(scopes, separators=(",", ":"), ensure_ascii=False),
                        self._now(),
                    ),
                )
                row = db.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
                if row is None:
                    raise _unavailable()
                return CreatedCanonicalApiKey(_safe_key(row), plain_text_key)
        except (sqlite3.Error, OSError, UnicodeError):
            raise _unavailable() from None

    def _revoke(self, snapshot: CanonicalIdentitySnapshot, key_id: str) -> None:
        try:
            with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                visible = self._visibility(db, snapshot)
                row = db.execute(
                    "SELECT user_id FROM api_keys WHERE id = ? AND org_id = ?",
                    (key_id, snapshot.organization_id),
                ).fetchone()
                if row is None:
                    raise MossworkError.localized(ErrorCode.NOT_FOUND)
                self._target(db, snapshot, visible, row["user_id"])
                db.execute("UPDATE api_keys SET status = 'revoked' WHERE id = ?", (key_id,))
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    def _verify(self, key_id: str, secret: str) -> CanonicalApiKeyFacts:
        try:
            with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                return self._verify_in_transaction(db, key_id, secret)
        except UnicodeError:
            raise MossworkError.localized(ErrorCode.AUTH_FAILED) from None
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    def _verify_in_transaction(
        self, db: sqlite3.Connection, key_id: str, secret: str
    ) -> CanonicalApiKeyFacts:
        row = db.execute(
            """SELECT k.*, u.role AS owner_role, u.status AS owner_status,
            m.account_id, m.org_id AS linked_org_id, a.status AS account_status
            FROM api_keys k JOIN users u ON u.id = k.user_id AND u.org_id = k.org_id
            LEFT JOIN human_account_memberships m ON m.user_id = u.id
            LEFT JOIN human_accounts a ON a.id = m.account_id
            JOIN organizations o ON o.id = k.org_id WHERE k.id = ?""",
            (key_id,),
        ).fetchone()
        if (
            row is None
            or row["status"] != "active"
            or row["owner_status"] != "active"
            or (
                row["account_id"] is not None
                and (row["linked_org_id"] != row["org_id"] or row["account_status"] != "active")
            )
            or not isinstance(row["secret_hash"], str)
            or not hmac.compare_digest(
                row["secret_hash"].encode("utf-8"),
                hashlib.sha256(node_utf8(secret)).hexdigest().encode(),
            )
        ):
            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
        db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (self._now(), key_id))
        return CanonicalApiKeyFacts(
            row["id"],
            row["org_id"],
            row["user_id"],
            row["owner_role"],
            _scopes(row["scopes_json"]),
        )

    async def list_keys(self, access_token: str) -> tuple[CanonicalApiKey, ...]:
        before = await self._snapshots.snapshot(access_token)
        _require_management_scope(before)
        result = await asyncio.get_running_loop().run_in_executor(None, self._list, before)
        after = await self._snapshots.snapshot(access_token)
        _require_management_scope(after)
        if (
            before.account_id != after.account_id
            or before.organization_id != after.organization_id
            or before.member != after.member
            or before.membership_revision != after.membership_revision
            or before.credential_id != after.credential_id
        ):
            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
        return result

    async def create_key(
        self, access_token: str, target_member_id: str, name: str, scopes: Sequence[str]
    ) -> CreatedCanonicalApiKey:
        target = _identifier(target_member_id)
        if (
            not isinstance(name, str)
            or not isinstance(scopes, (list, tuple))
            or any(not isinstance(scope, str) for scope in scopes)
        ):
            raise MossworkError.localized(ErrorCode.MOSS_IDENTITY_INVALID)
        normalized = tuple(
            scope.strip(_JS_WHITESPACE) for scope in scopes if scope.strip(_JS_WHITESPACE)
        )
        name = name.strip(_JS_WHITESPACE)
        if not name or not normalized:
            raise MossworkError.localized(ErrorCode.MOSS_IDENTITY_INVALID)
        snapshot = await self._snapshots.snapshot(access_token)
        _require_management_scope(snapshot)
        await assert_canonical_writer(self._writer)
        result = await asyncio.get_running_loop().run_in_executor(
            None, self._create, snapshot, target, name, normalized
        )
        await assert_canonical_writer(self._writer)
        return result

    async def revoke_key(self, access_token: str, key_id: str) -> None:
        target = _identifier(key_id)
        snapshot = await self._snapshots.snapshot(access_token)
        _require_management_scope(snapshot)
        await assert_canonical_writer(self._writer)
        await asyncio.get_running_loop().run_in_executor(None, self._revoke, snapshot, target)
        await assert_canonical_writer(self._writer)

    async def verify_existing_key(self, plain_text_key: str) -> CanonicalApiKeyFacts:
        """Check an original machine key and record actual use; issue no JWT.

        Requires writer admission because original last_used_at is a real write.
        The result cannot enter the normal human snapshot or native owner path.
        This combines the original exchange checks and final issued-token
        account check: linked accounts must be active; legacy unlinked members
        remain supported without backfill. No original JWT is minted here.
        """
        if not isinstance(plain_text_key, str) or not plain_text_key.strip(_JS_WHITESPACE):
            raise MossworkError.localized(ErrorCode.MOSS_IDENTITY_INVALID)
        match = re.fullmatch(
            r"moss_sk_([^.]+)\.([^\n\r\u2028\u2029]+)", plain_text_key.strip(_JS_WHITESPACE)
        )
        if match is None:
            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
        await assert_canonical_writer(self._writer)
        result = await asyncio.get_running_loop().run_in_executor(
            None, self._verify, match[1], match[2]
        )
        await assert_canonical_writer(self._writer)
        return result
