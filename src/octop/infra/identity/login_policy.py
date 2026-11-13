"""Read retained organization/platform login policy without creating authority.

Defaults come only from trusted server composition. Reading a method does not
authenticate a phone, grant organization membership or authorize token issuing.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal

from octop.infra.errors import ErrorCode, MossworkError

type LoginMethod = Literal["sms", "password", "cas"]


def _method(value: object) -> LoginMethod | None:
    # JavaScript === rejects booleans while accepting numeric 0.0/1.0/2.0.
    if isinstance(value, str):
        if value == "sms":
            return "sms"
        if value == "password":
            return "password"
        if value == "cas":
            return "cas"
        return None
    if type(value) in (int, float):
        if value == 0:
            return "sms"
        if value == 1:
            return "password"
        if value == 2:
            return "cas"
    return None


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-JSON numeric constant")


def _policy(connection: sqlite3.Connection, scope: str, scope_id: str) -> dict[str, object]:
    row = connection.execute(
        "SELECT policy_json FROM client_delivery_policies WHERE scope_type = ? AND scope_id = ?",
        (scope, scope_id),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        return {}
    try:
        value = json.loads(row[0], parse_constant=_reject_json_constant)
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


class SqliteLoginPolicyReader:
    """Read current retained policy in one transaction, using mode=ro only.

    Organization policy precedes platform policy. Explicit inheritance or a
    platform-only query excludes the legacy profile fallback. Missing rows and
    malformed JSON act as empty policy, as in the retained repository. Missing
    consumed tables/schema or I/O is an unavailable dependency, never a new DB.
    """

    def __init__(self, database_path: Path, *, default_method: LoginMethod = "password") -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A trusted local SQLite Path is required")
        if not isinstance(default_method, str) or default_method not in ("sms", "password", "cas"):
            raise ValueError("A controlled valid default login method is required")
        self._uri = database_path.absolute().as_uri() + "?mode=ro"
        self._default = default_method

    def _read(self, org_id: str | None, ignore_organization_policy: bool) -> LoginMethod:
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                return self._effective_in_connection(connection, org_id, ignore_organization_policy)
        except (sqlite3.Error, OSError):
            raise MossworkError.localized(
                ErrorCode.INTERNAL_ERROR,
                status=503,
                details={"dependency": "canonical_login_policy"},
            ) from None

    def _effective_in_connection(
        self, connection: sqlite3.Connection, org_id: str | None, ignore_organization_policy: bool
    ) -> LoginMethod:
        organization = (
            _policy(connection, "organization", org_id)
            if org_id and not ignore_organization_policy
            else {}
        )
        method = _method(organization.get("loginMethod"))
        if method is not None:
            return method
        method = _method(_policy(connection, "platform", "default").get("loginMethod"))
        if method is not None:
            return method
        if ignore_organization_policy or organization.get("loginMethodInherited") is True:
            return self._default
        if org_id:
            profile = connection.execute(
                "SELECT login_method FROM organization_profiles WHERE org_id = ? LIMIT 1", (org_id,)
            ).fetchone()
            # Original profile mapper converts the DB column to String;
            # numeric policy aliases are not legacy profile aliases.
            method = _method(profile[0]) if profile and isinstance(profile[0], str) else None
            if method is not None:
                return method
        return self._default

    def require_sms_in_transaction(self, connection: sqlite3.Connection, org_id: str) -> None:
        """Read current policy using the canonical writer's existing transaction.

        The caller must own BEGIN IMMEDIATE on the original canonical database
        and retain it through signing. The injected reader contributes only
        controlled defaults; its separate read-only path is not used here.
        ``in_transaction`` rejects accidental unguarded use; it does not prove
        writer admission, original-writer drain or an exclusive takeover lease.
        """
        self.require_method_in_transaction(connection, org_id, "sms")

    def require_method_in_transaction(
        self, connection: sqlite3.Connection, org_id: str, required_method: LoginMethod
    ) -> None:
        """Apply the retained effective method in the issuer's existing transaction."""
        if required_method not in {"sms", "password", "cas"} or not connection.in_transaction:
            raise MossworkError.localized(
                ErrorCode.INTERNAL_ERROR,
                status=503,
                details={"dependency": "canonical_login_policy"},
            )
        try:
            method = self._effective_in_connection(connection, org_id, False)
        except (sqlite3.Error, OSError):
            raise MossworkError.localized(
                ErrorCode.INTERNAL_ERROR,
                status=503,
                details={"dependency": "canonical_login_policy"},
            ) from None
        if method != required_method:
            raise MossworkError.localized(
                ErrorCode.FORBIDDEN, details={"required_login_method": required_method}
            )

    async def effective_method(
        self, org_id: str | None = None, *, ignore_organization_policy: bool = False
    ) -> LoginMethod:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._read, org_id, ignore_organization_policy
        )

    async def require_sms(self, org_id: str) -> None:
        """Refuse phone login when current effective organization policy disallows it."""
        if await self.effective_method(org_id) != "sms":
            raise MossworkError.localized(
                ErrorCode.FORBIDDEN, details={"required_login_method": "sms"}
            )


def default_scopes_for_role(role: str) -> tuple[str, ...]:
    """Retained issuer defaults, only after canonical current-role/phone checks.

    This function creates no grant. Credential consumers must retain the
    existing signed capability ceiling; they must never infer expanded scopes
    from a role or call these defaults to authorize an already issued token.
    """
    if role in ("admin", "super_admin"):
        return ("*",)
    if role == "dept_admin":
        return (
            "sessions:create",
            "sessions:attach",
            "sessions:list",
            "admin:users",
            "admin:api_keys",
            "secrets:department:read",
            "secrets:user:write",
            "store:read",
            "store:tenant:write",
            "store:custom:write",
            "cron:self",
            "cron:list:subtree",
            "cron:manage:subtree",
            "admin:triggers",
            "admin:mcp",
            "admin:mcp:write",
            "admin:mcp:audit",
        )
    return (
        "sessions:create",
        "sessions:attach",
        "sessions:list",
        "secrets:user:write",
        "store:read",
        "store:tenant:write",
        "store:custom:write",
        "cron:self",
    )
