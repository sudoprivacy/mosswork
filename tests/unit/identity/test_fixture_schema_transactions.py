"""Real fixture transactions preserve retained schemas, constraints and isolation."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from tests.unit.identity import test_canonical_business_composition as business
from tests.unit.identity import test_canonical_organization_members_port as organization
from tests.unit.identity import test_human_sessions as human_sessions
from tests.unit.identity import test_human_tokens as tokens
from tests.unit.identity.test_canonical_business_composition import environment as environment
from tests.unit.identity.test_canonical_human_request_projection import product as product
from tests.unit.identity.test_canonical_organization_members_port import members as members
from tests.unit.identity.test_human_sessions import sessions as sessions
from tests.unit.identity.test_human_tokens import legacy_db as legacy_db


@pytest.mark.parametrize(
    "fixture_name,module,explicit_foreign_keys",
    [
        ("legacy_db", tokens, 1),
        ("sessions", human_sessions, None),
        ("members", organization, None),
        ("environment", business, None),
    ],
    ids=["legacy-token", "human-session", "organization-member", "business-composition"],
)
def test_schema_setup_is_one_transaction_with_original_connection_modes(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_name: str,
    module: ModuleType,
    explicit_foreign_keys: int | None,
) -> None:
    connect = sqlite3.connect
    with closing(connect(tmp_path / "default-settings.sqlite")) as defaults:
        expected = {
            key: defaults.execute("PRAGMA " + key).fetchone()[0]
            for key in ("synchronous", "journal_mode", "foreign_keys")
        }
    if explicit_foreign_keys is not None:
        expected["foreign_keys"] = explicit_foreign_keys
    connections: list[sqlite3.Connection] = []
    traced: list[tuple[str, bool]] = []

    def observe(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = connect(*args, **kwargs)
        connections.append(connection)
        connection.set_trace_callback(lambda sql: traced.append((sql, connection.in_transaction)))
        return connection

    try:
        # Replace only the test module's dependency; framework SQLite stays unchanged.
        with monkeypatch.context() as patch:
            patch.setattr(module, "sqlite3", SimpleNamespace(connect=observe))
            request.getfixturevalue(fixture_name)
        assert len(connections) == 1
        connection = connections[0]
        ddl = [
            active
            for sql, active in traced
            if sql.lstrip().upper().startswith(("CREATE ", "ALTER "))
        ]
        assert ddl and all(ddl)
        assert not connection.in_transaction
        actual = {key: connection.execute("PRAGMA " + key).fetchone()[0] for key in expected}
        assert actual == expected
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        for connection in connections:
            connection.set_trace_callback(None)
            connection.close()


def test_each_token_fixture_still_has_an_independent_database(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_database = tokens.legacy_db.__wrapped__(first)
    second_database = tokens.legacy_db.__wrapped__(second)
    member = tokens._IDS["homeMember"]
    with closing(sqlite3.connect(first_database)) as db, db:
        db.execute("UPDATE users SET status='disabled' WHERE id=?", (member,))
    with closing(sqlite3.connect(second_database)) as db:
        assert db.execute("SELECT status FROM users WHERE id=?", (member,)).fetchone() == (
            "active",
        )


def test_token_fixture_keeps_the_original_unique_phone_constraint(legacy_db: Path) -> None:
    with closing(sqlite3.connect(legacy_db)) as db:
        phone = db.execute(
            "SELECT phone FROM users WHERE id=?", (tokens._IDS["homeMember"],)
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE users SET phone=? WHERE id=?", (phone, tokens._IDS["companyMember"]))
        db.rollback()


def test_failed_token_fixture_setup_commits_no_partial_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tokens,
        "_FROZEN_SCHEMA",
        tokens._FROZEN_SCHEMA + "CREATE TRIGGER reject_fixture_user BEFORE INSERT ON users "
        "BEGIN SELECT RAISE(ABORT,'fixture-seed-aborted'); END;",
    )
    with pytest.raises(sqlite3.IntegrityError, match="fixture-seed-aborted"):
        tokens.legacy_db.__wrapped__(tmp_path)
    with closing(sqlite3.connect(tmp_path / "original-canonical.sqlite")) as db:
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            == []
        )
