"""Shared canonical transaction facts, with no issuer or permission fallback."""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from tests.unit.identity.test_human_sessions import (
    _IDS,
    _Environment,
    _login,
    _write,
)
from tests.unit.identity.test_human_sessions import (
    legacy_db as legacy_db,
)
from tests.unit.identity.test_human_sessions import (
    sessions as sessions,
)

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.current_human import assert_current_human


async def test_assertion_only_reads_in_existing_transaction(sessions: _Environment) -> None:
    pair = await _login(sessions)
    snapshot = await sessions.service.snapshots.snapshot(pair.access_token)
    before = sessions.path.read_bytes()
    with sqlite3.connect(sessions.path) as db:
        # Default tuple row factory is intentional: the helper does not mutate
        # a consumer's connection or require its own DB pool/schema.
        db.execute("BEGIN")
        assert assert_current_human(db, snapshot, lambda: sessions.now[0]) is None
    assert sessions.path.read_bytes() == before


@pytest.mark.parametrize(
    "mutation",
    ["account", "member", "role", "revision", "revoked", "organization", "membership", "expiry"],
)
async def test_actual_current_facts_cannot_be_substituted_by_snapshot(
    sessions: _Environment, mutation: str
) -> None:
    pair = await _login(sessions)
    snapshot = await sessions.service.snapshots.snapshot(pair.access_token)
    if mutation == "account":
        _write(
            sessions, "UPDATE human_accounts SET status='disabled' WHERE id=?", (_IDS["account"],)
        )
    elif mutation == "member":
        _write(sessions, "UPDATE users SET status='disabled' WHERE id=?", (_IDS["homeMember"],))
    elif mutation == "role":
        _write(sessions, "UPDATE users SET role='user' WHERE id=?", (_IDS["homeMember"],))
    elif mutation == "revision":
        _write(sessions, "UPDATE users SET membership_revision=1 WHERE id=?", (_IDS["homeMember"],))
    elif mutation == "revoked":
        _write(
            sessions,
            "INSERT INTO revoked_tokens VALUES (?,?)",
            (snapshot.credential_id, snapshot.credential_expires_at),
        )
    elif mutation == "organization":
        # Simulate an orphaned legacy row after concurrent authoritative org
        # removal. Both consumers must retain the original organization JOIN.
        _write(sessions, "DELETE FROM organizations WHERE id=?", (_IDS["homeOrg"],))
    elif mutation == "membership":
        _write(
            sessions,
            "DELETE FROM human_account_memberships WHERE account_id=? AND org_id=?",
            (_IDS["account"], _IDS["homeOrg"]),
        )
    else:
        sessions.now[0] += 300
    with sqlite3.connect(sessions.path) as db:
        db.execute("BEGIN")
        with pytest.raises(MossworkError) as raised:
            assert_current_human(db, snapshot, lambda: sessions.now[0])
    assert raised.value.code == ErrorCode.AUTH_FAILED


@pytest.mark.parametrize("bad_clock", [math.nan, math.inf, -math.inf, -1, True, None, "now"])
async def test_invalid_clock_is_unavailable_not_an_expiry_bypass(
    sessions: _Environment, bad_clock: Any
) -> None:
    pair = await _login(sessions)
    snapshot = await sessions.service.snapshots.snapshot(pair.access_token)
    with sqlite3.connect(sessions.path) as db:
        db.execute("BEGIN")
        with pytest.raises(MossworkError) as raised:
            assert_current_human(db, snapshot, lambda: bad_clock)
    assert raised.value.status == 503
    assert raised.value.details == {"dependency": "canonical_current_human"}


async def test_transaction_must_already_exist(sessions: _Environment) -> None:
    pair = await _login(sessions)
    snapshot = await sessions.service.snapshots.snapshot(pair.access_token)
    with sqlite3.connect(sessions.path) as db, pytest.raises(MossworkError) as raised:
        assert_current_human(db, snapshot, lambda: sessions.now[0])
    assert raised.value.status == 503


async def test_missing_schema_is_not_initialized(sessions: _Environment, tmp_path: Path) -> None:
    pair = await _login(sessions)
    snapshot = await sessions.service.snapshots.snapshot(pair.access_token)
    with sqlite3.connect(tmp_path / "empty-fixture.sqlite") as db:
        db.execute("BEGIN")
        with pytest.raises(MossworkError) as raised:
            assert_current_human(db, snapshot, lambda: sessions.now[0])
        assert db.execute("SELECT * FROM sqlite_master").fetchall() == []
    assert raised.value.status == 503


async def test_snapshot_shape_is_not_a_raw_claim_dictionary(sessions: _Environment) -> None:
    await _login(sessions)
    with sqlite3.connect(sessions.path) as db:
        db.execute("BEGIN")
        with pytest.raises(MossworkError) as raised:
            assert_current_human(db, {"role": "admin"}, lambda: sessions.now[0])
    assert raised.value.status == 401


async def test_fact_assertion_does_not_require_or_grant_moss_use(sessions: _Environment) -> None:
    pair = await _login(sessions)
    _write(
        sessions,
        "INSERT INTO moss_member_access VALUES (?,?,0,?,1)",
        (_IDS["homeOrg"], _IDS["homeMember"], _IDS["homeMember"]),
    )
    snapshot = await sessions.service.snapshots.snapshot(pair.access_token)
    assert snapshot.personal_agent is None
    with sqlite3.connect(sessions.path) as db:
        db.execute("BEGIN")
        assert assert_current_human(db, snapshot, lambda: sessions.now[0]) is None
