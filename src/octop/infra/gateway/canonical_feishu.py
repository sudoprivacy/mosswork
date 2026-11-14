"""Resolve registered Feishu senders against the retained canonical authority.

This read-only adapter consumes the original channel pins and human bindings;
it never creates a human login, grants scopes, links a sender or claims delivery.
Only an authenticated, server-owned Feishu channel may call it. Native dispatch
and reply delivery must still consume current grants and their original fences.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from moss_gateway.models import InboundMessage

from octop.infra.agents.personal_moss_access import (
    MossMemberSnapshot,
    require_personal_moss_access,
)
from octop.infra.agents.personal_moss_identity import PersonalMossIdentity
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.gateway.personal_moss_identity import (
    FeishuMemberConnection,
    FeishuMemberMessage,
    personal_moss_conversation_key,
    verify_feishu_member_message,
)


def _denied() -> MossworkError:
    return MossworkError.localized(ErrorCode.FORBIDDEN, status=403)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _denied()
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _denied()
    return value


@dataclass(frozen=True, repr=False)
class CanonicalFeishuRoute:
    """Current binding facts; neither a JWT nor a native execution permission."""

    authority_id: str
    account_id: str
    member: MossMemberSnapshot
    membership_revision: int
    grant_updated_at: int | None
    identity: PersonalMossIdentity
    binding_id: str
    connection: FeishuMemberConnection
    event: FeishuMemberMessage
    conversation_key: str
    session_pointer: str | None


class CanonicalFeishuRouteService:
    """Read one server-pinned channel, without request-selected DB or provider.

    The channel projection is explicit. Its canonical plugin/connection owner,
    actual app ID and authority are supplied by trusted composition. Registered
    tenant, revision and org are read afresh, including on assert_current.
    A missing human binding returns None; an invalid or revoked one refuses.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        authority_id: str,
        channel_id: str,
        plugin_id: str,
        connection_owner_id: str,
        actual_app_id: str,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A local canonical SQLite Path is required")
        self._uri = database_path.absolute().as_uri() + "?mode=ro"
        self._authority_id = _text(authority_id)
        self._channel_id = _text(channel_id)
        self._plugin_id = _text(plugin_id)
        self._owner_id = _text(connection_owner_id)
        self._app_id = _text(actual_app_id)
        if not self._app_id.startswith("cli_"):
            raise _denied()

    async def resolve(self, message: InboundMessage) -> CanonicalFeishuRoute | None:
        """Resolve only trusted SDK evidence, freezing it before asynchronous IO."""
        copied = message.model_copy(deep=True)
        return await asyncio.get_running_loop().run_in_executor(None, self._read, copied, None)

    async def assert_current(self, route: CanonicalFeishuRoute) -> None:
        """Refuse changed identity/connection/grant/pointer; never select a new session."""
        if not isinstance(route, CanonicalFeishuRoute) or route.authority_id != self._authority_id:
            raise _denied()
        current = await asyncio.get_running_loop().run_in_executor(None, self._read, None, route)
        if current != route:
            raise _denied()

    def _connection(self, db: sqlite3.Connection) -> FeishuMemberConnection:
        row = db.execute(
            """SELECT c.org_id, c.app_id, c.tenant_key, c.revision,
                p.org_id AS plugin_org, p.type, p.enabled,
                u.org_id AS owner_org, u.status AS owner_status
            FROM moss_im_connections c
            JOIN channel_plugins p ON p.id = c.plugin_id AND p.user_id = c.connection_owner_id
            JOIN users u ON u.id = c.connection_owner_id
            WHERE c.plugin_id = ? AND c.connection_owner_id = ?""",
            (self._plugin_id, self._owner_id),
        ).fetchone()
        if (
            row is None
            or row["org_id"] != row["plugin_org"]
            or row["org_id"] != row["owner_org"]
            or row["owner_status"] != "active"
            or row["type"] != "lark"
            or type(row["enabled"]) is not int
            or row["enabled"] != 1
            or row["app_id"] != self._app_id
        ):
            raise _denied()
        return FeishuMemberConnection(
            _text(row["org_id"]),
            self._owner_id,
            self._plugin_id,
            _text(row["tenant_key"]),
            self._app_id,
            _text(row["revision"]),
            self._channel_id,
        )

    def _read(
        self, message: InboundMessage | None, previous: CanonicalFeishuRoute | None
    ) -> CanonicalFeishuRoute | None:
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only = ON")
                db.execute("BEGIN")
                connection = self._connection(db)
                if previous is not None:
                    if previous.connection != connection:
                        raise _denied()
                    event = previous.event
                else:
                    if message is None:
                        raise _denied()
                    event = verify_feishu_member_message(message, connection)
                binding = db.execute(
                    """SELECT id, org_id, user_id FROM user_auth_identities
                    WHERE provider = 'feishu-moss' AND issuer = ? AND normalized_subject = ?""",
                    (connection.scope, event.sender_id),
                ).fetchone()
                if binding is None:
                    return None
                if binding["org_id"] != connection.organization_id:
                    raise _denied()
                row = db.execute(
                    """SELECT a.id AS account_id, a.status AS account_status,
                        u.id, u.org_id, u.role, u.status, u.membership_revision,
                        g.user_id AS grant_member_id, g.enabled, g.updated_at
                    FROM human_account_memberships m
                    JOIN human_accounts a ON a.id = m.account_id
                    JOIN users u ON u.id = m.user_id AND u.org_id = m.org_id
                    LEFT JOIN moss_member_access g ON g.org_id = m.org_id AND g.user_id = m.user_id
                    WHERE m.org_id = ? AND m.user_id = ?""",
                    (connection.organization_id, binding["user_id"]),
                ).fetchone()
                if row is None or row["account_status"] != "active":
                    raise _denied()
                member = MossMemberSnapshot(
                    _text(row["org_id"]), _text(row["id"]), _text(row["role"]), _text(row["status"])
                )
                explicit: bool | None = None
                updated: int | None = None
                if row["grant_member_id"] is not None:
                    enabled = _integer(row["enabled"])
                    if enabled not in (0, 1):
                        raise _denied()
                    explicit = enabled == 1
                    updated = _integer(row["updated_at"])
                identity = require_personal_moss_access(
                    member,
                    organization_id=connection.organization_id,
                    owner_id=member.owner_id,
                    explicit_grant=explicit,
                )
                key = personal_moss_conversation_key(connection, identity, event.chat_id)
                pointer = db.execute(
                    "SELECT * FROM moss_im_conversations WHERE conversation_key = ?", (key,)
                ).fetchone()
                session: str | None = None
                if pointer is not None:
                    if any(
                        pointer[field] != value
                        for field, value in (
                            ("org_id", member.organization_id),
                            ("user_id", member.owner_id),
                            ("agent_ref", identity.reference),
                            ("connection_scope", connection.scope),
                            ("chat_id", event.chat_id),
                        )
                    ):
                        raise _denied()
                    session = _text(pointer["session_id"])
                return CanonicalFeishuRoute(
                    self._authority_id,
                    _text(row["account_id"]),
                    member,
                    _integer(row["membership_revision"]),
                    updated,
                    identity,
                    _text(binding["id"]),
                    connection,
                    event,
                    key,
                    session,
                )
        except (sqlite3.Error, OSError) as exc:
            raise MossworkError.localized(
                ErrorCode.INTERNAL_ERROR,
                status=503,
                details={"dependency": "canonical_feishu_identity_store"},
            ) from exc
