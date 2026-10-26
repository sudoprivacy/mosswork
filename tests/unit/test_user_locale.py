"""tests/unit/test_user_locale.py"""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserRepo
from octop.infra.utils.locale import normalize_locale, resolve_locale


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "mosswork.db")
    run_migrations(pool)
    return pool


def test_users_table_has_locale_default_zh(db: SqlitePool):
    uid = UserRepo(db).create(username="u", password_hash="h", role="user")
    row = UserRepo(db).get(uid)
    assert row is not None
    assert row.locale == "zh"


def test_user_repo_create_accepts_explicit_locale(db: SqlitePool):
    uid = UserRepo(db).create(username="en-user", password_hash="h", role="admin", locale="en")
    row = UserRepo(db).get(uid)
    assert row is not None
    assert row.locale == "en"


def test_legacy_users_table_gets_locale_column(tmp_path: Path):
    from octop.infra.db import migrate

    pool = SqlitePool(tmp_path / "legacy.db")
    for version, path in migrate._discover():
        if version <= 8:
            migrate._apply_sqlite_migration(pool, version, path)
    with pool.connect() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) "
            "VALUES ('legacy', 'h', 'admin', 0)"
        )
    run_migrations(pool)
    row = UserRepo(pool).get_by_username("legacy")
    assert row is not None
    assert row.locale == "zh"


def test_set_locale_persists(db: SqlitePool):
    repo = UserRepo(db)
    uid = repo.create(username="u", password_hash="h", role="user")
    repo.set_locale(uid, "en")
    row = repo.get(uid)
    assert row is not None
    assert row.locale == "en"


def test_resolve_locale_prefers_user_setting():
    assert resolve_locale(user_locale="en", channel_type="feishu") == "en"
    assert resolve_locale(user_locale="zh", channel_type="telegram") == "zh"


def test_normalize_locale_bcp47():
    assert normalize_locale("zh-CN") == "zh"
    assert normalize_locale("en-US") == "en"
