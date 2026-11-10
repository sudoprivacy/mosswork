"""Read retained binding facts against the actual pinned public SQLite schema."""

from __future__ import annotations

import dataclasses
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from octop.infra.db.repos.retained_cohost import (
    RetainedBindingInvalid,
    RetainedBindingMissing,
    RetainedCohostBindingRepo,
    RetainedSessionUnavailable,
    RetainedStoreUnavailable,
)


@pytest.fixture
def retained(tmp_path: Path) -> Path:
    path = tmp_path / "retained canonical.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            (Path(__file__).parent / "fixtures/retained_cohost_103.sql").read_text(encoding="utf-8")
        )
        connection.execute("INSERT INTO organizations VALUES ('org', 'Original organization', 1)")
        connection.execute(
            """INSERT INTO users (id, org_id, email, name, created_at)
               VALUES ('owner', 'org', 'synthetic@example.test', 'Synthetic owner', 1)"""
        )
        connection.execute(
            """INSERT INTO sessions (session_id, transcript_session_id, org_id, user_id,
                role, scopes_json, cwd, runtime_type, status, desired_state,
                transcript_path, created_at, last_active_at)
                VALUES ('app-original', 'view-transcript', 'org', 'owner',
                'user', '[]', '/original/repository', 'cohost', 'ended', 'stopped',
                '/original/transcript.jsonl', 1, 2)"""
        )
        connection.execute(
            """INSERT INTO cohost_sessions VALUES
                ('app-original', 'owner', 'moss-original', 'native_history_1',
                 '/original/repository')"""
        )
        connection.commit()
    return path


def _change(path: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(sql, params)
        connection.commit()


def _read(repo: RetainedCohostBindingRepo):
    return repo.get_for_resume("app-original", organization_id="org", owner_id="owner")


def test_original_durable_identity_is_distinct_and_database_is_unchanged(retained: Path) -> None:
    before = retained.read_bytes()
    repo = RetainedCohostBindingRepo(retained)
    binding = _read(repo)
    assert binding.application_session_id == "app-original"
    assert binding.durable_session_id == "native_history_1"
    assert binding.native_agent_id == "moss-original"
    assert binding.owner_id == "owner" and binding.organization_id == "org"
    assert binding.repository_path == "/original/repository"
    assert repo.assert_current(binding) is None
    assert "owner" not in repr(binding)
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.owner_id = "other"  # type: ignore[misc]
    assert retained.read_bytes() == before


@pytest.mark.parametrize(
    ("session", "org", "owner"),
    [
        ("unknown", "org", "owner"),
        ("app-original", "foreign-org", "owner"),
        ("app-original", "org", "foreign-owner"),
        ("app-original' OR 1=1 --", "org", "owner"),
    ],
)
def test_foreign_selectors_cannot_read_original_binding(
    retained: Path, session: str, org: str, owner: str
) -> None:
    with pytest.raises(RetainedSessionUnavailable):
        RetainedCohostBindingRepo(retained).get_for_resume(
            session, organization_id=org, owner_id=owner
        )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE sessions SET org_id = 'other'",
        "UPDATE users SET org_id = 'other'",
        "UPDATE users SET status = 'disabled'",
        "UPDATE users SET status = 'locked'",
        "UPDATE users SET status = 'pending'",
        "UPDATE sessions SET deleted_at = 1",
        "UPDATE sessions SET runtime_type = 'host'",
        "UPDATE sessions SET runtime_type = 'k8s'",
        "DELETE FROM sessions",
        "DELETE FROM users",
    ],
)
def test_changed_owner_or_non_cohost_data_refuses_without_replacement(
    retained: Path, sql: str
) -> None:
    repo = RetainedCohostBindingRepo(retained)
    original = _read(repo)
    _change(retained, sql)
    before = retained.read_bytes()
    with pytest.raises(RetainedSessionUnavailable):
        _read(repo)
    with pytest.raises(RetainedSessionUnavailable):
        repo.assert_current(original)
    assert retained.read_bytes() == before


def test_unpublished_binding_is_not_new_or_empty_native_history(retained: Path) -> None:
    _change(retained, "DELETE FROM cohost_sessions")
    before = retained.read_bytes()
    with pytest.raises(RetainedBindingMissing):
        _read(RetainedCohostBindingRepo(retained))
    assert retained.read_bytes() == before
    with closing(sqlite3.connect(retained)) as connection:
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM cohost_sessions").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("owner_user_id", "foreign-owner"),
        ("native_agent_id", "bad/agent"),
        ("native_agent_id", "bad\\agent"),
        ("native_agent_id", "bad\x00agent"),
        ("native_agent_id", "📝" * 129),
        ("durable_session_id", ""),
        ("durable_session_id", "history/other"),
        ("repository_path", "relative/workspace"),
        ("repository_path", "/original/../other"),
        ("repository_path", "/original\\other"),
    ],
)
def test_corrupt_binding_refuses_original_schema_violations(
    retained: Path, column: str, value: str
) -> None:
    assert column in {"owner_user_id", "native_agent_id", "durable_session_id", "repository_path"}
    _change(retained, f"UPDATE cohost_sessions SET {column} = ?", (value,))
    with pytest.raises(RetainedBindingInvalid):
        _read(RetainedCohostBindingRepo(retained))


def test_changed_durable_history_never_passes_original_revalidation(retained: Path) -> None:
    repo = RetainedCohostBindingRepo(retained)
    original = _read(repo)
    _change(retained, "UPDATE cohost_sessions SET durable_session_id = 'other_history'")
    with pytest.raises(RetainedBindingInvalid, match="Original binding changed"):
        repo.assert_current(original)
    assert _read(repo).durable_session_id == "other_history"


def test_missing_database_and_product_schema_are_unavailable_not_empty(
    tmp_path: Path, retained: Path
) -> None:
    missing = tmp_path / "absent.sqlite"
    with pytest.raises(RetainedStoreUnavailable):
        _read(RetainedCohostBindingRepo(missing))
    assert not missing.exists()
    _change(retained, "DROP TABLE cohost_sessions")
    before = retained.read_bytes()
    with pytest.raises(RetainedStoreUnavailable):
        _read(RetainedCohostBindingRepo(retained))
    assert retained.read_bytes() == before


def test_unicode_original_paths_are_preserved_without_local_path_reinterpretation(
    retained: Path,
) -> None:
    _change(retained, "UPDATE cohost_sessions SET repository_path = ?", ("/用户/项目 📝",))
    assert _read(RetainedCohostBindingRepo(retained)).repository_path == "/用户/项目 📝"
