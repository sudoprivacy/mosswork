"""Canonical compatibility bindings in the separate Mosswork PRODUCT database.

This repository does not initialize a pool or schema. Trusted composition must
supply the product pool, never the original canonical store. No account, grant,
Agent, login credential or execution session is created here.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, now_ts
from octop.infra.db.repos.users import UserRepo
from octop.infra.utils.ulid import new_ulid


class CanonicalProjectionConflict(Exception):
    """The canonical member is already associated with another account."""


class CanonicalProjectionUserChanged(Exception):
    """The local compatibility row acquired local authority or was disabled."""


class _ConcurrentAllocation(Exception):
    """Roll back a losing allocation before reading the committed winner."""


@dataclass(frozen=True)
class CanonicalProjectionRow:
    authority_id: str
    account_id: str
    organization_id: str
    member_id: str
    local_user_id: int


_SELECT = """SELECT p.authority_id, p.account_id, p.organization_id, p.member_id,
    p.local_user_id, u.id AS existing_user_id, u.role, u.disabled, u.password_hash,
    u.permissions, u.sso_provider_id, u.sso_subject, u.expert_access_mode,
    EXISTS (SELECT 1 FROM user_sso_identities s WHERE s.user_id = u.id) AS has_sso
FROM canonical_user_projections p LEFT JOIN users u ON u.id = p.local_user_id
WHERE p.authority_id = ? AND p.organization_id = ? AND p.member_id = ?"""


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid canonical product binding")
    return value


def _empty_permissions(raw: object) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        parsed: object = json.loads(raw)
        return isinstance(parsed, list) and len(parsed) == 0
    except (ValueError, TypeError):
        return False


def _binding(row: DbRow, *, account_id: str) -> CanonicalProjectionRow:
    if row["account_id"] != account_id:
        raise CanonicalProjectionConflict
    local_user_id = row["local_user_id"]
    if (
        type(local_user_id) is not int
        or local_user_id <= 0
        or row["existing_user_id"] != local_user_id
    ):
        raise ValueError("Invalid product user allocation")
    if (
        row["role"] != "user"
        or row["disabled"] != 0
        or row["password_hash"] is not None
        or not _empty_permissions(row["permissions"])
        or row["sso_provider_id"] is not None
        or row["sso_subject"] is not None
        or row["has_sso"]
        or row["expert_access_mode"] != "assigned_only"
    ):
        raise CanonicalProjectionUserChanged
    return CanonicalProjectionRow(
        _text(row["authority_id"]),
        _text(row["account_id"]),
        _text(row["organization_id"]),
        _text(row["member_id"]),
        local_user_id,
    )


def _unique_violation(exc: Exception) -> bool:
    # PostgreSQL exposes SQLSTATE even when psycopg is an optional dependency.
    return isinstance(exc, sqlite3.IntegrityError) or getattr(exc, "sqlstate", None) == "23505"


class CanonicalIdentityProjectionRepo:
    """Allocate one unprivileged product User per authority/org/member binding.

    The account association is never updated. User deletion is restricted by
    the mapping FK; this repository has no detach/rebind/delete operation.
    SQLite's BEGIN IMMEDIATE serializes first allocation across pools. A
    PostgreSQL unique-conflict loser rolls back its new User and reads the
    committed winner in a new transaction, leaving no orphan User.
    """

    def __init__(self, product_db: DatabasePool) -> None:
        if product_db.dialect not in {"sqlite", "postgresql"}:
            raise ValueError("Unsupported product database dialect")
        self._db = product_db
        self._users = UserRepo(product_db)

    def get(
        self, *, authority_id: str, account_id: str, organization_id: str, member_id: str
    ) -> CanonicalProjectionRow | None:
        _text(account_id)
        key = (_text(authority_id), _text(organization_id), _text(member_id))
        with self._db.connect() as conn:
            row = conn.execute(_SELECT, key).fetchone()
            return None if row is None else _binding(row, account_id=account_id)

    def get_or_create(
        self, *, authority_id: str, account_id: str, organization_id: str, member_id: str
    ) -> CanonicalProjectionRow:
        _text(account_id)
        key = (_text(authority_id), _text(organization_id), _text(member_id))
        for _attempt in range(3):
            username: str | None = None
            try:
                with self._db.transaction() as conn:
                    row = conn.execute(_SELECT, key).fetchone()
                    if row is not None:
                        return _binding(row, account_id=account_id)
                    # An opaque product username never matches canonical profile
                    # fields or attaches an existing standalone User.
                    username = "canonical_" + new_ulid()
                    local_user_id = self._users.create_in_transaction(
                        conn, username=username, role="user", password_hash=None, permissions=[]
                    )
                    # Canonical consumers require actual resource assignments,
                    # including before the standalone assignment source is set.
                    conn.execute(
                        "UPDATE users SET expert_access_mode = 'assigned_only' WHERE id = ?",
                        (local_user_id,),
                    )
                    inserted = conn.execute(
                        "INSERT INTO canonical_user_projections "
                        "(authority_id, account_id, organization_id, member_id, local_user_id, "
                        "created_at) VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (authority_id, organization_id, member_id) DO NOTHING "
                        "RETURNING local_user_id",
                        (*key[:1], account_id, *key[1:], local_user_id, now_ts()),
                    ).fetchone()
                    if inserted is None:
                        raise _ConcurrentAllocation
                    row = conn.execute(_SELECT, key).fetchone()
                    if row is None:
                        raise RuntimeError("Product projection was not persisted")
                    return _binding(row, account_id=account_id)
            except _ConcurrentAllocation:
                continue
            except Exception as exc:
                if username is None or not _unique_violation(exc):
                    raise
                # Retry a username collision with a new product allocation.
                # Never use the conflicting standalone row as the projection.
                with self._db.connect() as conn:
                    collision: Any = conn.execute(
                        "SELECT id FROM users WHERE username = ?", (username,)
                    ).fetchone()
                if collision is None:
                    raise
        raise RuntimeError("Product projection allocation could not converge")
