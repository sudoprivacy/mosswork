"""Original canonical review table, immutable snapshots and scoped recovery."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.unit.db.test_retained_cohost import retained as retained
from tests.unit.db.test_retained_ontology import registered as registered

from octop.infra.db.repos.retained_cohost import RetainedCohostBindingRepo
from octop.infra.db.repos.retained_ontology import RetainedOntologyBindingRepo
from octop.infra.db.repos.retained_review import (
    RetainedReviewDocumentRepo,
    ReviewDocumentUnavailable,
)

ENDPOINT = "/review/" + "a" * 32 + "/v1"


@pytest.fixture
def review_store(registered: Path) -> Path:
    with closing(sqlite3.connect(registered)) as db:
        db.executescript(
            (Path(__file__).parent / "fixtures/retained_review_103.sql").read_text(encoding="utf-8")
        )
    return registered


def original_scope(path: Path):
    return (
        RetainedCohostBindingRepo(path).get_for_resume(
            "app-original", organization_id="org", owner_id="owner"
        ),
        RetainedOntologyBindingRepo(path).resolve_original(
            "app-original", organization_id="org", owner_id="owner"
        ),
    )


def save(path: Path, **overrides: Any) -> None:
    RetainedReviewDocumentRepo(path).save(
        *original_scope(path),
        ENDPOINT,
        **{
            "record": {"endpoint": ENDPOINT, "fixture": "domain validation is separate"},
            "html": "<!doctype html><p>原始模型 🧩</p>",
            "account_id": "capturing-human-account",
            "actor_id": "capturing-admin-member",
            **overrides,
        },
    )


def test_committed_original_snapshot_survives_repo_reopen_without_other_tables_changed(
    review_store: Path,
) -> None:
    save(review_store)
    save(review_store)  # Same original snapshot confirms the existing record.
    before = review_store.read_bytes()
    original, registered = original_scope(review_store)
    value = RetainedReviewDocumentRepo(review_store).load(original, registered, ENDPOINT)
    assert value.html == "<!doctype html><p>原始模型 🧩</p>"
    assert value.account_id == "capturing-human-account"
    assert value.actor_id == "capturing-admin-member" and value.actor_id != original.owner_id
    assert "原始" not in repr(value) and "capturing" not in repr(value)
    assert review_store.read_bytes() == before
    with closing(sqlite3.connect(review_store)) as db:
        assert db.execute("SELECT count(*) FROM ontology_review_values").fetchone()[0] == 1
        assert db.execute("SELECT durable_session_id FROM cohost_sessions").fetchone()[0] == (
            original.durable_session_id
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"html": "changed HTML"},
        {"record": {"endpoint": ENDPOINT, "fixture": "new digest"}},
        {"actor_id": "replacement-member"},
        {"account_id": "replacement-account"},
    ],
)
def test_an_existing_endpoint_never_rebinds_or_overwrites_first_snapshot(
    review_store: Path, changed: dict[str, Any]
) -> None:
    save(review_store)
    before = review_store.read_bytes()
    with pytest.raises(ReviewDocumentUnavailable, match="cannot be rebound"):
        save(review_store, **changed)
    assert review_store.read_bytes() == before


@pytest.mark.parametrize("part", ["native", "registry", "owner", "application", "organization"])
def test_other_native_or_model_binding_cannot_recover_original_frame(
    review_store: Path, part: str
) -> None:
    save(review_store)
    original, registered = original_scope(review_store)
    if part == "native":
        original = replace(original, durable_session_id="replacement")
    elif part == "registry":
        registered = replace(registered, generation=registered.generation + 1)
    elif part == "owner":
        original = replace(original, owner_id="other")
        registered = replace(registered, owner_id="other")
    elif part == "application":
        original = replace(original, application_session_id="other")
        registered = replace(registered, application_session_id="other")
    else:
        original = replace(original, organization_id="other")
        registered = replace(registered, organization_id="other")
    with pytest.raises(ReviewDocumentUnavailable):
        RetainedReviewDocumentRepo(review_store).load(original, registered, ENDPOINT)


def test_legacy_revision_only_value_never_becomes_a_new_served_record(review_store: Path) -> None:
    with closing(sqlite3.connect(review_store)) as db, db:
        db.execute(
            "INSERT INTO ontology_review_values VALUES (?, ?, ?)",
            ("org", "review_binding:original", json.dumps({"revision": "b" * 64})),
        )
    with pytest.raises(ReviewDocumentUnavailable):
        RetainedReviewDocumentRepo(review_store).load(*original_scope(review_store), ENDPOINT)


@pytest.mark.parametrize("corrupted", ["null", "[]", "not JSON", "{}"])
def test_malformed_canonical_value_refuses_without_repair(
    review_store: Path, corrupted: str
) -> None:
    save(review_store)
    with closing(sqlite3.connect(review_store)) as db, db:
        db.execute("UPDATE ontology_review_values SET value_json=?", (corrupted,))
    before = review_store.read_bytes()
    with pytest.raises(ReviewDocumentUnavailable):
        RetainedReviewDocumentRepo(review_store).load(*original_scope(review_store), ENDPOINT)
    assert review_store.read_bytes() == before


def test_missing_database_and_original_schema_are_never_initialized(
    review_store: Path, tmp_path: Path
) -> None:
    original, registered = original_scope(review_store)
    missing = tmp_path / "missing.sqlite"
    repo = RetainedReviewDocumentRepo(missing)
    with pytest.raises(ReviewDocumentUnavailable):
        repo.load(original, registered, ENDPOINT)
    with pytest.raises(ReviewDocumentUnavailable):
        repo.save(
            original,
            registered,
            ENDPOINT,
            record={"endpoint": ENDPOINT},
            html="HTML",
            account_id="account",
            actor_id="member",
        )
    assert not missing.exists()
    with closing(sqlite3.connect(review_store)) as db:
        db.execute("DROP TABLE ontology_review_values")
    with pytest.raises(ReviewDocumentUnavailable):
        save(review_store)
    with closing(sqlite3.connect(review_store)) as db:
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='ontology_review_values'"
        ).fetchall()
