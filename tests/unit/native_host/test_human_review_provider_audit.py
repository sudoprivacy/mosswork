"""Existing reader and real audit SQL; full grants/writer remain fixtures."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from tests.unit.connectors.test_fixed_json import server
from tests.unit.native_host.test_human_review import (
    _ENDPOINT,
    _IDS,
    _POINTER,
    change,
    response,
    review_row,
)
from tests.unit.native_host.test_human_review import human as human
from tests.unit.native_host.test_human_review_audit import Writer, rows
from tests.unit.native_host.test_human_review_documents import pages as pages

from octop.infra.db.repos.retained_mcp import RetainedReviewedMcpRepo
from octop.infra.db.repos.retained_model_audit import RetainedModelAuditRepo
from octop.infra.errors import MossworkError
from octop.infra.native_host.human_review_provider import CanonicalHumanReviewReaders
from octop.infra.sudograph.protocol import SudographError


def readers(human, writer, **overrides):
    return CanonicalHumanReviewReaders.with_canonical_audit(
        **{
            "projects": human.projects,
            "reviews": RetainedReviewedMcpRepo(human.path),
            "audits": RetainedModelAuditRepo(human.path),
            "writer": writer,
            "require_review_access": human.grant,
            "allowed_addresses": ("127.0.0.1",),
            **overrides,
        }
    )


async def test_same_reader_writer_and_sql_capture_then_offline_restore(pages):
    human, writer = pages.human, Writer()
    factory = readers(human, writer)
    html = "<html><body>original human document</body></html>"
    async with server(
        lambda _: (
            200,
            {"Content-Type": "application/json"},
            json.dumps(response({**_POINTER, "html": html})).encode(),
        )
    ) as (port, requests):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        reader = await factory(human.principal, project_id="project", endpoint=_ENDPOINT)
        captured = await reader.capture_review(_ENDPOINT, pages.store, writer=writer)
        assert len(requests) == 1
    restored = await (
        await factory(human.principal, project_id="project", endpoint=_ENDPOINT)
    ).restore_review(_ENDPOINT, pages.store)
    assert captured == restored and restored.html == html
    assert [(row["session_id"], row["user_id"], row["status"]) for row in rows(human)] == [
        (None, _IDS["homeMember"], "success"),
        (None, _IDS["homeMember"], "success"),
    ]
    with sqlite3.connect(human.path) as db:
        assert db.execute("SELECT count(*) FROM ontology_review_values").fetchone() == (1,)
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('agents','sessions')"
        ).fetchall()


async def test_construction_revocation_still_audits_original_member(human):
    writer = Writer()

    async def grant(*args, **kwargs):
        await human.grant(*args, **kwargs)
        change(human.path, "UPDATE users SET status='disabled'")

    with pytest.raises(MossworkError):
        await readers(human, writer, require_review_access=grant)(
            human.principal, project_id="project", endpoint=_ENDPOINT
        )
    (row,) = rows(human)
    assert row["user_id"] == _IDS["homeMember"] and row["session_id"] is None
    assert row["status"] == "error" and writer.calls == 2


async def test_postcommit_writer_loss_withholds_page_without_replay(human):
    writer = Writer(fail_at=2)
    async with server(
        lambda _: (
            200,
            {"Content-Type": "application/json"},
            json.dumps(response({**_POINTER, "html": "<html>withheld</html>"})).encode(),
        )
    ) as (port, requests):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        reader = await readers(human, writer)(
            human.principal, project_id="project", endpoint=_ENDPOINT
        )
        with pytest.raises(SudographError) as failure:
            await reader.review_document(_ENDPOINT)
        assert failure.value.code == "AUDIT_UNAVAILABLE" and len(requests) == 1
    assert len(rows(human)) == 1 and writer.calls == 2


def test_foreign_audit_store_refuses_before_any_access(human, tmp_path):
    with pytest.raises(ValueError):
        readers(human, Writer(), audits=RetainedModelAuditRepo(tmp_path / "foreign.sqlite"))
    assert human.grant_requests == [] and rows(human) == []


@pytest.mark.parametrize("writer", [None, False, {}])
def test_missing_writer_has_no_local_fallback(human, writer):
    with pytest.raises(ValueError):
        readers(human, writer)
    assert human.grant_requests == [] and rows(human) == []


async def test_cancel_during_composed_audit_stays_cancelled(human):
    waiting = asyncio.Event()

    class WaitingWriter(Writer):
        async def assert_current_writer(self):
            self.calls += 1
            waiting.set()
            await asyncio.Event().wait()

    writer = WaitingWriter()
    async with server(
        lambda _: (
            200,
            {"Content-Type": "application/json"},
            json.dumps(response({**_POINTER, "html": "<html>withheld</html>"})).encode(),
        )
    ) as (port, requests):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        reader = await readers(human, writer)(
            human.principal, project_id="project", endpoint=_ENDPOINT
        )
        task = asyncio.create_task(reader.review_document(_ENDPOINT))
        await asyncio.wait_for(waiting.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled() and len(requests) == 1
    assert rows(human) == [] and writer.calls == 1
