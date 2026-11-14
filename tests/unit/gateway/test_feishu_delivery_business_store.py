"""Original Feishu SQLite business keys with actual SDK and signed human proofs."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from moss_gateway.models import InboundMessage
from tests.unit.gateway.test_canonical_feishu import _IM_SCHEMA, _SECOND_ACCOUNT, _SECOND_MEMBER
from tests.unit.gateway.test_personal_moss_im_identity import _normalized_message
from tests.unit.identity.test_canonical_human_request_projection import (
    _IDS,
    _NOW,
    _snapshots,
    _token,
)
from tests.unit.identity.test_human_tokens import legacy_db as legacy_db

from octop.infra.errors import MossworkError
from octop.infra.gateway.canonical_feishu import CanonicalFeishuRoute, CanonicalFeishuRouteService
from octop.infra.gateway.feishu_delivery import (
    CanonicalFeishuClaim,
    CanonicalFeishuDeliveryService,
)
from octop.infra.gateway.personal_moss_identity import FeishuMemberConnection

# Exact remaining DDL from moss@d0b84d4c/src/server/mossImIdentity.ts:20-39.
# This is fixture setup only; the service has no initializer/migration.
_DELIVERY_SCHEMA = """
CREATE TABLE moss_im_link_challenges (
    digest TEXT PRIMARY KEY, org_id TEXT NOT NULL, connection_scope TEXT NOT NULL,
    plugin_id TEXT NOT NULL, connection_owner_id TEXT NOT NULL, platform_user_id TEXT NOT NULL,
    expires_at BIGINT NOT NULL, claimed_by TEXT, created_at BIGINT NOT NULL
);
CREATE INDEX moss_im_challenge_sender_idx
    ON moss_im_link_challenges (connection_scope, platform_user_id, created_at);
