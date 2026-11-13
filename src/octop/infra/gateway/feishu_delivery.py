"""Retained Feishu business bookkeeping, without native execution or IM sends.

The schema and keys come from moss@d0b84d4c/src/server/mossImIdentity.ts.
No schema is initialized here. An actual exclusive canonical writer and an
actual native session binding assertion must be injected by trusted composition.
Delivery terminal states are deliberately stricter than the original unrestricted
UPDATE: uncertainty/failure never permits redispatch or a replacement session.
"""

from __future__ import annotations

import asyncio
import math
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, TypeVar, final
from uuid import uuid4

from moss_gateway.models import InboundMessage

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.gateway.canonical_feishu import CanonicalFeishuRoute, CanonicalFeishuRouteService
from octop.infra.gateway.personal_moss_identity import (
    FeishuMemberConnection,
    FeishuMemberMessage,
    verify_feishu_member_message,
)
from octop.infra.identity.canonical import CanonicalSnapshotService, has_signed_scope
from octop.infra.identity.current_human import assert_current_human
from octop.infra.identity.write_authority import CanonicalWriterAdmission, assert_canonical_writer

FeishuDeliveryStatus = Literal["claimed", "dispatching", "completed", "failed", "unknown"]
_TERMINAL = frozenset({"completed", "failed", "unknown"})
_R = TypeVar("_R")


def _refused(status: int = 403) -> MossworkError:
    return MossworkError.localized(
        ErrorCode.NOT_FOUND if status == 404 else ErrorCode.FORBIDDEN, status=status
    )


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR, status=503, details={"dependency": "canonical_feishu_delivery"}
    )


