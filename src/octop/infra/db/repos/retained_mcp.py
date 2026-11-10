"""Read original reviewed MCP registration, policy and member visibility.

Ports Moss d0b84d4c ReviewedMcpAccess/currentMcpMember against its existing
SQLite tables. This reader neither initializes the store nor grants a graph,
task, native operation, secret capability or authenticated human identity.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import rfc8785


class ReviewedMcpUnavailable(PermissionError):
    """The original review, policy, member or credential binding refuses."""


def _deny() -> ReviewedMcpUnavailable:
    return ReviewedMcpUnavailable("Original reviewed MCP access is unavailable")


def _digest(value: Any) -> str:
    try:
        return "sha256:" + hashlib.sha256(rfc8785.dumps(value)).hexdigest()
    except (ValueError, TypeError):
        raise _deny() from None


def _text(value: object) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise _deny()
    return value


def _json(value: object) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise _deny()
            result[key] = item
        return result

    if type(value) is not str:
        raise _deny()
    try:
        return json.loads(value, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError):
        raise _deny() from None


def _strings(value: object) -> list[str]:
    if type(value) is not list or any(type(v) is not str for v in value):
        raise _deny()
    return value


def _visible(raw: object, member: sqlite3.Row, ancestors: set[str]) -> bool:
    if raw is None:
        return True
    value = _json(raw)
    if value is None:
        return True
    if type(value) is not dict or set(value) - {"user_ids", "role_ids", "department_ids"}:
        raise _deny()
    for key, matches in (
        ("user_ids", {member["member_id"]}),
        ("role_ids", {member["role"]}),
        ("department_ids", ancestors),
    ):
        if value.get(key) is not None:
            allowed = _strings(value[key])
            # Original order matters: an empty earlier whitelist refuses even
            # when a later role or department would match. Admin never expands it.
            if not allowed:
                return False
            if matches.intersection(allowed):
                return True
    return False


@dataclass(frozen=True, slots=True, repr=False)
class RetainedReviewedMcp:
    organization_id: str
    member_id: str
    worker_ref: str
    tool_name: str
    endpoint: str
    timeout_ms: int
    auth_type: str
    secret_ref: str | None
    auth_config_json: str | None = field(repr=False)
    review_document: str = field(repr=False)
    fingerprint: str = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class RetainedMcpSecretDefinition:
    organization_id: str
    pinyin: str
    scheme: str
    bearer_prefix: str | None
    entries: tuple[str, ...]
    fingerprint: str


class RetainedReviewedMcpRepo:
    """One read transaction over original rows; async callers must offload it.

    The supplied member is the original authenticated actor, never a target
    owner or Product integer. Current human/full-model/native admission stays
    mandatory in the provider around this lookup and every external wait.
    """

    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path) or not database_path.is_absolute():
            raise ValueError("Require an absolute original SQLite path")
        self._uri = database_path.as_uri() + "?mode=ro"

    def resolve(
        self, organization_id: str, member_id: str, worker_ref: str, tool_name: str
    ) -> RetainedReviewedMcp:
        for value in (organization_id, member_id, worker_ref, tool_name):
            _text(value)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", tool_name):
            raise _deny()
        resource = f"mcp:{worker_ref}/{tool_name}"
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only = ON")
                db.execute("BEGIN")
                server = db.execute(
                    "SELECT * FROM mcp_servers WHERE org_id = ? AND id = ?",
                    (organization_id, worker_ref),
                ).fetchone()
                policy = db.execute(
                    "SELECT * FROM mcp_policies WHERE org_id = ?", (organization_id,)
                ).fetchone()
                review = db.execute(
                    "SELECT * FROM agent_tool_reviews WHERE org_id = ? AND resource = ?",
                    (organization_id, resource),
                ).fetchone()
                members = db.execute(
                    """SELECT u.id AS member_id, u.role, u.department_id, u.membership_revision,
                        a.id AS account_id FROM users u
                    JOIN human_account_memberships m ON m.org_id = u.org_id AND m.user_id = u.id
                    JOIN human_accounts a ON a.id = m.account_id
                    WHERE u.org_id = ? AND u.id = ? AND u.status = 'active'
                        AND a.status = 'active' LIMIT 2""",
                    (organization_id, member_id),
                ).fetchall()
                if server is None or policy is None or review is None or len(members) != 1:
                    raise _deny()
                member = members[0]
                departments = db.execute(
                    "SELECT id, parent_id FROM departments WHERE org_id = ?", (organization_id,)
                ).fetchall()
                by_id = {row["id"]: row["parent_id"] for row in departments}
                ancestors: set[str] = set()
                current = member["department_id"]
                while current in by_id:
                    if current in ancestors:
                        raise _deny()
                    ancestors.add(current)
                    current = by_id[current]
                return self._checked(
                    organization_id,
                    member_id,
                    worker_ref,
                    tool_name,
                    resource,
                    server,
                    policy,
                    review,
                    member,
                    departments,
                    ancestors,
                )
        except (sqlite3.Error, OSError, UnicodeError, KeyError, IndexError):
            raise _deny() from None

    @staticmethod
    def _checked(
        org: str,
        actor: str,
        worker: str,
        tool: str,
        resource: str,
        server: sqlite3.Row,
        policy: sqlite3.Row,
        row: sqlite3.Row,
        member: sqlite3.Row,
        departments: list[sqlite3.Row],
        ancestors: set[str],
    ) -> RetainedReviewedMcp:
        if (
            server["status"] != "enabled"
            or server["enabled"] != 1
            or server["scope"] not in ("org", "department")
            or server["mcp_type"] != "http"
            or server["use_proxy"] != 0
            or policy["allow_http_sse_mcp"] != 1
            or policy["allow_external_network"] != 1
            or server["auth_type"]
            not in ("none", "bearer", "basic", "api_key", "custom_header", "secret_ref")
        ):
            raise _deny()
        endpoint = _text(server["url"])
        try:
            target = urlsplit(endpoint)
            if (
                target.scheme not in ("http", "https")
                or not target.hostname
                or target.username is not None
                or target.password is not None
                or target.fragment
                or target.hostname not in _strings(_json(policy["domain_whitelist_json"]))
            ):
                raise _deny()
        except ValueError:
            raise _deny() from None
        document = _json(row["document"])
        if type(document) is not dict:
            raise _deny()
        descriptor = document.get("tool")
        if (
            document.get("enabled") is not True
            or document.get("orgId") != org
            or document.get("serverId") != worker
            or row["server_id"] != worker
            or document.get("resource") != resource
            or document.get("revision") != row["revision"]
            or not row["revision"]
            or type(descriptor) is not dict
            or descriptor.get("name") != tool
            or type(document.get("protocol")) is not str
            or document.get("digest")
            != _digest({"protocol": document["protocol"], "tool": descriptor})
            or document.get("serverDigest")
            != _digest(
                {
                    "id": worker,
                    "org": org,
                    "type": server["mcp_type"],
                    "endpoint": endpoint,
                    "scope": server["scope"],
                    "owner": server["owner_id"],
                }
            )
        ):
            raise _deny()
        action = document.get("action")
        if (
            action not in ("read", "write")
            or server[f"allow_{action}"] != 1
            or type(document.get("sensitive")) is not bool
            or type(document.get("outbound")) is not bool
            or document["sensitive"]
            and server["allow_read_sensitive_fields"] != 1
            or document["outbound"]
            and server["allow_outbound_network"] != 1
            or action == "write"
            and (
                server["require_confirmation_for_write"] != 0
                or policy["require_confirmation_for_write"] != 0
            )
            or server["risk_level"] == "high"
            and policy["require_confirmation_for_high_risk"] != 0
            or server["scope"] == "department"
            and server["owner_id"] != member["department_id"]
            or not _visible(server["visible_to"], member, ancestors)
        ):
            raise _deny()
        # This consumer has no task asset/skill binding proof. Preserve the
        # original host-read refusal rather than silently ignoring those rows.
        if any(
            server[key] is not None and _json(server[key]) is not None
            for key in ("bound_assistants", "bound_skills")
        ):
            raise _deny()
        timeout = server["timeout_ms"]
        if type(timeout) is not int:
            raise _deny()
        fingerprint = _digest(
            {
                "server": dict(server),
                "policy": dict(policy),
                "review": dict(row),
                "member": dict(member),
                "departments": sorted((dict(r) for r in departments), key=lambda r: r["id"]),
            }
        )
        return RetainedReviewedMcp(
            org,
            actor,
            worker,
            tool,
            endpoint,
            min(max(timeout, 1000), 15000),
            server["auth_type"],
            server["secret_ref"],
            server["auth_config_json"],
            row["document"],
            fingerprint,
        )

    def assert_current(self, expected: RetainedReviewedMcp) -> None:
        if (
            type(expected) is not RetainedReviewedMcp
            or self.resolve(
                expected.organization_id,
                expected.member_id,
                expected.worker_ref,
                expected.tool_name,
            )
            != expected
        ):
            raise _deny()

    def secret_definition(
        self, organization_id: str, secret_ref: str
    ) -> RetainedMcpSecretDefinition:
        """Original system ConfigItem only; no value, user or deployment fallback."""
        _text(organization_id)
        pinyin = _text(secret_ref).rsplit(":", 1)[-1]
        if not re.fullmatch(r"[a-z0-9_-]+", pinyin):
            raise _deny()
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only = ON")
                db.execute("BEGIN")
                rows = db.execute(
                    "SELECT * FROM config_items WHERE org_id = ? AND pinyin = ? AND scope = 'system' AND status = 1 LIMIT 2",
                    (organization_id, pinyin),
                ).fetchall()
                if len(rows) != 1 or rows[0]["scheme"] not in ("bearer", "basic", "header"):
                    raise _deny()
                item = rows[0]
                entries = db.execute(
                    "SELECT * FROM config_entries WHERE config_item_id = ?", (item["id"],)
                ).fetchall()
                keys = tuple(_text(entry["config_key"]) for entry in entries)
                if not keys or len(set(keys)) != len(keys):
                    raise _deny()
                if item["bearer_prefix"] is not None and type(item["bearer_prefix"]) is not str:
                    raise _deny()
                return RetainedMcpSecretDefinition(
                    organization_id,
                    pinyin,
                    item["scheme"],
                    item["bearer_prefix"],
                    keys,
                    _digest({"item": dict(item), "entries": [dict(entry) for entry in entries]}),
                )
        except (sqlite3.Error, OSError, UnicodeError, KeyError, IndexError):
            raise _deny() from None
