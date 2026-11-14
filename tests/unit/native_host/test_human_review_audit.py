"""Original sealed human/SQLite audit; writer admission is an explicit fixture."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.unit.connectors.test_fixed_json import server
from tests.unit.native_host.test_human_review import _FIXTURE, _IDS, response, review_row
from tests.unit.native_host.test_human_review import compose as compose_reader
from tests.unit.native_host.test_human_review import human as human

from octop.infra.db.repos.retained_model_audit import ModelAuditUnavailable, RetainedModelAuditRepo
from octop.infra.errors import MossworkError
from octop.infra.identity.request_principal import CanonicalAuthenticatedRequest, authenticate
from octop.infra.native_host.human_review import HumanReviewAudit
from octop.infra.native_host.human_review_audit import CanonicalHumanReviewAudit
from octop.infra.sudograph.protocol import ModelBinding, SudographError


class Writer:
    def __init__(self, *, fail_at=None, verdict=None, verdict_at=1):
        self.calls = 0
        self.fail_at, self.verdict, self.verdict_at = fail_at, verdict, verdict_at

    async def assert_current_writer(self):
        self.calls += 1
        if self.calls == self.fail_at:
            raise PermissionError("fixture writer withdrawn")
        return self.verdict if self.calls == self.verdict_at else None


def compose(human, writer=None):
    repo = RetainedModelAuditRepo(human.path)
    writer = writer if writer is not None else Writer()
    audit = CanonicalHumanReviewAudit(
        principal=human.principal, project=human.project, audits=repo, writer=writer
    )
    model = ModelBinding("authority", "model", "project", ("source",))
    return SimpleNamespace(audit=audit, model=model, writer=writer, repo=repo)


def rows(human):
    with sqlite3.connect(human.path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM mcp_audit_log")]


@pytest.mark.parametrize("status", ["success", "error"])
async def test_original_actor_metadata_commits_with_null_session_and_no_sensitive_payload(
    human, status
):
    state = compose(human)
    protocol: HumanReviewAudit = state.audit
    await protocol(human.principal, human.project, state.model, status)
    assert state.writer.calls == 2
    (row,) = rows(human)
    assert row["org_id"] == _IDS["homeOrg"]
    assert row["user_id"] == _IDS["homeMember"]
    assert row["user_id"] != _IDS["account"]
    assert row["mcp_server_id"] == "worker" and row["session_id"] is None
    assert row["action"] == "ontology.review.read"
    assert row["tool_name"] == "sudograph_context_read" and row["status"] == status
    for name in (
        "mcp_server_name",
        "user_name",
        "request_params_json",
        "response_summary",
        "error_message",
        "ip_address",
    ):
        assert row[name] is None
    with sqlite3.connect(human.path) as db:
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('agents','sessions','ontology_session_bindings')"
        ).fetchall()


async def test_foreign_organization_refuses_before_retaining_audit_scope(human):
    with pytest.raises(PermissionError):
        CanonicalHumanReviewAudit(
            principal=human.principal,
            project=replace(human.project, organization_id="foreign-org"),
            audits=RetainedModelAuditRepo(human.path),
            writer=Writer(),
        )
    assert rows(human) == []


@pytest.mark.parametrize("principal", [None, 1, {}, SimpleNamespace(identity="metadata")])
async def test_nonsealed_principal_is_not_an_audit_actor(human, principal):
    with pytest.raises(ValueError):
        CanonicalHumanReviewAudit(
            principal=principal,
            project=human.project,
            audits=RetainedModelAuditRepo(human.path),
            writer=Writer(),
        )
    assert rows(human) == []


async def test_factory_seal_is_checked_even_for_an_exact_class_object(human):
    unsealed = object.__new__(CanonicalAuthenticatedRequest)
    with pytest.raises(MossworkError):
        CanonicalHumanReviewAudit(
            principal=unsealed,
            project=human.project,
            audits=RetainedModelAuditRepo(human.path),
            writer=Writer(),
        )
    assert rows(human) == []


async def test_another_real_principal_with_equal_identity_is_refused(human):
    token = next(item["token"] for item in _FIXTURE["cases"] if item["name"] == "sms_home")
    original_resolver = human.principal._CanonicalAuthenticatedRequest__resolver
    other = await authenticate(original_resolver, token)
    assert other.identity == human.principal.identity and other is not human.principal
    state = compose(human)
    with pytest.raises(PermissionError):
        await state.audit(other, human.project, state.model, "error")
    assert rows(human) == [] and state.writer.calls == 0


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": "other"},
        {"organization_id": "other"},
        {"authority_ref": "other"},
        {"worker_ref": "other"},
        {"model_ref": "other"},
        {"source_refs": ("other",)},
        {"generation": 2},
    ],
)
async def test_changed_original_project_refuses_before_writer_or_sql(human, change):
    state = compose(human)
    with pytest.raises(PermissionError):
        await state.audit(human.principal, replace(human.project, **change), state.model, "error")
    assert rows(human) == [] and state.writer.calls == 0


@pytest.mark.parametrize(
    "model",
    [
        ModelBinding("other", "model", "project", ("source",)),
        ModelBinding("authority", "other", "project", ("source",)),
        ModelBinding("authority", "model", "other", ("source",)),
        ModelBinding("authority", "model", "project", ("other",)),
        {"resource_ref": "project"},
    ],
)
async def test_full_registered_model_tuple_is_required(human, model):
    state = compose(human)
    with pytest.raises(PermissionError):
        await state.audit(human.principal, human.project, model, "success")
    assert rows(human) == [] and state.writer.calls == 0


@pytest.mark.parametrize("status", [None, True, 1, "denied", {}, []])
async def test_malformed_status_never_reaches_sql(human, status):
    state = compose(human)
    with pytest.raises(PermissionError):
        await state.audit(human.principal, human.project, state.model, status)
    assert rows(human) == [] and state.writer.calls == 0


async def test_revoked_human_error_audit_uses_held_actor_without_another_read_grant(human):
    state = compose(human)
    with sqlite3.connect(human.path) as db:
        db.execute("UPDATE users SET status='disabled' WHERE id=?", (_IDS["homeMember"],))
    with pytest.raises(MossworkError):
        await human.principal.revalidate()
    await state.audit(human.principal, human.project, state.model, "error")
    (row,) = rows(human)
    assert row["user_id"] == _IDS["homeMember"] and row["status"] == "error"
    assert state.writer.calls == 2


@pytest.mark.parametrize("verdict", [False, True, {"ready": True}])
@pytest.mark.parametrize("stage", [1, 2])
async def test_assertion_port_malformed_success_refuses_even_after_possible_commit(
    human, verdict, stage
):
    state = compose(human, Writer(verdict=verdict, verdict_at=stage))
    with pytest.raises(MossworkError):
        await state.audit(human.principal, human.project, state.model, "error")
    assert len(rows(human)) == stage - 1
    assert state.writer.calls == stage


@pytest.mark.parametrize("stage", [1, 2])
async def test_writer_withdrawal_never_retries_or_reports_success(human, stage):
    state = compose(human, Writer(fail_at=stage))
    with pytest.raises(MossworkError):
        await state.audit(human.principal, human.project, state.model, "success")
    assert len(rows(human)) == stage - 1
    assert state.writer.calls == stage


async def test_sql_failure_propagates_without_a_fallback_audit_or_schema_creation(human):
    state = compose(human)
    with sqlite3.connect(human.path) as db:
        db.execute("DROP TABLE mcp_audit_log")
    with pytest.raises(ModelAuditUnavailable):
        await state.audit(human.principal, human.project, state.model, "error")
    assert state.writer.calls == 1
    with sqlite3.connect(human.path) as db:
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='mcp_audit_log'"
        ).fetchall()


async def test_cancellation_while_awaiting_writer_never_starts_sql(human):
    waiting = asyncio.Event()

    class WaitingWriter(Writer):
        async def assert_current_writer(self):
            self.calls += 1
            waiting.set()
            await asyncio.Event().wait()

    state = compose(human, WaitingWriter())
    task = asyncio.create_task(state.audit(human.principal, human.project, state.model, "error"))
    await asyncio.wait_for(waiting.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled() and rows(human) == [] and state.writer.calls == 1


async def test_sql_runs_off_event_loop_and_cancellation_after_commit_is_preserved(
    human, monkeypatch
):
    state = compose(human)
    main_thread = threading.get_ident()
    committed, release, finished = threading.Event(), threading.Event(), threading.Event()
    record = state.repo.record_project_access
    calls = []

    def write(*args, **kwargs):
        assert threading.get_ident() != main_thread
        calls.append(kwargs)
        record(*args, **kwargs)
        committed.set()
        try:
            assert release.wait(5)
        finally:
            finished.set()

    monkeypatch.setattr(state.repo, "record_project_access", write)
    task = asyncio.create_task(state.audit(human.principal, human.project, state.model, "error"))
    try:
        assert await asyncio.to_thread(committed.wait, 5)
        assert len(rows(human)) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    assert len(rows(human)) == 1 and len(calls) == 1
    assert calls[0]["actor_id"] == _IDS["homeMember"]
    assert state.writer.calls == 1


@pytest.mark.parametrize("withdraw_member_after_io", [False, True])
async def test_joint_reader_actual_transport_and_original_audit_after_member_withdrawal(
    human, withdraw_member_after_io
):
    state = compose(human)

    def reply(_):
        if withdraw_member_after_io:
            with sqlite3.connect(human.path) as db:
                db.execute("UPDATE users SET status='disabled' WHERE id=?", (_IDS["homeMember"],))
        return (
            200,
            {"Content-Type": "application/json"},
            json.dumps(response({"head": "value"})).encode(),
        )

    async with server(reply) as (port, requests):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        _, reader = compose_reader(human, audit=state.audit)
        if withdraw_member_after_io:
            with pytest.raises(SudographError):
                await reader.review({"operation": "head"})
        else:
            assert await reader.review({"operation": "head"}) == response({"head": "value"})
        assert len(requests) == 1
    (row,) = rows(human)
    assert row["user_id"] == _IDS["homeMember"] and row["session_id"] is None
    assert row["status"] == ("error" if withdraw_member_after_io else "success")
    assert state.writer.calls == 2


async def test_joint_reader_does_not_release_or_replay_after_audit_writer_loss(human):
    state = compose(human, Writer(fail_at=2))
    async with server(
        lambda _: (
            200,
            {"Content-Type": "application/json"},
            json.dumps(response({"head": "value"})).encode(),
        )
    ) as (port, requests):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        _, reader = compose_reader(human, audit=state.audit)
        with pytest.raises(SudographError) as caught:
            await reader.review({"operation": "head"})
        assert caught.value.code == "AUDIT_UNAVAILABLE"
        assert len(requests) == 1
    assert len(rows(human)) == 1 and state.writer.calls == 2