def _session_id(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _refused()
    return value


class FeishuSessionAssertion(Protocol):
    async def assert_session(self, route: CanonicalFeishuRoute, session_id: str) -> None:
        """Assert the real native org/member/ref/chat/session binding or raise.

        Only None means success. This assertion must use the actual native
        authority; it cannot manufacture a session from the supplied route.
        Resource/action/native input authorization remains a separate obligation.
        """
        ...


@final
class CanonicalFeishuClaim:
    """An opaque, immutable receipt issued only for this service's SDK event.

    A receipt is bookkeeping evidence, not a human token or execution permission.
    It is process-local; a duplicate after restart remains occupied in SQLite.
    """

    __slots__ = ("__connection", "__created_at", "__event", "__route", "__seal")
    __connection: FeishuMemberConnection
    __created_at: int
    __event: FeishuMemberMessage
    __route: CanonicalFeishuRoute | None
    __seal: object

    def __new__(cls) -> CanonicalFeishuClaim:
        raise TypeError("Use CanonicalFeishuDeliveryService.claim")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("A Feishu claim is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("A Feishu claim is immutable")

    def __repr__(self) -> str:
        return "CanonicalFeishuClaim()"

    @property
    def route(self) -> CanonicalFeishuRoute | None:
        return self.__route

    @property
    def connection(self) -> FeishuMemberConnection:
        return self.__connection

    @property
    def event(self) -> FeishuMemberMessage:
        return self.__event

    @property
    def created_at(self) -> int:
        return self.__created_at


@dataclass(frozen=True)
class FeishuDeliveryRecord:
    connection_scope: str
    message_id: str
    platform_user_id: str
    chat_id: str
    member_id: str | None
    agent_ref: str | None
    session_id: str | None
    status: FeishuDeliveryStatus
    created_at: int
    updated_at: int


class CanonicalFeishuDeliveryService:
    """Write only the configured canonical channel's original business rows.

    The database, plugin, connection owner, app and authority are server-owned.
    No browser can select them. mode=rw refuses a missing DB. IO runs in an
    executor. Writer admission is mandatory but is not itself writer takeover.
    """

    def __init__(
        self,
        database_path: Path,
        snapshots: CanonicalSnapshotService,
        channel: CanonicalFeishuRouteService,
        *,
        writer: CanonicalWriterAdmission,
        session_assertion: FeishuSessionAssertion,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A trusted existing canonical SQLite Path is required")
        prefix = database_path.absolute().as_uri()
        if channel._uri != prefix + "?mode=ro" or snapshots._store._uri != prefix + "?mode=ro":
            raise ValueError(
                "Route, human verifier and business writer must share the canonical DB"
            )
        if channel._authority_id != snapshots._store.authority_id:
            raise ValueError("Route and human authority must agree")
        if not callable(getattr(session_assertion, "assert_session", None)):
            raise TypeError("A real native session assertion is required")
        self._write_uri = prefix + "?mode=rw"
        self._snapshots, self._channel = snapshots, channel
        self._writer, self._sessions, self._clock = writer, session_assertion, clock
        self._seal = object()

    def _now(self) -> int:
        try:
            now = self._clock()
            if isinstance(now, bool) or not math.isfinite(now) or now < 0:
                raise _unavailable()
            return math.floor(now * 1000)
        except MossworkError:
            raise
        except Exception:
            raise _unavailable() from None

    def _transaction(self, operation: Callable[[sqlite3.Connection], _R]) -> _R:
        try:
            with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                return operation(db)
        except sqlite3.IntegrityError:
            raise _refused(409) from None
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    def _current_route(self, route: CanonicalFeishuRoute) -> None:
        # Reuse the frozen reader's complete membership/Moss/binding policy.
        # In _transaction this separate read occurs before our first write and
        # after BEGIN IMMEDIATE: SQLite prevents every other writer from changing
        # the facts until our commit. No grant policy is duplicated here.
        current = self._channel._read(None, route)
        if current == route:
            return
        if current is not None and replace(current, session_pointer=route.session_pointer) == route:
            raise _refused(409)
        raise _refused()

    async def _check(self, route: CanonicalFeishuRoute) -> None:
        await assert_canonical_writer(self._writer)
        await asyncio.get_running_loop().run_in_executor(None, self._current_route, route)
        await assert_canonical_writer(self._writer)

    async def _assert_session(self, route: CanonicalFeishuRoute, session_id: str) -> None:
        await self._check(route)
        try:
            assertion: Callable[[CanonicalFeishuRoute, str], Awaitable[object]] = (
                self._sessions.assert_session
            )
            result = await assertion(route, session_id)
        except Exception:
            raise _refused() from None
        if result is not None:
            raise _refused()
        await self._check(route)

    def _receipt(
        self,
        connection: FeishuMemberConnection,
        event: FeishuMemberMessage,
        route: CanonicalFeishuRoute | None,
        created_at: int,
    ) -> CanonicalFeishuClaim:
        claim = object.__new__(CanonicalFeishuClaim)
        object.__setattr__(claim, "_CanonicalFeishuClaim__route", route)
        object.__setattr__(claim, "_CanonicalFeishuClaim__connection", connection)
        object.__setattr__(claim, "_CanonicalFeishuClaim__event", event)
        object.__setattr__(claim, "_CanonicalFeishuClaim__created_at", created_at)
        object.__setattr__(claim, "_CanonicalFeishuClaim__seal", self._seal)
        return claim

    def _assert_receipt(self, claim: CanonicalFeishuClaim) -> None:
        if (
            type(claim) is not CanonicalFeishuClaim
            or getattr(claim, "_CanonicalFeishuClaim__seal", None) is not self._seal
        ):
            raise _refused()

    def _route(self, claim: CanonicalFeishuClaim) -> CanonicalFeishuRoute:
        self._assert_receipt(claim)
        if claim.route is None:
            raise _refused()
        return claim.route

    def _delivery_row(self, db: sqlite3.Connection, claim: CanonicalFeishuClaim) -> sqlite3.Row:
        self._assert_receipt(claim)
        route = claim.route
        row: sqlite3.Row | None = db.execute(
            "SELECT * FROM moss_im_deliveries WHERE connection_scope = ? AND message_id = ?",
            (claim.connection.scope, claim.event.message_id),
        ).fetchone()
        if row is None or (
            row["platform_user_id"] != claim.event.sender_id
            or row["chat_id"] != claim.event.chat_id
            or row["created_at"] != claim.created_at
            or row["status"] not in {"claimed", "dispatching", *_TERMINAL}
        ):
            raise _refused(409)
        if row["user_id"] is not None and (
            route is None or row["user_id"] != route.member.owner_id
        ):
            raise _refused(409)
        if row["agent_ref"] is not None and (
            route is None or row["agent_ref"] != route.identity.reference
        ):
            raise _refused(409)
        if (row["user_id"] is None) != (row["agent_ref"] is None) or (
            row["session_id"] is not None and row["user_id"] is None
        ):
            raise _refused(409)
        return row

    async def assert_dispatch_current(
        self, claim: CanonicalFeishuClaim, *, session_id: str
    ) -> None:
        """Recheck this service's queued IM proof before a real dispatch effect.

        Only an already dispatching original delivery with its confirmed current
        pointer may pass. This read-only assertion never resolves native ports,
        credentials or sessions. It is not a resource/model grant or native
        Ready/lease: the backend still applies its own current owner, exact
        operation and native admission checks in the correct effect order.
        Terminal bookkeeping deliberately does not call this effect assertion.
        """
        route = self._route(claim)
        session_id = _session_id(session_id)
        if (
            route.session_pointer != session_id
            or claim.connection != route.connection
            or claim.event != route.event
        ):
            raise _refused(409)

        def current() -> None:
            try:
                with closing(sqlite3.connect(self._channel._uri, uri=True, timeout=1.0)) as db:
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA query_only = ON")
                    db.execute("BEGIN")
                    self._current_route(route)
                    row = self._delivery_row(db, claim)
                    if (
                        row["status"] != "dispatching"
                        or row["user_id"] != route.member.owner_id
                        or row["agent_ref"] != route.identity.reference
                        or row["session_id"] != session_id
                    ):
                        raise _refused(409)
                    self._current_route(route)
            except (sqlite3.Error, OSError):
                raise _unavailable() from None

        await assert_canonical_writer(self._writer)
        await asyncio.get_running_loop().run_in_executor(None, current)
        await assert_canonical_writer(self._writer)
        # Writer admission and both SQLite reads can yield. A final fresh read
        # catches terminal/pointer/route changes made during the last await.
        await asyncio.get_running_loop().run_in_executor(None, current)

    @staticmethod
    def _record(row: sqlite3.Row) -> FeishuDeliveryRecord:
        return FeishuDeliveryRecord(
            row["connection_scope"],
            row["message_id"],
            row["platform_user_id"],
            row["chat_id"],
            row["user_id"],
            row["agent_ref"],
            row["session_id"],
            row["status"],
            row["created_at"],
            row["updated_at"],
        )

    async def configure(self, opaque_access_token: str, tenant_key: str) -> None:
        """Pin this trusted app/plugin/owner using a current same-org human admin.

        Original app/tenant regex and revision-preserving UPSERT are retained.
        Older bindings, challenges, conversations and deliveries are untouched.
        Foreign-org super_admin does not override the same-org requirement.
        """
        if (
            not re.fullmatch(r"cli_[a-zA-Z0-9]+", self._channel._app_id)
            or not isinstance(tenant_key, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", tenant_key)
        ):
            raise _refused(400)
        snapshot = await self._snapshots.snapshot(opaque_access_token)
        if (
            snapshot.auth_app not in {"moss", "sudowork"}
            or snapshot.member.role not in {"admin", "super_admin"}
            or not has_signed_scope(snapshot.scopes, "admin:users")
        ):
            raise _refused()
        await assert_canonical_writer(self._writer)

        def configure(db: sqlite3.Connection) -> None:
            assert_current_human(db, snapshot, self._clock)
            row = db.execute(
                "SELECT p.type FROM channel_plugins p JOIN users u ON u.id = p.user_id "
                "AND u.org_id = p.org_id AND u.status = 'active' "
                "WHERE p.id = ? AND p.user_id = ? AND p.org_id = ?",
                (self._channel._plugin_id, self._channel._owner_id, snapshot.organization_id),
            ).fetchone()
            if row is None or row["type"] != "lark":
                raise _refused(404)
            db.execute(
                """INSERT INTO moss_im_connections
                (plugin_id, connection_owner_id, org_id, app_id, tenant_key, revision,
                 updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (plugin_id, connection_owner_id) DO UPDATE SET
                revision = CASE WHEN moss_im_connections.org_id = excluded.org_id
                  AND moss_im_connections.app_id = excluded.app_id
                  AND moss_im_connections.tenant_key = excluded.tenant_key
                  THEN moss_im_connections.revision ELSE excluded.revision END,
                org_id = excluded.org_id, app_id = excluded.app_id, tenant_key = excluded.tenant_key,
                updated_by = excluded.updated_by, updated_at = excluded.updated_at""",
                (
                    self._channel._plugin_id,
                    self._channel._owner_id,
                    snapshot.organization_id,
                    self._channel._app_id,
                    tenant_key,
                    str(uuid4()),
                    snapshot.member.owner_id,
                    self._now(),
                ),
            )

        await asyncio.get_running_loop().run_in_executor(None, self._transaction, configure)
        await assert_canonical_writer(self._writer)
        if await self._snapshots.snapshot(opaque_access_token) != snapshot:
            raise _refused()
        await assert_canonical_writer(self._writer)

    async def claim(self, message: InboundMessage) -> CanonicalFeishuClaim | None:
        """Occupy one verified original event once; only duplicate returns None.

        A claimed/dispatching/completed/failed/unknown row always stays occupied.
        Failure of a post-write check does not release the claim for replay.
        Unbound senders also receive a receipt, allowing a private challenge
        response once. They cannot set a native pointer or enter dispatching.
        """
        frozen_message = message.model_copy(deep=True)
        await assert_canonical_writer(self._writer)

        def claim(
            db: sqlite3.Connection,
        ) -> tuple[FeishuMemberConnection, FeishuMemberMessage, int] | None:
            connection = self._channel._connection(db)
            event = verify_feishu_member_message(frozen_message, connection)
            now = self._now()
            changed = db.execute(
                """INSERT INTO moss_im_deliveries
                (connection_scope, message_id, platform_user_id, chat_id, status, created_at,
                 updated_at) VALUES (?, ?, ?, ?, 'claimed', ?, ?)
                ON CONFLICT (connection_scope, message_id) DO NOTHING""",
                (
                    connection.scope,
                    event.message_id,
                    event.sender_id,
                    event.chat_id,
                    now,
                    now,
                ),
            ).rowcount
            return (connection, event, now) if changed == 1 else None

        result = await asyncio.get_running_loop().run_in_executor(None, self._transaction, claim)
        await assert_canonical_writer(self._writer)
        if result is None:
            return None
        connection, event, created = result
        route = await self._channel.resolve(frozen_message)
        if route is not None and (route.connection != connection or route.event != event):
            raise _refused()

        def current(db: sqlite3.Connection) -> None:
            if self._channel._connection(db) != connection:
                raise _refused()
            if route is not None:
                self._current_route(route)

        await assert_canonical_writer(self._writer)
        await asyncio.get_running_loop().run_in_executor(None, self._transaction, current)
        await assert_canonical_writer(self._writer)
        return self._receipt(connection, event, route, created)

    async def set_pointer(
        self, claim: CanonicalFeishuClaim, session_id: str
    ) -> CanonicalFeishuClaim:
        """CAS the original conversation tuple; no native input is sent here.

        previous comes only from the resolved route. The returned receipt holds
        the confirmed new pointer; the old receipt becomes stale. A delivery
        already associated with a native session cannot select a new session.
        """
        route = self._route(claim)
        session_id = _session_id(session_id)
        await self._assert_session(route, session_id)

        def set_pointer(db: sqlite3.Connection) -> None:
            self._current_route(route)
            row = self._delivery_row(db, claim)
            if row["status"] != "claimed" or row["session_id"] is not None:
                raise _refused(409)
            args = (
                route.conversation_key,
                route.member.organization_id,
                route.member.owner_id,
                route.identity.reference,
                route.connection.scope,
                route.event.chat_id,
            )
            if route.session_pointer is None:
                changed = db.execute(
                    """INSERT INTO moss_im_conversations (conversation_key, org_id, user_id,
                    agent_ref, connection_scope, chat_id, session_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (conversation_key) DO NOTHING""",
                    (*args, session_id, self._now()),
                ).rowcount
            else:
                changed = db.execute(
                    """UPDATE moss_im_conversations SET session_id = ?, updated_at = ?
                    WHERE conversation_key = ? AND org_id = ? AND user_id = ? AND agent_ref = ?
                    AND connection_scope = ? AND chat_id = ? AND session_id = ?""",
                    (session_id, self._now(), *args, route.session_pointer),
                ).rowcount
            if changed != 1:
                raise _refused(409)

        await asyncio.get_running_loop().run_in_executor(None, self._transaction, set_pointer)
        updated = replace(route, session_pointer=session_id)
        await self._assert_session(updated, session_id)
        return self._receipt(claim.connection, claim.event, updated, claim.created_at)

    async def delivery(
        self,
        claim: CanonicalFeishuClaim,
        status: Literal["dispatching", "completed", "failed", "unknown"],
        *,
        session_id: str | None = None,
    ) -> FeishuDeliveryRecord:
        """Record the trusted consumer's actual outcome, keeping owner/session.

        Entering dispatching requires a confirmed pointer and native binding.
        The consumer must use native completion evidence for completed; queue
        acceptance, an IM ACK or a transport response is not completion proof.
        Terminal accounting needs only the original durable event/association
        and current writer. Revocation, unlink or native lookup uncertainty must
        not strand an already admitted effect in dispatching. Terminal rows
        cannot dispatch again, including after uncertainty.
        """
        self._assert_receipt(claim)
        route = claim.route
        if status not in {"dispatching", "completed", "failed", "unknown"}:
            raise _refused()
        if session_id is not None:
            session_id = _session_id(session_id)
        if status == "dispatching":
            route = self._route(claim)
            if session_id is None or session_id != route.session_pointer:
                raise _refused(409)
            await self._assert_session(route, session_id)
        else:
            await assert_canonical_writer(self._writer)

        def delivery(db: sqlite3.Connection) -> FeishuDeliveryRecord:
            if status == "dispatching":
                assert route is not None
                self._current_route(route)
            row = self._delivery_row(db, claim)
            previous = row["status"]
            if (
                (previous in _TERMINAL and previous != status)
                or (status == "dispatching" and previous != "claimed")
                or (previous == "claimed" and status == "completed" and route is not None)
                or (
                    session_id is not None
                    and status != "dispatching"
                    and row["session_id"] != session_id
                )
            ):
                raise _refused(409)
            if row["session_id"] is not None and (
                route is None or route.session_pointer != row["session_id"]
            ):
                raise _refused(409)
            now = self._now()
            if status == "dispatching":
                assert route is not None
                if row["session_id"] is not None and row["session_id"] != session_id:
                    raise _refused(409)
                db.execute(
                    """UPDATE moss_im_deliveries SET user_id = ?, agent_ref = ?, session_id = ?
                    WHERE connection_scope = ? AND message_id = ?""",
                    (
                        route.member.owner_id,
                        route.identity.reference,
                        session_id,
                        claim.connection.scope,
                        claim.event.message_id,
                    ),
                )
            db.execute(
                "UPDATE moss_im_deliveries SET status = ?, updated_at = ? "
                "WHERE connection_scope = ? AND message_id = ?",
                (status, now, claim.connection.scope, claim.event.message_id),
            )
            return self._record(self._delivery_row(db, claim))

        result = await asyncio.get_running_loop().run_in_executor(None, self._transaction, delivery)
        if status == "dispatching":
            assert route is not None and session_id is not None
            await self._assert_session(route, session_id)
        else:
            await assert_canonical_writer(self._writer)
        return result
