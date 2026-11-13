"""Original SQL/HTTP capture and recovery with fixture grants and writer custody."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.connectors.test_fixed_json import server
from tests.unit.native_host.test_human_review import (
    _ENDPOINT,
    _IDS,
    _POINTER,
    compose,
    response,
    review_row,
)
from tests.unit.native_host.test_human_review import (
    human as human,
)

from octop.infra.db.repos.retained_ontology import RetainedModelInvalid, RetainedModelUnavailable
from octop.infra.db.repos.retained_review import (
    RetainedHumanReviewDocumentRepo,
    ReviewDocumentUnavailable,
)
from octop.infra.errors import MossworkError
from octop.infra.sudograph.protocol import ReviewDocument, SudographError

_HTML = "<html><body>original human review 🐈</body></html>"


class Writer:
    def __init__(self, *, fail_at=None, result=None):
        self.calls, self.fail_at, self.result = 0, fail_at, result

    async def assert_current_writer(self):
        self.calls += 1
        if self.calls == self.fail_at:
            raise PermissionError("fixture writer withdrawn")
        return self.result


@pytest.fixture
async def pages(human):
    with sqlite3.connect(human.path) as db:
        db.executescript(
            (Path(__file__).parents[1] / "db/fixtures/retained_review_103.sql").read_text()
        )
    _, reader = compose(human)
    return SimpleNamespace(
        human=human, reader=reader, store=RetainedHumanReviewDocumentRepo(human.path)
    )


def document(pages, *, html=_HTML):
    return ReviewDocument.from_worker(pages.reader.binding, _ENDPOINT, {**_POINTER, "html": html})


def persist(pages, doc=None):
    doc = doc or document(pages)
    pages.store.save(
        pages.human.project,
        _ENDPOINT,
        record=doc.record(),
        html=doc.html,
        account_id=_IDS["account"],
        actor_id=_IDS["homeMember"],
    )


def row_count(pages):
    with sqlite3.connect(pages.human.path) as db:
        return db.execute("SELECT count(*) FROM ontology_review_values").fetchone()[0]


async def capture(pages, *, writer=None):
    payload = response({**_POINTER, "html": _HTML})
    async with server(
        lambda _: (200, {"Content-Type": "application/json"}, json.dumps(payload).encode())
    ) as (port, requests):
        review_row(pages.human, f"http://127.0.0.1:{port}/mcp")
        result = await pages.reader.capture_review(
            _ENDPOINT, pages.store, writer=writer or Writer()
        )
        assert len(requests) == 1
        return result


async def test_capture_commits_exact_actor_project_pointer_html_before_return_and_offline_restore(
    pages,
):
    writer = Writer()
    captured = await capture(pages, writer=writer)
    assert writer.calls == 3
    assert captured == document(pages)
    restored = await pages.reader.restore_review(_ENDPOINT, pages.store)
    assert restored == captured and row_count(pages) == 1
    with sqlite3.connect(pages.human.path) as db:
        value = json.loads(
            db.execute("SELECT value_json FROM ontology_review_values").fetchone()[0]
        )
        assert value["project"]["project_id"] == "project"
        assert value["project"]["generation"] == 1
        assert value["account_id"] == _IDS["account"]
        assert value["actor_id"] == _IDS["homeMember"]
        assert value["record"] == captured.record() and value["html"] == _HTML
        assert set(value) == {"format", "project", "account_id", "actor_id", "record", "html"}
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('sessions','agents','ontology_session_bindings')"
        ).fetchall()
        assert db.execute("SELECT DISTINCT session_id FROM mcp_audit_log").fetchall() == [(None,)]


@pytest.mark.parametrize("fail_at", [1, 2, 3])
async def test_writer_refusal_withholds_page_and_preserves_possible_commit(pages, fail_at):
    with pytest.raises(MossworkError):
        await capture(pages, writer=Writer(fail_at=fail_at))
    assert row_count(pages) == (1 if fail_at == 3 else 0)
    if fail_at == 3:
        # Original worker is already closed; recovery reads the same saved bytes.
        assert await pages.reader.restore_review(_ENDPOINT, pages.store) == document(pages)


@pytest.mark.parametrize("result", [False, True, {"allowed": True}])
async def test_non_none_writer_verdict_refuses_before_network_or_store(pages, result):
    with pytest.raises(MossworkError):
        await pages.reader.capture_review(_ENDPOINT, pages.store, writer=Writer(result=result))
    assert pages.human.audits == [] and row_count(pages) == 0


async def test_missing_original_table_refuses_without_creating_schema(pages):
    with sqlite3.connect(pages.human.path) as db:
        db.execute("DROP TABLE ontology_review_values")
    with pytest.raises(ReviewDocumentUnavailable):
        await capture(pages)
    with sqlite3.connect(pages.human.path) as db:
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='ontology_review_values'"
        ).fetchall()


async def test_store_must_be_same_original_project_database_before_io(pages, tmp_path):
    store = RetainedHumanReviewDocumentRepo(tmp_path / "another.sqlite")
    with pytest.raises(PermissionError):
        await pages.reader.capture_review(_ENDPOINT, store, writer=Writer())
    with pytest.raises(PermissionError):
        await pages.reader.restore_review(_ENDPOINT, store)
    assert not (tmp_path / "another.sqlite").exists()
    assert pages.human.audits == [] and row_count(pages) == 0


async def test_revocation_after_commit_withholds_page_and_explicit_restore_never_refetches(
    pages, monkeypatch
):
    original = RetainedHumanReviewDocumentRepo.save

    def save_then_revoke(self, *args, **kwargs):
        original(self, *args, **kwargs)
        pages.human.allowed = False

    monkeypatch.setattr(RetainedHumanReviewDocumentRepo, "save", save_then_revoke)
    with pytest.raises(PermissionError):
        await capture(pages)
    assert row_count(pages) == 1
    with pytest.raises(PermissionError):
        await pages.reader.restore_review(_ENDPOINT, pages.store)
    assert pages.human.audits[-1] == "error"
    pages.human.allowed = True
    assert await pages.reader.restore_review(_ENDPOINT, pages.store) == document(pages)


async def test_cancelled_persistence_wait_releases_no_page_even_if_original_sql_committed(
    pages, monkeypatch
):
    original = RetainedHumanReviewDocumentRepo.save
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def save_then_wait(self, *args, **kwargs):
        original(self, *args, **kwargs)
        entered.set()
        try:
            assert release.wait(5)
        finally:
            finished.set()

    monkeypatch.setattr(RetainedHumanReviewDocumentRepo, "save", save_then_wait)
    task = asyncio.create_task(capture(pages))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled() and row_count(pages) == 1
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    assert await pages.reader.restore_review(_ENDPOINT, pages.store) == document(pages)


async def test_restore_missing_snapshot_refuses_and_does_not_contact_worker(pages):
    with pytest.raises(ReviewDocumentUnavailable):
        await pages.reader.restore_review(_ENDPOINT, pages.store)
    assert pages.human.audits == ["error"] and row_count(pages) == 0


async def test_same_tuple_confirms_identical_bytes_and_refuses_rebinding(pages):
    persist(pages)
    persist(pages)
    with pytest.raises(ReviewDocumentUnavailable):
        persist(pages, document(pages, html="<html>replacement</html>"))
    assert row_count(pages) == 1
    assert await pages.reader.restore_review(_ENDPOINT, pages.store) == document(pages)


@pytest.mark.parametrize("field", ["account_id", "actor_id"])
async def test_different_actor_tuple_cannot_restore_original(pages, field):
    persist(pages)
    ids = {"account_id": _IDS["account"], "actor_id": _IDS["homeMember"], field: "other-original"}
    with pytest.raises(ReviewDocumentUnavailable):
        pages.store.load(pages.human.project, _ENDPOINT, **ids)


@pytest.mark.parametrize(
    "column,value", [("generation", 2), ("enabled", 0), ("model_ref", "other")]
)
async def test_project_change_inside_sql_refuses_save_and_restore(pages, column, value):
    persist(pages)
    with sqlite3.connect(pages.human.path) as db:
        db.execute(f"UPDATE ontology_projects SET {column}=?", (value,))
    with pytest.raises((RetainedModelInvalid, RetainedModelUnavailable, ReviewDocumentUnavailable)):
        persist(pages)
    with pytest.raises((RetainedModelInvalid, RetainedModelUnavailable, ReviewDocumentUnavailable)):
        await pages.reader.restore_review(_ENDPOINT, pages.store)
    assert row_count(pages) == 1


@pytest.mark.parametrize("field", ["html", "record", "project", "account_id", "actor_id", "format"])
async def test_tampered_stored_bytes_or_binding_never_released(pages, field):
    persist(pages)
    with sqlite3.connect(pages.human.path) as db:
        value = json.loads(
            db.execute("SELECT value_json FROM ontology_review_values").fetchone()[0]
        )
        if field == "record":
            value["record"]["revision"] = "d" * 64
        elif field == "project":
            value["project"]["generation"] = 2
        else:
            value[field] = "altered"
        db.execute("UPDATE ontology_review_values SET value_json=?", (json.dumps(value),))
    with pytest.raises((ReviewDocumentUnavailable, SudographError)):
        await pages.reader.restore_review(_ENDPOINT, pages.store)
    assert pages.human.audits == ["error"]


async def test_restore_audit_wait_rechecks_human_before_release(pages):
    persist(pages)

    async def audit(*args):
        await pages.human.audit(*args)
        if args[-1] == "success":
            pages.human.allowed = False

    _, reader = compose(pages.human, audit=audit)
    with pytest.raises(PermissionError):
        await reader.restore_review(_ENDPOINT, pages.store)
    assert pages.human.audits == ["success", "error"]


async def test_human_capture_never_creates_a_session_document_binding(pages):
    persist(pages)
    with sqlite3.connect(pages.human.path) as db:
        keys = [row[0] for row in db.execute("SELECT key FROM ontology_review_values")]
    assert len(keys) == 1 and keys[0].startswith("served_human_review/v1:")
    assert not keys[0].startswith("served_review/v1:")
    assert row_count(pages) == 1