CREATE TABLE moss_im_deliveries (
    connection_scope TEXT NOT NULL, message_id TEXT NOT NULL, platform_user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL, user_id TEXT, agent_ref TEXT, session_id TEXT,
    status TEXT NOT NULL, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
    PRIMARY KEY (connection_scope, message_id)
);
"""


class _Writer:
    def __init__(self) -> None:
        self.calls = 0
        self.hook: Callable[[], None] | None = None
        self.result: Any = None
        self.is_available = True

    async def assert_current_writer(self) -> Any:
        self.calls += 1
        if self.hook is not None:
            hook, self.hook = self.hook, None
            hook()
        if not self.is_available:
            raise RuntimeError("Fixture writer ownership lost")
        return self.result


class _Sessions:
    """Test native authority with explicit tuples; never a default success."""

    def __init__(self) -> None:
        self.bindings: dict[str, tuple[str, str, str, str]] = {}
        self.calls: list[tuple[CanonicalFeishuRoute, str]] = []
        self.hook: Callable[[], None] | None = None
        self.result: Any = None
        self.is_available = True

    async def assert_session(self, route: CanonicalFeishuRoute, session_id: str) -> Any:
        self.calls.append((route, session_id))
        if self.hook is not None:
            hook, self.hook = self.hook, None
            hook()
        if not self.is_available or self.bindings.get(session_id) != (
            route.member.organization_id,
            route.member.owner_id,
            route.identity.reference,
            route.event.chat_id,
        ):
            raise RuntimeError("Fixture native session binding unavailable")
        return self.result

    def register(self, route: CanonicalFeishuRoute, session_id: str) -> None:
        self.bindings[session_id] = (
            route.member.organization_id,
            route.member.owner_id,
            route.identity.reference,
            route.event.chat_id,
        )


@dataclass
class _Environment:
    path: Path
    routes: CanonicalFeishuRouteService
    writer: _Writer
    sessions: _Sessions
    service: CanonicalFeishuDeliveryService = field(init=False)


def _service(env: _Environment) -> CanonicalFeishuDeliveryService:
    return CanonicalFeishuDeliveryService(
        env.path,
        _snapshots(env.path),
        env.routes,
        writer=env.writer,
        session_assertion=env.sessions,
        clock=lambda: _NOW,
    )


@pytest.fixture
def business(legacy_db: Path) -> _Environment:
    connection = FeishuMemberConnection(
        _IDS["homeOrg"], _IDS["homeMember"], "plugin-a", "tenant-aa", "cli_appA", "r1", "channel-a"
    )
    with sqlite3.connect(legacy_db) as db:
        db.executescript(_IM_SCHEMA + _DELIVERY_SCHEMA)
        db.execute(
            "INSERT INTO channel_plugins (id,type,name,enabled,status,user_id,org_id,created_at,"
            "updated_at) VALUES ('plugin-a','lark','Moss',1,'connected',?,?,1,1)",
            (_IDS["homeMember"], _IDS["homeOrg"]),
        )
        db.execute(
            "INSERT INTO moss_im_connections VALUES ('plugin-a',?,?,'cli_appA','tenant-aa','r1',?,1)",
            (_IDS["homeMember"], _IDS["homeOrg"], _IDS["homeMember"]),
        )
        db.execute(
            "INSERT INTO user_auth_identities VALUES ('binding-a',?,?,'feishu-moss',?,"
            "'ou_member_a','{}',1,1)",
            (_IDS["homeOrg"], _IDS["homeMember"], connection.scope),
        )
    routes = CanonicalFeishuRouteService(
        legacy_db,
        authority_id="retained-moss",
        channel_id="channel-a",
        plugin_id="plugin-a",
        connection_owner_id=_IDS["homeMember"],
        actual_app_id="cli_appA",
    )
    env = _Environment(legacy_db, routes, _Writer(), _Sessions())
    env.service = _service(env)
    return env


def _message(message_id: str = "om_message_a") -> InboundMessage:
    message = _normalized_message()
    message.metadata.update(
        feishu_app_id="cli_appA", sender_tenant_key="tenant-aa", message_id=message_id
    )
    assert message.channel_subject is not None
    message.channel_subject.metadata["message_id"] = message_id
    return message


def _execute(env: _Environment, sql: str, args: tuple[object, ...] = ()) -> None:
    with sqlite3.connect(env.path) as db:
        db.execute(sql, args)


def _rows(env: _Environment, table: str) -> list[tuple[Any, ...]]:
    assert table in {
        "moss_im_deliveries",
        "moss_im_connections",
        "moss_im_conversations",
        "moss_im_link_challenges",
        "user_auth_identities",
    }
    with sqlite3.connect(env.path) as db:
        return db.execute("SELECT * FROM " + table).fetchall()


async def _ready(
    env: _Environment, session_id: str = "native-original-session"
) -> CanonicalFeishuClaim:
    claim = await env.service.claim(_message())
    assert claim is not None and claim.route is not None
    env.sessions.register(claim.route, session_id)
    return await env.service.set_pointer(claim, session_id)


async def test_original_uuid_event_pointer_and_completed_association(
    business: _Environment,
) -> None:
    before = {}
    with sqlite3.connect(business.path) as db:
        for table in ("users", "human_accounts", "human_account_memberships", "moss_member_access"):
            before[table] = db.execute("SELECT * FROM " + table).fetchall()
    claim = await _ready(business)
    assert claim.route is not None
    assert claim.route.account_id == _IDS["account"]
    assert claim.route.member.owner_id == _IDS["homeMember"] != claim.route.account_id
    assert claim.route.session_pointer == "native-original-session"
    assert _rows(business, "moss_im_conversations")[0][:7] == (
        claim.route.conversation_key,
        _IDS["homeOrg"],
        _IDS["homeMember"],
        claim.route.identity.reference,
        claim.connection.scope,
        claim.event.chat_id,
        "native-original-session",
    )
    dispatch = await business.service.delivery(
        claim, "dispatching", session_id="native-original-session"
    )
    completed = await business.service.delivery(claim, "completed")
    assert dispatch.status == "dispatching" and completed.status == "completed"
    assert completed.member_id == _IDS["homeMember"]
    assert completed.agent_ref == claim.route.identity.reference
    assert completed.session_id == "native-original-session"
    assert await business.service.claim(_message()) is None
    with sqlite3.connect(business.path) as db:
        assert all(
            db.execute("SELECT * FROM " + table).fetchall() == rows
            for table, rows in before.items()
        )


async def test_unbound_sdk_event_is_claimed_once_even_after_challenge_cooldown(
    business: _Environment,
) -> None:
    _execute(business, "DELETE FROM user_auth_identities")
    claim = await business.service.claim(_message())
    assert claim is not None and claim.route is None
    assert claim.event.sender_id == "ou_member_a"
    assert _rows(business, "moss_im_deliveries")[0][4:8] == (None, None, None, "claimed")
    result = await business.service.delivery(claim, "completed")
    assert result.member_id is result.agent_ref is result.session_id is None
    business.service._clock = lambda: _NOW + 120
    assert await business.service.claim(_message()) is None
    with pytest.raises(MossworkError):
        await business.service.set_pointer(claim, "arbitrary-session")
    with pytest.raises(MossworkError):
        await business.service.delivery(claim, "dispatching", session_id="arbitrary-session")
    assert not _rows(business, "moss_im_conversations") and not business.sessions.calls


async def test_two_instances_eight_threads_atomically_claim_original_event(
    business: _Environment,
) -> None:
    services = [business.service, _service(business)]
    gate = threading.Barrier(8)

    def claim(index: int) -> CanonicalFeishuClaim | None:
        gate.wait(timeout=10)
        return asyncio.run(services[index % 2].claim(_message()))

    results = await asyncio.gather(*(asyncio.to_thread(claim, index) for index in range(8)))
    assert sum(result is not None for result in results) == 1
    assert len(_rows(business, "moss_im_deliveries")) == 1


async def test_pointer_cas_conflict_never_selects_second_session(business: _Environment) -> None:
    first, second = await asyncio.gather(
        business.service.claim(_message("event-a")), business.service.claim(_message("event-b"))
    )
    assert first is not None and first.route is not None
    assert second is not None and second.route is not None
    business.sessions.register(first.route, "session-a")
    business.sessions.register(second.route, "session-b")
    results = await asyncio.gather(
        business.service.set_pointer(first, "session-a"),
        business.service.set_pointer(second, "session-b"),
        return_exceptions=True,
    )
    assert sum(isinstance(value, CanonicalFeishuClaim) for value in results) == 1
    failures = [value for value in results if isinstance(value, MossworkError)]
    assert len(failures) == 1 and failures[0].status == 409
    assert _rows(business, "moss_im_conversations")[0][6] in {"session-a", "session-b"}
    assert all(row[7] == "claimed" for row in _rows(business, "moss_im_deliveries"))


@pytest.mark.parametrize("state", ["claimed", "dispatching", "completed", "failed", "unknown"])
async def test_every_original_delivery_state_remains_occupied(
    business: _Environment, state: str
) -> None:
    claim = await business.service.claim(_message())
    assert claim is not None
    _execute(business, "UPDATE moss_im_deliveries SET status=?", (state,))
    assert await business.service.claim(_message()) is None


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE human_accounts SET status='disabled'",
        "UPDATE users SET role='user' WHERE phone IS NOT NULL",
        "UPDATE users SET membership_revision=membership_revision+1",
        "DELETE FROM user_auth_identities",
        "UPDATE moss_im_connections SET revision='rotated'",
        "UPDATE channel_plugins SET enabled=0",
    ],
)
async def test_after_dispatch_revocation_or_rotation_still_records_unknown(
    business: _Environment,
    mutation: str,
) -> None:
    claim = await _ready(business)
    await business.service.delivery(claim, "dispatching", session_id="native-original-session")
    _execute(business, mutation)
    business.sessions.is_available = False
    calls = len(business.sessions.calls)
    result = await business.service.delivery(claim, "unknown")
    assert result.status == "unknown" and result.session_id == "native-original-session"
    assert result.member_id == _IDS["homeMember"]
    assert len(business.sessions.calls) == calls
    with pytest.raises(MossworkError):
        await business.service.delivery(claim, "dispatching", session_id="native-original-session")
    assert _rows(business, "moss_im_deliveries")[0][7] == "unknown"


@pytest.mark.parametrize("terminal", ["completed", "failed", "unknown"])
async def test_terminal_cannot_change_session_owner_or_dispatch_again(
    business: _Environment,
    terminal: Any,
) -> None:
    claim = await _ready(business)
    await business.service.delivery(claim, "dispatching", session_id="native-original-session")
    await business.service.delivery(claim, terminal)
    original = _rows(business, "moss_im_deliveries")
    with pytest.raises(MossworkError) as caught:
        await business.service.delivery(claim, terminal, session_id="new-session")
    assert caught.value.status == 409
    with pytest.raises(MossworkError):
        await business.service.delivery(claim, "dispatching", session_id="native-original-session")
    with pytest.raises(MossworkError):
        await business.service.set_pointer(claim, "native-original-session")
    assert _rows(business, "moss_im_deliveries") == original


async def test_dispatch_admission_is_once_and_ack_is_not_completion(business: _Environment) -> None:
    claim = await _ready(business)
    with pytest.raises(MossworkError) as caught:
        await business.service.delivery(claim, "completed")
    assert caught.value.status == 409
    with pytest.raises(MossworkError):
        await business.service.delivery(claim, "accepted")  # type: ignore[arg-type]
    first = await business.service.delivery(
        claim, "dispatching", session_id="native-original-session"
    )
    assert first.status == "dispatching"
    with pytest.raises(MossworkError) as caught:
        await business.service.delivery(claim, "dispatching", session_id="native-original-session")
    assert caught.value.status == 409


@pytest.mark.parametrize("bad_result", [False, True, "ok", 1, {}])
async def test_native_assertion_requires_exact_none(
    business: _Environment, bad_result: Any
) -> None:
    claim = await business.service.claim(_message())
    assert claim is not None and claim.route is not None
    business.sessions.register(claim.route, "session-a")
    business.sessions.result = bad_result
    with pytest.raises(MossworkError) as caught:
        await business.service.set_pointer(claim, "session-a")
    assert caught.value.status == 403 and not _rows(business, "moss_im_conversations")


async def test_missing_or_foreign_actual_native_binding_is_refused(business: _Environment) -> None:
    claim = await business.service.claim(_message())
    assert claim is not None and claim.route is not None
    business.sessions.bindings["session-a"] = ("foreign-org", _IDS["homeMember"], "ref", "chat")
    with pytest.raises(MossworkError):
        await business.service.set_pointer(claim, "session-a")
    with pytest.raises(TypeError):
        CanonicalFeishuDeliveryService(
            business.path,
            _snapshots(business.path),
            business.routes,
            writer=business.writer,
            session_assertion=None,  # type: ignore[arg-type]
        )
    assert not _rows(business, "moss_im_conversations")


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE moss_im_connections SET revision='rotated'",
        "UPDATE moss_im_connections SET app_id='cli_other'",
        "UPDATE moss_im_connections SET tenant_key='other-tenant'",
        "UPDATE users SET membership_revision=membership_revision+1",
        "DELETE FROM user_auth_identities",
        "INSERT INTO moss_member_access SELECT org_id,id,0,id,9 FROM users WHERE phone IS NOT NULL",
    ],
)
async def test_native_await_cannot_hide_current_route_or_grant_change(
    business: _Environment,
    mutation: str,
) -> None:
    claim = await business.service.claim(_message())
    assert claim is not None and claim.route is not None
    business.sessions.register(claim.route, "session-a")
    business.sessions.hook = lambda: _execute(business, mutation)
    with pytest.raises(MossworkError):
        await business.service.set_pointer(claim, "session-a")
    assert not _rows(business, "moss_im_conversations")


@pytest.mark.parametrize("bad_result", [False, True, "ok"])
async def test_writer_strict_none_and_missing_writer_never_write(
    business: _Environment,
    bad_result: Any,
) -> None:
    business.writer.result = bad_result
    with pytest.raises(MossworkError) as caught:
        await business.service.claim(_message())
    assert caught.value.status == 503 and not _rows(business, "moss_im_deliveries")


async def test_terminal_accounting_still_requires_current_writer(business: _Environment) -> None:
    claim = await _ready(business)
    await business.service.delivery(claim, "dispatching", session_id="native-original-session")
    business.writer.is_available = False
    with pytest.raises(MossworkError) as caught:
        await business.service.delivery(claim, "unknown")
    assert caught.value.status == 503
    assert _rows(business, "moss_im_deliveries")[0][7] == "dispatching"


async def test_configure_original_revision_preservation_and_rotation_leaves_old_rows(
    business: _Environment,
) -> None:
    claim = await _ready(business)
    old_bindings = _rows(business, "user_auth_identities")
    old_pointers = _rows(business, "moss_im_conversations")
    await business.service.configure(_token(), "tenant-aa")
    assert _rows(business, "moss_im_connections")[0][5] == "r1"
    await business.service.configure(_token(), "tenant-new")
    revision = _rows(business, "moss_im_connections")[0][5]
    UUID(revision)
    assert revision != "r1"
    await business.service.configure(_token(), "tenant-new")
    assert _rows(business, "moss_im_connections")[0][5] == revision
    assert _rows(business, "user_auth_identities") == old_bindings
    assert _rows(business, "moss_im_conversations") == old_pointers
    assert claim.route is not None
    with pytest.raises(MossworkError):
        await business.service.set_pointer(claim, "native-original-session")


@pytest.mark.parametrize(
    "token_name",
    [
        "admin_with_narrow_signed_scopes",
        "admin_with_empty_signed_scopes",
        "sms_company_actual_member",
        "foreign_org_super_admin",
    ],
)
async def test_configure_real_signed_scope_and_same_org_admin_required(
    business: _Environment,
    token_name: str,
) -> None:
    before = _rows(business, "moss_im_connections")
    with pytest.raises(MossworkError):
        await business.service.configure(_token(token_name), "tenant-aa")
    assert _rows(business, "moss_im_connections") == before


@pytest.mark.parametrize("tenant", ["short", "tenant space", "a" * 129, "", "tenant\nnewline"])
async def test_configure_exact_original_tenant_regex(business: _Environment, tenant: str) -> None:
    with pytest.raises(MossworkError) as caught:
        await business.service.configure(_token(), tenant)
    assert caught.value.status == 400


async def test_configure_rechecks_actual_human_after_writer_await(business: _Environment) -> None:
    business.writer.hook = lambda: _execute(
        business, "UPDATE users SET membership_revision=membership_revision+1"
    )
    before = _rows(business, "moss_im_connections")
    with pytest.raises(MossworkError):
        await business.service.configure(_token(), "tenant-new")
    assert _rows(business, "moss_im_connections") == before


@pytest.mark.parametrize("field", ["org_id", "user_id", "agent_ref", "connection_scope", "chat_id"])
async def test_pointer_existing_original_tuple_cannot_be_rebound(
    business: _Environment,
    field: str,
) -> None:
    claim = await _ready(business)
    _execute(business, f"UPDATE moss_im_conversations SET {field}='foreign'")
    with pytest.raises(MossworkError):
        await business.service.set_pointer(claim, "native-original-session")


@pytest.mark.parametrize(
    "field", ["platform_user_id", "chat_id", "user_id", "agent_ref", "session_id", "created_at"]
)
async def test_terminal_only_updates_the_original_persisted_event_tuple(
    business: _Environment,
    field: str,
) -> None:
    claim = await _ready(business)
    await business.service.delivery(claim, "dispatching", session_id="native-original-session")
    _execute(business, f"UPDATE moss_im_deliveries SET {field}='foreign'")
    with pytest.raises(MossworkError) as caught:
        await business.service.delivery(claim, "unknown")
    assert caught.value.status == 409


async def test_receipt_is_private_to_issuing_service_and_cannot_be_constructed(
    business: _Environment,
) -> None:
    claim = await business.service.claim(_message())
    assert claim is not None
    with pytest.raises(TypeError):
        CanonicalFeishuClaim()
    with pytest.raises(AttributeError):
        claim.route = None  # type: ignore[misc]
    with pytest.raises(TypeError):
        vars(claim)
    with pytest.raises(TypeError):
        json.dumps(claim)
    with pytest.raises(MossworkError):
        await _service(business).delivery(claim, "unknown")
    with pytest.raises(MossworkError):
        await business.service.delivery(object.__new__(CanonicalFeishuClaim), "unknown")


async def test_missing_db_is_not_created_or_empty_schema_initialized(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite"
    routes = CanonicalFeishuRouteService(
        path,
        authority_id="retained-moss",
        channel_id="channel-a",
        plugin_id="plugin-a",
        connection_owner_id=_IDS["homeMember"],
        actual_app_id="cli_appA",
    )
    service = CanonicalFeishuDeliveryService(
        path,
        _snapshots(path),
        routes,
        writer=_Writer(),
        session_assertion=_Sessions(),
        clock=lambda: _NOW,
    )
    with pytest.raises(MossworkError) as caught:
        await service.claim(_message())
    assert caught.value.status == 503 and not path.exists()
    with sqlite3.connect(path):
        pass
    before = path.read_bytes()
    with pytest.raises(MossworkError) as caught:
        await service.claim(_message())
    assert caught.value.status == 503 and path.read_bytes() == before


async def test_bad_sdk_evidence_never_claims_and_mutation_during_await_cannot_change_event(
    business: _Environment,
) -> None:
    bad = _message()
    bad.metadata["sender_type"] = "bot"
    with pytest.raises(MossworkError):
        await business.service.claim(bad)
    assert not _rows(business, "moss_im_deliveries")
    original = _message()
    business.writer.hook = lambda: original.metadata.update(message_id="late-mutated")
    claim = await business.service.claim(original)
    assert claim is not None and claim.event.message_id == "om_message_a"


async def test_db_and_session_io_does_not_run_on_event_loop(business: _Environment) -> None:
    thread_ids = []
    original = business.service._transaction

    def transaction(operation: Callable[..., Any]) -> Any:
        thread_ids.append(threading.get_ident())
        return original(operation)

    business.service._transaction = transaction  # type: ignore[method-assign]
    await _ready(business)
    assert thread_ids and threading.get_ident() not in thread_ids


async def test_exact_original_ts_sqlite_oracle_matches_business_keys_and_rows(
    business: _Environment,
) -> None:
    oracle = json.loads(
        (Path(__file__).parent / "fixtures/feishu_delivery_business_legacy.json").read_text(
            encoding="utf-8"
        )
    )
    assert oracle["sourceCommit"] == "d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d"
    assert oracle["sourceSha256"] == (
        "11580c5e309a241f1611fac56bffa7069f3172759abf0c7f015b403aeff13423"
    )
    assert len(oracle["consumerTightenings"]) == 3
    original = oracle["values"]
    claim = await business.service.claim(_message())
    assert claim is not None and claim.route is not None
    assert claim.connection.scope == original["connection"]["scope"]

    def row(table: str) -> dict[str, Any]:
        assert table in {"moss_im_deliveries", "moss_im_conversations"}
        with sqlite3.connect(business.path) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM " + table).fetchone())

    assert row("moss_im_deliveries") == original["initial"]
    assert all(value["claimed"] is False for value in original["duplicates"])
    business.sessions.register(claim.route, "native-original-session")
    claim = await business.service.set_pointer(claim, "native-original-session")
    assert row("moss_im_conversations") == original["pointer"]
    await business.service.delivery(claim, "dispatching", session_id="native-original-session")
    assert row("moss_im_deliveries") == original["dispatching"]
    await business.service.delivery(claim, "completed")
    assert row("moss_im_deliveries") == original["completed"]
    assert original["conflict"] == 409
    assert original["configure"]["samePreservesRevision"]
    assert original["configure"]["tenantChangeRotatesRevision"]
    assert original["configure"]["repeatPreservesRevision"]
    # Document the intentional difference from the old unrestricted UPDATE.
    assert original["originalPermittedRedispatch"]["session_id"] == "replacement-session"
    with pytest.raises(MossworkError):
        await business.service.delivery(claim, "dispatching", session_id="replacement-session")


async def test_claim_keeps_reservation_when_postcommit_writer_is_lost(
    business: _Environment,
) -> None:
    original = business.service._transaction

    def transaction(operation: Callable[..., Any]) -> Any:
        result = original(operation)
        business.writer.is_available = False
        return result

    business.service._transaction = transaction  # type: ignore[method-assign]
    with pytest.raises(MossworkError) as caught:
        await business.service.claim(_message())
    assert caught.value.status == 503
    assert _rows(business, "moss_im_deliveries")[0][7] == "claimed"
    business.service._transaction = original  # type: ignore[method-assign]
    business.writer.is_available = True
    assert await business.service.claim(_message()) is None


async def test_dispatch_postcommit_revoke_can_be_recorded_unknown_without_new_admission(
    business: _Environment,
) -> None:
    claim = await _ready(business)
    original = business.service._transaction

    def transaction(operation: Callable[..., Any]) -> Any:
        result = original(operation)
        _execute(business, "DELETE FROM user_auth_identities")
        return result

    business.service._transaction = transaction  # type: ignore[method-assign]
    with pytest.raises(MossworkError):
        await business.service.delivery(claim, "dispatching", session_id="native-original-session")
    assert _rows(business, "moss_im_deliveries")[0][7] == "dispatching"
    business.service._transaction = original  # type: ignore[method-assign]
    business.sessions.is_available = False
    result = await business.service.delivery(claim, "unknown")
    assert result.status == "unknown" and result.session_id == "native-original-session"


async def test_configure_cannot_use_foreign_same_account_membership_as_admin(
    business: _Environment,
) -> None:
    # Reuse a correctly signed/current company member token: being the same
    # human account as the home admin supplies no authority over another org.
    before = _rows(business, "moss_im_connections")
    with pytest.raises(MossworkError):
        await business.service.configure(_token("sms_company_actual_member"), "tenant-new")
    assert _rows(business, "moss_im_connections") == before


async def test_group_delivery_preserves_actual_sender_member_not_connection_owner(
    business: _Environment,
) -> None:
    first = await business.service.claim(_message("first-event"))
    assert first is not None and first.route is not None
    with sqlite3.connect(business.path) as db:
        db.execute(
            "INSERT INTO users (id,org_id,email,name,role,status,created_at) "
            "VALUES (?,?,'second@example.test','second','user','active',1)",
            (_SECOND_MEMBER, _IDS["homeOrg"]),
        )
        db.execute(
            "INSERT INTO human_accounts VALUES (?,'13900000002','active',1)", (_SECOND_ACCOUNT,)
        )
        db.execute(
            "INSERT INTO human_account_memberships VALUES (?,?,?,1)",
            (_SECOND_ACCOUNT, _IDS["homeOrg"], _SECOND_MEMBER),
        )
        db.execute(
            "INSERT INTO moss_member_access VALUES (?,?,1,?,9)",
            (_IDS["homeOrg"], _SECOND_MEMBER, _IDS["homeMember"]),
        )
        db.execute(
            "INSERT INTO user_auth_identities VALUES ('binding-b',?,?,'feishu-moss',?,"
            "'ou_member_b','{}',1,1)",
            (_IDS["homeOrg"], _SECOND_MEMBER, first.connection.scope),
        )
    message = _message("second-event")
    message.metadata.update(sender_id="ou_member_b", sender_open_id="ou_member_b")
    assert message.channel_subject is not None
    message.channel_subject.metadata["sender_id"] = "ou_member_b"
    second = await business.service.claim(message)
    assert second is not None and second.route is not None
    assert second.route.account_id == _SECOND_ACCOUNT
    assert second.route.member.owner_id == _SECOND_MEMBER != second.connection.owner_id
    assert second.route.conversation_key != first.route.conversation_key
    business.sessions.register(second.route, "second-native-session")
    second = await business.service.set_pointer(second, "second-native-session")
    delivery = await business.service.delivery(
        second, "dispatching", session_id="second-native-session"
    )
    assert delivery.member_id == _SECOND_MEMBER
    assert second.route is not None and delivery.agent_ref == second.route.identity.reference
