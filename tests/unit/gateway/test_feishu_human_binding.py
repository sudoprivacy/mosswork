"""Real retained SQLite, human phone JWT and normalized Feishu SDK evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from moss_gateway.channels.feishu import FeishuChannel, FeishuConfig
from tests.unit.gateway.test_canonical_feishu import _IM_SCHEMA
from tests.unit.gateway.test_personal_moss_im_identity import (
    _normalized_message,
    _processor,
    _sdk_message,
)
from tests.unit.identity.test_human_sessions import _IDS, _Environment, _login
from tests.unit.identity.test_human_sessions import legacy_db as legacy_db
from tests.unit.identity.test_human_sessions import sessions as sessions

from octop.infra.errors import MossworkError
from octop.infra.gateway.canonical_feishu import CanonicalFeishuRouteService
from octop.infra.gateway.feishu_binding import CanonicalFeishuBindingService

_ORIGINAL = json.loads(
    (Path(__file__).parent / "fixtures/feishu_binding_legacy.json").read_text(encoding="utf-8")
)

# Exact retained mossImIdentity.ts DDL, fixture setup only.
_CHALLENGES = """
CREATE TABLE moss_im_link_challenges (
    digest TEXT PRIMARY KEY, org_id TEXT NOT NULL, connection_scope TEXT NOT NULL,
    plugin_id TEXT NOT NULL, connection_owner_id TEXT NOT NULL, platform_user_id TEXT NOT NULL,
    expires_at BIGINT NOT NULL, claimed_by TEXT, created_at BIGINT NOT NULL
);
CREATE INDEX moss_im_challenge_sender_idx
    ON moss_im_link_challenges (connection_scope, platform_user_id, created_at);
