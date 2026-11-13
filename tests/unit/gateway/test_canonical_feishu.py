"""Current human/connection boundaries against the retained real SQLite tables."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from tests.unit.gateway.test_personal_moss_im_identity import _normalized_message
from tests.unit.identity.test_canonical import (
    _ACCOUNT,
    _HOME_MEMBER,
    _HOME_ORG,
)
from tests.unit.identity.test_canonical import (
    canonical_db as canonical_db,
)

from octop.infra.errors import MossworkError
from octop.infra.gateway.canonical_feishu import CanonicalFeishuRouteService
from octop.infra.gateway.personal_moss_identity import FeishuMemberConnection

_SECOND_MEMBER = "9fbb17db-3d1d-4f8e-bd81-f828536d940a"
_SECOND_ACCOUNT = "aa754d7f-66ad-4d90-b3e6-6e9c682bf654"

# Frozen relevant DDL from sudoprivacy/moss@d0b84d4:
# db.ts:343, identity/identityRepository.ts:195, mossImIdentity.ts:13.
_IM_SCHEMA = """
CREATE TABLE channel_plugins (
    id TEXT NOT NULL, type TEXT NOT NULL, name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0, credentials_json TEXT, config_json TEXT,
    status TEXT NOT NULL, last_connected INTEGER, user_id TEXT NOT NULL,
    org_id TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    PRIMARY KEY (id, user_id)
);
CREATE TABLE moss_im_connections (
    plugin_id TEXT NOT NULL, connection_owner_id TEXT NOT NULL, org_id TEXT NOT NULL,
    app_id TEXT NOT NULL, tenant_key TEXT NOT NULL, revision TEXT NOT NULL,
    updated_by TEXT NOT NULL, updated_at BIGINT NOT NULL,
    PRIMARY KEY (plugin_id, connection_owner_id)
);
CREATE TABLE user_auth_identities (
    id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
    user_id TEXT NOT NULL REFERENCES users(id), provider TEXT NOT NULL,
    issuer TEXT NOT NULL, normalized_subject TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL, UNIQUE (provider, issuer, normalized_subject),
    UNIQUE (user_id, provider, issuer)
);
CREATE TABLE moss_im_conversations (
    conversation_key TEXT PRIMARY KEY, org_id TEXT NOT NULL, user_id TEXT NOT NULL,
    agent_ref TEXT NOT NULL, connection_scope TEXT NOT NULL, chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL, updated_at BIGINT NOT NULL
);
"""


def _connection() -> FeishuMemberConnection:
    return FeishuMemberConnection(
        _HOME_ORG, _HOME_MEMBER, "plugin-a", "tenant-a", "cli_app_a", "r1", "channel-a"
    )


def _service(path: Path) -> CanonicalFeishuRouteService:
    return CanonicalFeishuRouteService(
        path,
        authority_id="retained-authority",
        channel_id="channel-a",
        plugin_id="plugin-a",
        connection_owner_id=_HOME_MEMBER,
        actual_app_id="cli_app_a",
    )


@pytest.fixture
def im_db(canonical_db: Path) -> Path:
    with sqlite3.connect(canonical_db) as db:
        db.executescript(_IM_SCHEMA)
        db.execute(
            "INSERT INTO channel_plugins (id, type, name, enabled, status, user_id, org_id, "
            "created_at, updated_at) VALUES ('plugin-a', 'lark', 'Moss', 1, 'connected', ?, ?, 1, 1)",
            (_HOME_MEMBER, _HOME_ORG),
        )
        db.execute(
            "INSERT INTO moss_im_connections VALUES ('plugin-a', ?, ?, 'cli_app_a', "
            "'tenant-a', 'r1', ?, 1)",
            (_HOME_MEMBER, _HOME_ORG, _HOME_MEMBER),
        )
        db.execute(
            "INSERT INTO users (id, org_id, email, name, role, status, membership_revision, created_at) "
            "VALUES (?, ?, 'second@example.test', 'second', 'user', 'active', 4, 1)",
            (_SECOND_MEMBER, _HOME_ORG),
        )
        db.execute(
            "INSERT INTO human_accounts VALUES (?, '13900000002', 'active', 1)", (_SECOND_ACCOUNT,)
        )
        db.execute(
            "INSERT INTO human_account_memberships VALUES (?, ?, ?, 1)",
            (_SECOND_ACCOUNT, _HOME_ORG, _SECOND_MEMBER),
        )
        db.execute(
            "INSERT INTO moss_member_access VALUES (?, ?, 1, ?, 5)",
            (_HOME_ORG, _SECOND_MEMBER, _HOME_MEMBER),
        )
        db.executemany(
            "INSERT INTO user_auth_identities VALUES (?, ?, ?, 'feishu-moss', ?, ?, '{}', 1, 1)",
            [
                ("binding-a", _HOME_ORG, _HOME_MEMBER, _connection().scope, "ou_member_a"),
                ("binding-b", _HOME_ORG, _SECOND_MEMBER, _connection().scope, "ou_member_b"),
            ],
        )
    return canonical_db


async def test_two_members_in_one_group_keep_their_original_identity_and_pointer(
    im_db: Path,
) -> None:
    service = _service(im_db)
    first = await service.resolve(_normalized_message())
    assert first is not None
    assert (first.account_id, first.member.owner_id) == (_ACCOUNT, _HOME_MEMBER)
    assert first.session_pointer is None
    second_message = _normalized_message()
    second_message.metadata.update(sender_id="ou_member_b", sender_open_id="ou_member_b")
    assert second_message.channel_subject is not None
    second_message.channel_subject.metadata["sender_id"] = "ou_member_b"
    second = await service.resolve(second_message)
    assert second is not None
    assert (second.account_id, second.member.owner_id) == (_SECOND_ACCOUNT, _SECOND_MEMBER)
    assert first.connection == second.connection
    assert first.event.chat_id == second.event.chat_id
    assert first.identity != second.identity
    assert first.conversation_key != second.conversation_key
    assert second.member.owner_id != second.connection.owner_id
    with sqlite3.connect(im_db) as db:
        db.execute(
            "INSERT INTO moss_im_conversations VALUES (?, ?, ?, ?, ?, ?, 'original-session', 1)",
            (
                second.conversation_key,
                _HOME_ORG,
                _SECOND_MEMBER,
                second.identity.reference,
                second.connection.scope,
                second.event.chat_id,
            ),
        )
    resumed = await service.resolve(second_message)
    assert resumed is not None and resumed.session_pointer == "original-session"
    await service.assert_current(resumed)
    with pytest.raises(MossworkError):
        await service.assert_current(second)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE moss_im_connections SET revision = 'changed'",
        "UPDATE moss_im_connections SET app_id = 'cli_other'",
        "UPDATE moss_im_connections SET tenant_key = 'other'",
        "UPDATE channel_plugins SET enabled = 0",
        "UPDATE channel_plugins SET type = 'webhook'",
        "UPDATE channel_plugins SET org_id = 'foreign-org'",
        "UPDATE human_accounts SET status = 'disabled'",
        "UPDATE users SET status = 'disabled'",
        "UPDATE users SET membership_revision = membership_revision + 1",
        "DELETE FROM human_account_memberships",
        "UPDATE user_auth_identities SET id = 'replacement' WHERE id = 'binding-a'",
        "DELETE FROM user_auth_identities WHERE id = 'binding-a'",
    ],
)
async def test_current_route_refuses_changed_registration_identity_or_binding(
    im_db: Path, sql: str
) -> None:
    service = _service(im_db)
    route = await service.resolve(_normalized_message())
    assert route is not None
    with sqlite3.connect(im_db) as db:
        db.execute(sql)
    with pytest.raises(MossworkError) as caught:
        await service.assert_current(route)
    assert caught.value.status == 403


async def test_explicit_admin_revoke_prevents_resolve_and_reuse(im_db: Path) -> None:
    service = _service(im_db)
    route = await service.resolve(_normalized_message())
    assert route is not None
    with sqlite3.connect(im_db) as db:
        db.execute(
            "INSERT INTO moss_member_access VALUES (?, ?, 0, ?, 9)",
            (_HOME_ORG, _HOME_MEMBER, _HOME_MEMBER),
        )
    for operation in (service.resolve(_normalized_message()), service.assert_current(route)):
        with pytest.raises(MossworkError) as caught:
            await operation
        assert caught.value.status == 403


@pytest.mark.parametrize("field", ["org_id", "user_id", "agent_ref", "connection_scope", "chat_id"])
async def test_foreign_existing_conversation_is_not_resumed(im_db: Path, field: str) -> None:
    service = _service(im_db)
    route = await service.resolve(_normalized_message())
    assert route is not None
    with sqlite3.connect(im_db) as db:
        db.execute(
            "INSERT INTO moss_im_conversations VALUES (?, ?, ?, ?, ?, ?, 'original-session', 1)",
            (
                route.conversation_key,
                _HOME_ORG,
                _HOME_MEMBER,
                route.identity.reference,
                route.connection.scope,
                route.event.chat_id,
            ),
        )
        # field is the fixed parameter set above, never external input.
        db.execute(f"UPDATE moss_im_conversations SET {field} = 'foreign'")
    with pytest.raises(MossworkError) as caught:
        await service.resolve(_normalized_message())
    assert caught.value.status == 403


async def test_missing_binding_is_distinct_from_missing_authority(
    im_db: Path, tmp_path: Path
) -> None:
    with sqlite3.connect(im_db) as db:
        db.execute("DELETE FROM user_auth_identities")
    assert await _service(im_db).resolve(_normalized_message()) is None
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(MossworkError) as caught:
        await _service(missing).resolve(_normalized_message())
    assert caught.value.status == 503
    assert not missing.exists()


async def test_registration_is_not_enabled_by_user_editable_channel_config(im_db: Path) -> None:
    with sqlite3.connect(im_db) as db:
        db.execute("DELETE FROM moss_im_connections")
        db.execute('UPDATE channel_plugins SET config_json = \'{"identityMode":"member"}\'')
    with pytest.raises(MossworkError) as caught:
        await _service(im_db).resolve(_normalized_message())
    assert caught.value.status == 403


async def test_routes_from_another_authority_are_refused(im_db: Path) -> None:
    service = _service(im_db)
    route = await service.resolve(_normalized_message())
    assert route is not None
    with pytest.raises(MossworkError):
        await service.assert_current(replace(route, authority_id="other-authority"))
