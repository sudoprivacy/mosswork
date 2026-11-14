"""Retained one-time human Feishu binding in the canonical identity database.

Only the configured authenticated SDK channel may issue a challenge. Its
private code proves control of that sender; the normal human access token
proves the account. Neither proof creates a native session or a model grant.
All mutations require the actual exclusive canonical-writer provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from moss_gateway.models import InboundMessage

from octop.infra.agents.personal_moss_access import require_personal_moss_access
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.gateway.canonical_feishu import CanonicalFeishuRouteService
from octop.infra.gateway.personal_moss_identity import verify_feishu_member_message
from octop.infra.identity.canonical import CanonicalIdentitySnapshot, CanonicalSnapshotService
from octop.infra.identity.current_human import assert_current_human
from octop.infra.identity.write_authority import CanonicalWriterAdmission, assert_canonical_writer

_PROVIDER = "feishu-moss"


def _refused(status: int = 403) -> MossworkError:
    return MossworkError.localized(
        ErrorCode.NOT_FOUND if status == 404 else ErrorCode.FORBIDDEN, status=status
    )


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR, status=503, details={"dependency": "canonical_feishu_binding"}
    )


@dataclass(frozen=True)
class FeishuHumanBinding:
    id: str
    sender_id: str
    plugin_id: str
    app_id: str
    is_current: bool


class CanonicalFeishuBindingService:
    """Use the original tables and one configured channel, without schema init.

    The channel verifier and snapshot authority come from trusted composition.
    Browser callers can supply only their token and the private binding code.
    The channel's owner is distinct from the human member being linked.
    """

    def __init__(
        self,
        database_path: Path,
        snapshots: CanonicalSnapshotService,
        channel: CanonicalFeishuRouteService,
        *,
        writer: CanonicalWriterAdmission,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A trusted existing canonical SQLite Path is required")
        prefix = database_path.absolute().as_uri()
        if channel._uri != prefix + "?mode=ro":
            raise ValueError("The binding writer must use the configured channel's canonical store")
        if snapshots._store._uri != prefix + "?mode=ro":
            raise ValueError(
                "The human verifier and binding writer must use the same canonical store"
            )
        if channel._authority_id != snapshots._store.authority_id:
            raise ValueError("The binding channel and human authority must agree")
        self._read_uri, self._write_uri = prefix + "?mode=ro", prefix + "?mode=rw"
        self._snapshots, self._channel = snapshots, channel
        self._writer, self._clock = writer, clock

    def _current_human(
        self, db: sqlite3.Connection, snapshot: CanonicalIdentitySnapshot, *, require_moss: bool
    ) -> None:
        assert_current_human(db, snapshot, self._clock)
        if require_moss:
            row = db.execute(
                "SELECT enabled FROM moss_member_access WHERE org_id = ? AND user_id = ?",
                (snapshot.organization_id, snapshot.member.owner_id),
            ).fetchone()
            grant: bool | None = None
            if row is not None:
                if type(row["enabled"]) is not int or row["enabled"] not in (0, 1):
                    raise _unavailable()
                grant = bool(row["enabled"])
            require_personal_moss_access(
                snapshot.member,
                organization_id=snapshot.organization_id,
                owner_id=snapshot.member.owner_id,
                explicit_grant=grant,
            )

    async def issue_challenge(self, message: InboundMessage) -> str | None:
        """Return a private one-time code only to the authenticated SDK consumer.

        The consumer must deliver it privately to the verified sender, never
        into a group or log. A cooldown returns None without a second code.
        """
        frozen_message = message.model_copy(deep=True)
        await assert_canonical_writer(self._writer)

        def issue() -> str | None:
            try:
                with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                    db.row_factory = sqlite3.Row
                    db.execute("BEGIN IMMEDIATE")
                    connection = self._channel._connection(db)
                    event = verify_feishu_member_message(frozen_message, connection)
                    now = math.floor(self._clock() * 1000)
                    recent = db.execute(
                        "SELECT digest FROM moss_im_link_challenges WHERE connection_scope = ? "
                        "AND platform_user_id = ? AND created_at > ? LIMIT 1",
                        (connection.scope, event.sender_id, now - 60_000),
                    ).fetchone()
                    if recent:
                        return None
                    code = secrets.token_urlsafe(24)
                    db.execute(
                        """INSERT INTO moss_im_link_challenges
                        (digest, org_id, connection_scope, plugin_id, connection_owner_id,
                         platform_user_id, expires_at, claimed_by, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                        (
                            hashlib.sha256(code.encode("ascii")).hexdigest(),
                            connection.organization_id,
                            connection.scope,
                            connection.plugin_id,
                            connection.owner_id,
                            event.sender_id,
                            now + 600_000,
                            now,
                        ),
                    )
                    return code
            except (sqlite3.Error, OSError):
                raise _unavailable() from None

        code = await asyncio.get_running_loop().run_in_executor(None, issue)
        await assert_canonical_writer(self._writer)
        return code

    async def bind(self, opaque_access_token: str, private_code: str) -> None:
        snapshot = await self._snapshots.snapshot(opaque_access_token)
        if not isinstance(private_code, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{32}", private_code
        ):
            raise _refused()
        await assert_canonical_writer(self._writer)

        def claim_and_bind() -> None:
            try:
                with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                    db.row_factory = sqlite3.Row
                    db.execute("BEGIN IMMEDIATE")
                    self._current_human(db, snapshot, require_moss=True)
                    digest = hashlib.sha256(private_code.encode("ascii")).hexdigest()
                    proof = db.execute(
                        "SELECT * FROM moss_im_link_challenges WHERE digest = ?", (digest,)
                    ).fetchone()
                    now = math.floor(self._clock() * 1000)
                    if (
                        proof is None
                        or proof["org_id"] != snapshot.organization_id
                        or proof["claimed_by"]
                        or proof["expires_at"] <= now
                    ):
                        raise _refused()
                    connection = self._channel._connection(db)
                    if (
                        connection.scope != proof["connection_scope"]
                        or connection.plugin_id != proof["plugin_id"]
                        or connection.owner_id != proof["connection_owner_id"]
                        or connection.organization_id != snapshot.organization_id
                    ):
                        raise _refused()
                    linked = db.execute(
                        "SELECT user_id FROM user_auth_identities "
                        "WHERE provider = ? AND issuer = ? AND normalized_subject = ?",
                        (_PROVIDER, connection.scope, proof["platform_user_id"]),
                    ).fetchone()
                    own = db.execute(
                        "SELECT normalized_subject FROM user_auth_identities "
                        "WHERE user_id = ? AND provider = ? AND issuer = ?",
                        (snapshot.member.owner_id, _PROVIDER, connection.scope),
                    ).fetchone()
                    if (linked is not None and linked["user_id"] != snapshot.member.owner_id) or (
                        own is not None and own["normalized_subject"] != proof["platform_user_id"]
                    ):
                        raise _refused(409)
                    changed = db.execute(
                        "UPDATE moss_im_link_challenges SET claimed_by = ? "
                        "WHERE digest = ? AND claimed_by IS NULL AND expires_at > ?",
                        (snapshot.member.owner_id, digest, now),
                    ).rowcount
                    if changed != 1:
                        raise _refused(409)
                    if linked is None:
                        db.execute(
                            """INSERT INTO user_auth_identities
                            (id, org_id, user_id, provider, issuer, normalized_subject,
                             metadata_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                str(uuid4()),
                                snapshot.organization_id,
                                snapshot.member.owner_id,
                                _PROVIDER,
                                connection.scope,
                                proof["platform_user_id"],
                                json.dumps(
                                    {
                                        "pluginId": connection.plugin_id,
                                        "connectionOwnerId": connection.owner_id,
                                        "appId": connection.app_id,
                                    },
                                    separators=(",", ":"),
                                ),
                                now,
                                now,
                            ),
                        )
            except sqlite3.IntegrityError:
                raise _refused(409) from None
            except (sqlite3.Error, OSError):
                raise _unavailable() from None

        await asyncio.get_running_loop().run_in_executor(None, claim_and_bind)
        await assert_canonical_writer(self._writer)

    async def bindings(self, opaque_access_token: str) -> tuple[FeishuHumanBinding, ...]:
        """List this member's configured-bot bindings, including older revisions."""
        snapshot = await self._snapshots.snapshot(opaque_access_token)

        def read() -> tuple[FeishuHumanBinding, ...]:
            try:
                with closing(sqlite3.connect(self._read_uri, uri=True, timeout=1.0)) as db:
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA query_only = ON")
                    db.execute("BEGIN")
                    self._current_human(db, snapshot, require_moss=False)
                    try:
                        current_scope: str | None = self._channel._connection(db).scope
                    except MossworkError:
                        current_scope = None
                    rows = db.execute(
                        "SELECT id, issuer, normalized_subject, metadata_json "
                        "FROM user_auth_identities WHERE user_id = ? AND org_id = ? AND provider = ? "
                        "ORDER BY created_at, id",
                        (snapshot.member.owner_id, snapshot.organization_id, _PROVIDER),
                    ).fetchall()
                    result = []
                    for row in rows:
                        metadata = json.loads(row["metadata_json"])
                        if not isinstance(metadata, dict):
                            raise _unavailable()
                        if (
                            metadata.get("pluginId") != self._channel._plugin_id
                            or metadata.get("connectionOwnerId") != self._channel._owner_id
                        ):
                            continue
                        result.append(
                            FeishuHumanBinding(
                                row["id"],
                                row["normalized_subject"],
                                str(metadata.get("pluginId", "")),
                                str(metadata.get("appId", "")),
                                row["issuer"] == current_scope,
                            )
                        )
                    return tuple(result)
            except (sqlite3.Error, OSError, ValueError):
                raise _unavailable() from None

        result = await asyncio.get_running_loop().run_in_executor(None, read)
        if await self._snapshots.snapshot(opaque_access_token) != snapshot:
            raise _refused()
        return result

    async def unlink(self, opaque_access_token: str, binding_id: str) -> None:
        snapshot = await self._snapshots.snapshot(opaque_access_token)
        if not isinstance(binding_id, str) or not binding_id.strip():
            raise _refused(404)
        await assert_canonical_writer(self._writer)

        def delete() -> None:
            try:
                with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                    db.row_factory = sqlite3.Row
                    db.execute("BEGIN IMMEDIATE")
                    self._current_human(db, snapshot, require_moss=False)
                    changed = db.execute(
                        "DELETE FROM user_auth_identities "
                        "WHERE id = ? AND user_id = ? AND org_id = ? AND provider = ?",
                        (binding_id, snapshot.member.owner_id, snapshot.organization_id, _PROVIDER),
                    ).rowcount
                    if changed != 1:
                        raise _refused(404)
            except (sqlite3.Error, OSError):
                raise _unavailable() from None

        await asyncio.get_running_loop().run_in_executor(None, delete)
        await assert_canonical_writer(self._writer)