"""


@dataclass
class _BindingEnvironment:
    human: _Environment
    service: CanonicalFeishuBindingService
    routes: CanonicalFeishuRouteService


@pytest.fixture
def binding(sessions: _Environment) -> _BindingEnvironment:
    with sqlite3.connect(sessions.path) as db:
        db.executescript(_IM_SCHEMA + _CHALLENGES)
        db.execute(
            "INSERT INTO channel_plugins (id, type, name, enabled, status, user_id, org_id, "
            "created_at, updated_at) VALUES ('plugin-a', 'lark', 'Moss', 1, 'connected', ?, ?, 1, 1)",
            (_IDS["homeMember"], _IDS["homeOrg"]),
        )
        db.execute(
            "INSERT INTO moss_im_connections VALUES ('plugin-a', ?, ?, 'cli_app_a', "
            "'tenant-a', 'r1', ?, 1)",
            (_IDS["homeMember"], _IDS["homeOrg"], _IDS["homeMember"]),
        )
    routes = CanonicalFeishuRouteService(
        sessions.path,
        authority_id="retained-moss",
        channel_id="channel-a",
        plugin_id="plugin-a",
        connection_owner_id=_IDS["homeMember"],
        actual_app_id="cli_app_a",
    )
    return _BindingEnvironment(
        sessions,
        CanonicalFeishuBindingService(
            sessions.path,
            sessions.service.snapshots,
            routes,
            writer=sessions.writer,
            clock=lambda: sessions.now[0],
        ),
        routes,
    )


def _execute(binding: _BindingEnvironment, sql: str, params: tuple[object, ...] = ()) -> None:
    with sqlite3.connect(binding.human.path) as db:
        db.execute(sql, params)


def _rows(binding: _BindingEnvironment, table: str) -> list[tuple[object, ...]]:
    assert table in {"moss_im_link_challenges", "user_auth_identities", "moss_im_conversations"}
    with sqlite3.connect(binding.human.path) as db:
        return db.execute("SELECT * FROM " + table).fetchall()


async def test_sdk_challenge_normal_human_login_binding_route_and_unlink(
    binding: _BindingEnvironment,
) -> None:
    message = _normalized_message()
    assert await binding.routes.resolve(message) is None
    code = await binding.service.issue_challenge(message)
    assert code is not None and re.fullmatch(r"[A-Za-z0-9_-]{32}", code)
    stored = _rows(binding, "moss_im_link_challenges")
    assert stored[0][0] == hashlib.sha256(code.encode("ascii")).hexdigest()
    assert code not in str(stored)
    assert stored[0][6] - stored[0][8] == 600_000
    human = await _login(binding.human)
    await binding.service.bind(human.access_token, code)
    actual = await binding.routes.resolve(message)
    assert actual is not None
    assert actual.member.owner_id == human.member_id == _IDS["homeMember"]
    assert actual.account_id == human.account_id
    assert actual.session_pointer is None
    assert _rows(binding, "moss_im_conversations") == []
    views = await binding.service.bindings(human.access_token)
    assert len(views) == 1
    assert views[0].id == actual.binding_id
    assert views[0].sender_id == "ou_member_a"
    assert views[0].plugin_id == "plugin-a" and views[0].app_id == "cli_app_a"
    assert views[0].is_current
    with pytest.raises(MossworkError):
        await binding.service.bind(human.access_token, code)
    await binding.service.unlink(human.access_token, actual.binding_id)
    assert await binding.service.bindings(human.access_token) == ()
    assert await binding.routes.resolve(message) is None


async def test_competing_challenge_and_claim_use_atomic_original_rows(
    binding: _BindingEnvironment,
) -> None:
    results = await asyncio.gather(
        binding.service.issue_challenge(_normalized_message()),
        binding.service.issue_challenge(_normalized_message()),
    )
    assert sum(code is not None for code in results) == 1
    assert len(_rows(binding, "moss_im_link_challenges")) == 1
    code = next(code for code in results if code is not None)
    human = await _login(binding.human)
    attempts = await asyncio.gather(
        binding.service.bind(human.access_token, code),
        binding.service.bind(human.access_token, code),
        return_exceptions=True,
    )
    assert sum(attempt is None for attempt in attempts) == 1
    assert sum(isinstance(attempt, MossworkError) for attempt in attempts) == 1
    assert len(_rows(binding, "user_auth_identities")) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE moss_im_connections SET revision = 'r2'",
        "UPDATE moss_im_connections SET tenant_key = 'other-tenant'",
        "UPDATE moss_im_connections SET app_id = 'cli_other'",
        "UPDATE channel_plugins SET enabled = 0",
        "UPDATE users SET status = 'disabled' WHERE phone IS NOT NULL",
        "UPDATE human_accounts SET status = 'disabled'",
    ],
)
async def test_pending_code_cannot_outlive_its_original_connection_or_human(
    binding: _BindingEnvironment, mutation: str
) -> None:
    code = await binding.service.issue_challenge(_normalized_message())
    assert code is not None
    human = await _login(binding.human)
    _execute(binding, mutation)
    with pytest.raises(MossworkError):
        await binding.service.bind(human.access_token, code)
    assert not _rows(binding, "user_auth_identities")
    assert _rows(binding, "moss_im_link_challenges")[0][7] is None


@pytest.mark.parametrize("mutation", ["revoke", "role", "revision", "grant", "expire"])
async def test_human_rechecked_inside_binding_transaction_after_writer_await(
    binding: _BindingEnvironment, mutation: str
) -> None:
    code = await binding.service.issue_challenge(_normalized_message())
    assert code is not None
    human = await _login(binding.human)
    snapshot = await binding.human.service.snapshots.snapshot(human.access_token)

    def revoke_after_snapshot(_: int) -> None:
        if mutation == "revoke":
            _execute(
                binding,
                "INSERT INTO revoked_tokens VALUES (?, ?)",
                (snapshot.credential_id, 2_100_000_000),
            )
        elif mutation == "role":
            _execute(binding, "UPDATE users SET role = 'user' WHERE id = ?", (human.member_id,))
        elif mutation == "revision":
            _execute(binding, "UPDATE users SET membership_revision = membership_revision + 1")
        elif mutation == "grant":
            _execute(
                binding,
                "INSERT INTO moss_member_access VALUES (?, ?, 0, ?, 3)",
                (human.organization_id, human.member_id, human.member_id),
            )
        else:
            binding.human.now[0] += 301

    binding.human.writer.on_call = revoke_after_snapshot
    with pytest.raises(MossworkError):
        await binding.service.bind(human.access_token, code)
    assert not _rows(binding, "user_auth_identities")
    assert _rows(binding, "moss_im_link_challenges")[0][7] is None


async def test_expired_code_and_foreign_organization_do_not_claim_a_sender(
    binding: _BindingEnvironment,
) -> None:
    code = await binding.service.issue_challenge(_normalized_message())
    assert code is not None
    human = await _login(binding.human)
    company = await binding.human.service.switch_organization(
        human.access_token, _IDS["companyOrg"]
    )
    with pytest.raises(MossworkError):
        await binding.service.bind(company.access_token, code)
    assert _rows(binding, "moss_im_link_challenges")[0][7] is None
    binding.human.now[0] += 601
    binding.human.now[0] += 1
    # A fresh human proof cannot revive the old ten-minute private code.
    fresh = await _login(binding.human)
    with pytest.raises(MossworkError):
        await binding.service.bind(fresh.access_token, code)
    assert not _rows(binding, "user_auth_identities")


async def test_existing_sender_and_member_bindings_require_explicit_unlink(
    binding: _BindingEnvironment,
) -> None:
    code = await binding.service.issue_challenge(_normalized_message())
    assert code is not None
    human = await _login(binding.human)
    await binding.service.bind(human.access_token, code)
    other_sender = _normalized_message()
    other_sender.metadata.update(sender_id="ou_other", sender_open_id="ou_other")
    assert other_sender.channel_subject is not None
    other_sender.channel_subject.metadata["sender_id"] = "ou_other"
    other_code = await binding.service.issue_challenge(other_sender)
    assert other_code is not None
    with pytest.raises(MossworkError) as conflict:
        await binding.service.bind(human.access_token, other_code)
    assert conflict.value.status == 409
    assert len(_rows(binding, "user_auth_identities")) == 1
    assert _rows(binding, "moss_im_link_challenges")[1][7] is None


async def test_revoked_moss_user_can_unlink_their_own_identity_without_regrant(
    binding: _BindingEnvironment,
) -> None:
    code = await binding.service.issue_challenge(_normalized_message())
    assert code is not None
    human = await _login(binding.human)
    await binding.service.bind(human.access_token, code)
    current = (await binding.service.bindings(human.access_token))[0]
    _execute(
        binding,
        "INSERT INTO moss_member_access VALUES (?, ?, 0, ?, 3)",
        (human.organization_id, human.member_id, human.member_id),
    )
    assert (await binding.service.bindings(human.access_token))[0].id == current.id
    with pytest.raises(MossworkError):
        await binding.routes.resolve(_normalized_message())
    await binding.service.unlink(human.access_token, current.id)
    assert not _rows(binding, "user_auth_identities")
    with pytest.raises(MossworkError) as missing:
        await binding.service.unlink(human.access_token, current.id)
    assert missing.value.status == 404


async def test_denied_writer_has_no_mutation_and_lost_writer_reports_unconfirmed_effect(
    binding: _BindingEnvironment,
) -> None:
    binding.human.writer.is_allowed = False
    with pytest.raises(MossworkError) as denied:
        await binding.service.issue_challenge(_normalized_message())
    assert denied.value.status == 503
    assert not _rows(binding, "moss_im_link_challenges")
    binding.human.writer.is_allowed = True
    next_call = binding.human.writer.calls + 2
    binding.human.writer.on_call = lambda count: setattr(
        binding.human.writer, "is_allowed", count != next_call
    )
    with pytest.raises(MossworkError) as lost:
        await binding.service.issue_challenge(_normalized_message())
    assert lost.value.status == 503
    # The first admitted transaction committed; refusal must never be described
    # as rollback or be used to issue a replacement code without reconciliation.
    assert len(_rows(binding, "moss_im_link_challenges")) == 1


@pytest.mark.parametrize(
    "field,value",
    [("sender_type", "bot"), ("sender_tenant_key", "foreign"), ("feishu_app_id", "cli_foreign")],
)
async def test_unverified_platform_metadata_cannot_issue_challenges(
    binding: _BindingEnvironment, field: str, value: str
) -> None:
    message = _normalized_message()
    message.metadata[field] = value
    with pytest.raises(MossworkError):
        await binding.service.issue_challenge(message)
    assert not _rows(binding, "moss_im_link_challenges")


@pytest.mark.parametrize("case", _ORIGINAL["cases"], ids=lambda case: case["scenario"])
async def test_original_pending_private_codes_and_claim_semantics_survive_the_port(
    binding: _BindingEnvironment, case: dict[str, object]
) -> None:
    assert _ORIGINAL["sourceCommit"] == "d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d"
    assert (
        _ORIGINAL["sourceSha256"]
        == "11580c5e309a241f1611fac56bffa7069f3172759abf0c7f015b403aeff13423"
    )
    pending = case["pending"]
    assert isinstance(pending, dict)
    columns = (
        "digest",
        "org_id",
        "connection_scope",
        "plugin_id",
        "connection_owner_id",
        "platform_user_id",
        "expires_at",
        "claimed_by",
        "created_at",
    )
    _execute(
        binding,
        "INSERT INTO moss_im_link_challenges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(pending[column] for column in columns),
    )
    assert await binding.service.issue_challenge(_normalized_message()) is case["cooldown"] is None
    human = await _login(binding.human)
    token = human.access_token
    scenario = case["scenario"]
    if scenario == "expired":
        _execute(binding, "UPDATE moss_im_link_challenges SET expires_at = ?", (_ORIGINAL["now"],))
    elif scenario == "claimed":
        _execute(binding, "UPDATE moss_im_link_challenges SET claimed_by = 'prior-member'")
    elif scenario == "changed_connection":
        _execute(binding, "UPDATE moss_im_connections SET revision = 'r2'")
    elif scenario == "foreign_org":
        token = (
            await binding.human.service.switch_organization(token, _IDS["companyOrg"])
        ).access_token
    elif scenario in ("sender_conflict", "member_conflict"):
        _execute(
            binding,
            "INSERT INTO user_auth_identities VALUES ('existing-binding', ?, ?, 'feishu-moss', ?, ?, '{}', ?, ?)",
            (
                human.organization_id,
                _IDS["companyMember"] if scenario == "sender_conflict" else human.member_id,
                pending["connection_scope"],
                "ou_member_a" if scenario == "sender_conflict" else "ou_other",
                _ORIGINAL["now"],
                _ORIGINAL["now"],
            ),
        )
    code = case["code"]
    assert isinstance(code, str)
    try:
        await binding.service.bind(token, code)
        status = 204
    except MossworkError as error:
        status = error.status
    assert status == case["status"]
    assert _rows(binding, "moss_im_link_challenges")[0][7] == case["claimedBy"]
    with sqlite3.connect(binding.human.path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT org_id, user_id, provider, issuer, normalized_subject, metadata_json "
            "FROM user_auth_identities WHERE id != 'existing-binding'"
        ).fetchone()
    assert (dict(row) if row else None) == case["linked"]


async def test_lists_only_configured_bot_and_preserves_its_historical_revisions(
    binding: _BindingEnvironment,
) -> None:
    human = await _login(binding.human)
    first_code = await binding.service.issue_challenge(_normalized_message())
    assert first_code is not None
    await binding.service.bind(human.access_token, first_code)
    _execute(
        binding,
        "INSERT INTO channel_plugins (id,type,name,enabled,status,user_id,org_id,created_at,updated_at) "
        "VALUES ('plugin-b','lark','Other Moss',1,'connected',?,?,1,1)",
        (_IDS["homeMember"], _IDS["homeOrg"]),
    )
    _execute(
        binding,
        "INSERT INTO moss_im_connections VALUES ('plugin-b',?,?,'cli_app_b','tenant-a','r1',?,1)",
        (_IDS["homeMember"], _IDS["homeOrg"], _IDS["homeMember"]),
    )
    routes = CanonicalFeishuRouteService(
        binding.human.path,
        authority_id="retained-moss",
        channel_id="channel-b",
        plugin_id="plugin-b",
        connection_owner_id=_IDS["homeMember"],
        actual_app_id="cli_app_b",
    )
    second = CanonicalFeishuBindingService(
        binding.human.path,
        binding.human.service.snapshots,
        routes,
        writer=binding.human.writer,
        clock=lambda: binding.human.now[0],
    )
    sdk = _sdk_message()
    assert sdk.header is not None
    sdk.header.app_id = "cli_app_b"
    channel = FeishuChannel(_processor, FeishuConfig(app_id="cli_app_b"), channel_id="channel-b")
    channel._running = True
    queued = []
    channel._enqueue_callback = queued.append
    channel._add_reaction_async = MagicMock()
    channel._on_message_event(sdk)
    assert len(queued) == 1
    second_code = await second.issue_challenge(channel.parse_inbound(queued[0]))
    assert second_code is not None
    await second.bind(human.access_token, second_code)
    first_views = await binding.service.bindings(human.access_token)
    second_views = await second.bindings(human.access_token)
    assert len(first_views) == len(second_views) == 1
    assert first_views[0].plugin_id == "plugin-a" and first_views[0].is_current
    assert second_views[0].plugin_id == "plugin-b" and second_views[0].is_current
    assert await routes.resolve(channel.parse_inbound(queued[0])) is not None
    _execute(binding, "UPDATE moss_im_connections SET revision='r2' WHERE plugin_id='plugin-a'")
    old_views = await binding.service.bindings(human.access_token)
    assert len(old_views) == 1 and not old_views[0].is_current
    next_code = await binding.service.issue_challenge(_normalized_message())
    assert next_code is not None
    await binding.service.bind(human.access_token, next_code)
    current_and_history = await binding.service.bindings(human.access_token)
    assert len(current_and_history) == 2
    assert {view.is_current for view in current_and_history} == {False, True}
    assert all(view.plugin_id == "plugin-a" for view in current_and_history)
    assert (await second.bindings(human.access_token))[0].is_current
