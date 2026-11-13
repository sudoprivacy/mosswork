"""Host grants and original canonical served-document persistence composition.

Uses real original-schema SQLite and the real domain parser; canonical/native
grants and the compiler reply remain fixtures, with no runtime/model dispatch.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from tests.unit.db.test_retained_cohost import _change
from tests.unit.db.test_retained_cohost import retained as retained
from tests.unit.db.test_retained_ontology import registered as registered
from tests.unit.db.test_retained_review import ENDPOINT
from tests.unit.native_host.test_sudograph import Setup
from tests.unit.native_host.test_sudograph import prepared as prepared

from octop.infra.db.repos.retained_cohost import RetainedCohostBindingRepo
from octop.infra.db.repos.retained_model_audit import ModelAuditUnavailable
from octop.infra.db.repos.retained_review import (
    RetainedReviewDocumentRepo,
    ReviewDocumentUnavailable,
)
from octop.infra.sudograph.protocol import ModelPointer, SudographError


@pytest.fixture
def documents(prepared: Setup) -> RetainedReviewDocumentRepo:
    with closing(sqlite3.connect(prepared.database)) as db:
        db.executescript(
            (Path(__file__).parent.parent / "db/fixtures/retained_review_103.sql").read_text(
                encoding="utf-8"
            )
        )
    model = prepared.ports.binding
    prepared.transport.response = {
        "format": "sudograph-worker-review/v2",
        "authority_ref": model.authority_ref,
        "model_ref": model.model_ref,
        "source_refs": list(model.source_refs),
        "scope": "full-registered-model",
        "resource_ref": model.resource_ref,
        "result": {
            "revision": "a" * 64,
            "context_digest": "b" * 64,
            "html": "<!doctype html><p>编译时的原始视图 🧩</p>",
        },
    }
    return RetainedReviewDocumentRepo(prepared.database)


def count(prepared: Setup) -> int:
    with closing(sqlite3.connect(prepared.database)) as db:
        return int(db.execute("SELECT count(*) FROM ontology_review_values").fetchone()[0])


async def test_capture_is_committed_before_return_and_restore_never_reads_latest_head(
    prepared: Setup, documents: RetainedReviewDocumentRepo
) -> None:
    served = await prepared.ports.capture_review(ENDPOINT, documents)
    assert count(prepared) == 1
    assert served.pointer == ModelPointer("a" * 64, "b" * 64)
    prepared.transport.response["result"] = {"revision": "c" * 64}
    restored = await prepared.ports.restore_review(
        ENDPOINT, RetainedReviewDocumentRepo(prepared.database)
    )
    assert restored == served
    assert len(prepared.transport.calls) == 1  # Only the original document request.
    assert prepared.transport.calls[0][2]["operation"] == "document"
    original = RetainedCohostBindingRepo(prepared.database).get_for_resume(
        "app-original", organization_id="org", owner_id="owner"
    )
    stored = documents.load(original, prepared.original, ENDPOINT)
    assert stored.account_id == "original-human-account"
    assert stored.actor_id == "authenticated-actor-member" != original.owner_id


@pytest.mark.parametrize("phase", ["capture", "restore"])
async def test_current_review_revocation_refuses_store_and_frame_access(
    prepared: Setup, documents: RetainedReviewDocumentRepo, phase: str
) -> None:
    if phase == "restore":
        await prepared.ports.capture_review(ENDPOINT, documents)
    original_calls = len(prepared.transport.calls)
    prepared.grants.allowed.remove("review")
    with pytest.raises((PermissionError, SudographError)):
        if phase == "capture":
            await prepared.ports.capture_review(ENDPOINT, documents)
        else:
            await prepared.ports.restore_review(ENDPOINT, documents)
    assert len(prepared.transport.calls) == original_calls
    assert count(prepared) == (1 if phase == "restore" else 0)


@pytest.mark.parametrize("field", ["revision", "context_digest", "html"])
async def test_legacy_or_malformed_compiler_document_cannot_be_persisted(
    prepared: Setup, documents: RetainedReviewDocumentRepo, field: str
) -> None:
    del prepared.transport.response["result"][field]
    with pytest.raises(SudographError):
        await prepared.ports.capture_review(ENDPOINT, documents)
    assert count(prepared) == 0 and len(prepared.transport.calls) == 1


@pytest.mark.parametrize("changed", ["digest", "html", "endpoint", "binding"])
async def test_altered_stored_frame_or_missing_digest_never_uses_current_head(
    prepared: Setup, documents: RetainedReviewDocumentRepo, changed: str
) -> None:
    await prepared.ports.capture_review(ENDPOINT, documents)
    with closing(sqlite3.connect(prepared.database)) as db:
        value = json.loads(
            db.execute("SELECT value_json FROM ontology_review_values").fetchone()[0]
        )
        if changed == "digest":
            del value["record"]["context_digest"]
        elif changed == "html":
            value["html"] = "replacement HTML"
        elif changed == "endpoint":
            value["record"]["endpoint"] = "/review/" + "b" * 32 + "/v1"
        else:
            value["session"]["durable_session_id"] = "replacement"
        with db:
            db.execute("UPDATE ontology_review_values SET value_json=?", (json.dumps(value),))
    with pytest.raises((ReviewDocumentUnavailable, SudographError)):
        await prepared.ports.restore_review(ENDPOINT, documents)
    assert len(prepared.transport.calls) == 1


async def test_changed_native_binding_refuses_original_frame_without_rebinding(
    prepared: Setup, documents: RetainedReviewDocumentRepo
) -> None:
    await prepared.ports.capture_review(ENDPOINT, documents)
    _change(prepared.database, "UPDATE cohost_sessions SET durable_session_id='replacement'")
    with pytest.raises(RuntimeError):
        await prepared.ports.restore_review(ENDPOINT, documents)
    assert count(prepared) == 1 and len(prepared.transport.calls) == 1


async def test_store_commit_failure_never_returns_a_frame_or_retries_compiler(
    prepared: Setup, documents: RetainedReviewDocumentRepo
) -> None:
    _change(prepared.database, "DROP TABLE ontology_review_values")
    with pytest.raises(ReviewDocumentUnavailable):
        await prepared.ports.capture_review(ENDPOINT, documents)
    assert len(prepared.transport.calls) == 1


async def test_recovered_sensitive_document_requires_durable_access_audit(
    prepared: Setup, documents: RetainedReviewDocumentRepo
) -> None:
    await prepared.ports.capture_review(ENDPOINT, documents)
    _change(prepared.database, "DROP TABLE mcp_audit_log")
    with pytest.raises(ModelAuditUnavailable):
        await prepared.ports.restore_review(ENDPOINT, documents)
    assert count(prepared) == 1 and len(prepared.transport.calls) == 1


async def test_revocation_during_actual_commit_preserves_record_but_never_serves(
    prepared: Setup, documents: RetainedReviewDocumentRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_save = documents.save

    def save_then_revoke(*args: Any, **kwargs: Any) -> None:
        original_save(*args, **kwargs)
        prepared.grants.allowed.remove("review")

    monkeypatch.setattr(documents, "save", save_then_revoke)
    with pytest.raises(PermissionError):
        await prepared.ports.capture_review(ENDPOINT, documents)
    assert count(prepared) == 1 and len(prepared.transport.calls) == 1


async def test_cancelled_store_wait_retains_original_commit_without_serving_or_replay(
    prepared: Setup, documents: RetainedReviewDocumentRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    original_save = documents.save

    def blocked_save(*args: Any, **kwargs: Any) -> None:
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        try:
            original_save(*args, **kwargs)
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(documents, "save", blocked_save)
    task = asyncio.create_task(prepared.ports.capture_review(ENDPOINT, documents))
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
    finally:
        release.set()
        await finished.wait()
    assert count(prepared) == 1 and len(prepared.transport.calls) == 1
    recovered = await prepared.ports.restore_review(ENDPOINT, documents)
    assert recovered.pointer == ModelPointer("a" * 64, "b" * 64)
