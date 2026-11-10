"""Actual retained SQLite/model-port composition with fixture grants/transport."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from tests.unit.db.test_retained_cohost import _change
from tests.unit.db.test_retained_cohost import retained as retained
from tests.unit.db.test_retained_ontology import registered as registered
from tests.unit.native_host.test_context import Authority, Runtime

from octop.infra.db.repos.retained_cohost import RetainedCohostBindingRepo
from octop.infra.db.repos.retained_model_audit import RetainedModelAuditRepo
from octop.infra.db.repos.retained_ontology import (
    RetainedOntologyBinding,
    RetainedOntologyBindingRepo,
)
from octop.infra.native_host.context import (
    CohostSessionBinding,
    HostBinding,
    HostSelection,
    HostUnavailable,
    ResolvedHost,
    resolve_trusted_host,
)
from octop.infra.native_host.sudograph import RegisteredSudographPorts
from octop.infra.sudograph.client import Operation, SudographClient
from octop.infra.sudograph.protocol import ModelBinding, ModelPointer, SudographError, ToolResult

OPERATIONS: tuple[Operation, ...] = (
    "sudograph_context",
    "sudograph_context_read",
    "sudograph_model_contract",
    "sudograph_model_apply",
    "review",
)


class Grants:
    def __init__(self) -> None:
        self.allowed = set(OPERATIONS)
        self.checks: list[tuple[HostBinding, str, RetainedOntologyBinding, ModelBinding, str]] = []
        self.completion: Any = None
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def __call__(
        self,
        host: HostBinding,
        actor_id: str,
        original: RetainedOntologyBinding,
        model: ModelBinding,
        operation: Operation,
    ) -> None:
        self.checks.append((host, actor_id, original, model, operation))
        if self.entered is not None and self.release is not None:
            entered, release = self.entered, self.release
            self.entered = self.release = None
            entered.set()
            await release.wait()
        if operation not in self.allowed:
            raise PermissionError("private grant diagnostic")
        return self.completion


class Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[RetainedOntologyBinding, str, dict[str, Any]]] = []
        self.result = ToolResult(json.dumps({"revision": "a" * 64}))
        self.response: dict[str, Any] = {}
        self.before_return: Any = None

    async def call_tool(
        self, original: RetainedOntologyBinding, name: Operation, request: dict[str, Any]
    ) -> ToolResult:
        self.calls.append((original, name, copy.deepcopy(request)))
        if self.before_return:
            await self.before_return()
        return self.result

    async def read_review(
        self, original: RetainedOntologyBinding, request: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((original, "review", copy.deepcopy(request)))
        if self.before_return:
            await self.before_return()
        return self.response


@dataclass
class Setup:
    database: Path
    authority: Authority
    grants: Grants
    transport: Transport
    ports: RegisteredSudographPorts
    original: RetainedOntologyBinding

    @property
    def client(self) -> SudographClient:
        return SudographClient(self.ports.binding, self.ports)


@pytest.fixture
async def prepared(registered: Path) -> Setup:
    with closing(sqlite3.connect(registered)) as connection:
        connection.execute("CREATE TABLE mcp_servers (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO mcp_servers VALUES ('worker')")
        connection.executescript(
            (Path(__file__).parent.parent / "db/fixtures/retained_mcp_audit_103.sql").read_text(
                encoding="utf-8"
            )
        )
        connection.commit()
    bindings, registry = (
        RetainedCohostBindingRepo(registered),
        RetainedOntologyBindingRepo(registered),
    )
    original = bindings.get_for_resume("app-original", organization_id="org", owner_id="owner")
    model = registry.resolve_original("app-original", organization_id="org", owner_id="owner")
    authority = Authority()
    authority.current = HostBinding(
        "original-human-account",
        "org",
        HostSelection("product-agent", "product-thread"),
        CohostSessionBinding(
            original.owner_id,
            original.native_agent_id,
            original.durable_session_id,
            original.repository_path,
        ),
    )
    authority.runtime = Runtime(authority.current)
    authority.result = ResolvedHost(authority.current, authority.runtime)
    authority.grants = {(operation, model.resource_ref) for operation in OPERATIONS}
    host = await resolve_trusted_host(authority.principal, authority.current.selection, authority)
    grants, transport = Grants(), Transport()
    ports = RegisteredSudographPorts(
        host,
        original,
        model,
        bindings=bindings,
        registry=registry,
        audit=RetainedModelAuditRepo(registered),
        actor_id="authenticated-actor-member",
        require_model_access=grants,
        transport=transport,
    )
    return Setup(registered, authority, grants, transport, ports, model)


def rows(prepared: Setup) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(prepared.database)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM mcp_audit_log ORDER BY rowid").fetchall()


def compiled_context(prepared: Setup) -> None:
    binding = prepared.ports.binding
    prepared.transport.result = ToolResult(
        json.dumps(
            {
                "format": "sudograph-worker-context/v1",
                "authority_ref": binding.authority_ref,
                "model_ref": binding.model_ref,
                "source_refs": list(binding.source_refs),
                "scope": "full-registered-model",
                "context": {
                    "format": "sudograph-agent-context/v1",
                    "revision": "a" * 64,
                    "context_digest": "b" * 64,
                    "text": "Exact compiled Unicode context",
                },
            }
        )
    )


async def test_actual_sql_audit_uses_actor_and_full_original_model(prepared: Setup) -> None:
    await prepared.client.contract()
    host, actor, original, model, operation = prepared.grants.checks[-1]
    assert host == prepared.authority.current
    assert actor != host.account_id and actor != host.native.owner_id
    assert original == prepared.original and model == prepared.ports.binding
    assert operation == "sudograph_model_contract"
    assert prepared.transport.calls == [(prepared.original, operation, {})]
    (row,) = rows(prepared)
    assert row["user_id"] == actor and row["session_id"] == "app-original"
    assert row["mcp_server_id"] == "worker" and row["status"] == "success"
    assert row["request_params_json"] is None and row["response_summary"] is None


@pytest.mark.parametrize("field", ["authority_ref", "model_ref", "resource_ref", "source_refs"])
async def test_resource_permission_never_grants_a_foreign_full_tuple(
    prepared: Setup, field: str
) -> None:
    value = ("other-source",) if field == "source_refs" else "other"
    foreign = replace(prepared.ports.binding, **{field: value})
    with pytest.raises(PermissionError):
        await prepared.ports.call_tool(foreign, "sudograph_model_contract", {})
    assert prepared.grants.checks == [] and prepared.transport.calls == []


async def test_resource_access_without_full_model_grant_denies_and_audits(prepared: Setup) -> None:
    prepared.grants.allowed.clear()
    with pytest.raises(SudographError) as failure:
        await prepared.client.contract()
    assert failure.value.code == "ACCESS_DENIED"
    assert prepared.transport.calls == []
    assert rows(prepared)[0]["status"] == "error"


async def test_review_has_an_independent_permission(prepared: Setup) -> None:
    prepared.grants.allowed.remove("review")
    await prepared.client.contract()
    with pytest.raises(SudographError) as failure:
        await prepared.client.review({"operation": "head"})
    assert failure.value.code == "ACCESS_DENIED"
    assert len(prepared.transport.calls) == 1
    assert [row["status"] for row in rows(prepared)] == ["success", "error"]


@pytest.mark.parametrize("completion", [False, True, {}, "allowed", 0])
async def test_non_none_model_assertion_never_dispatches(prepared: Setup, completion: Any) -> None:
    prepared.grants.completion = completion
    with pytest.raises(HostUnavailable):
        await prepared.ports.call_tool(prepared.ports.binding, "sudograph_model_contract", {})
    assert prepared.transport.calls == []


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ontology_projects SET generation = generation + 1",
        "UPDATE ontology_projects SET source_refs_json = '[\"other-source\"]'",
        "UPDATE cohost_sessions SET durable_session_id = 'replacement'",
    ],
)
async def test_original_binding_change_while_grant_awaits_refuses_io(
    prepared: Setup, statement: str
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    prepared.grants.entered, prepared.grants.release = entered, release
    task = asyncio.create_task(prepared.client.contract())
    await entered.wait()
    _change(prepared.database, statement)
    release.set()
    with pytest.raises(SudographError):
        await task
    assert prepared.transport.calls == []
    assert rows(prepared)[0]["status"] == "error"


async def test_native_or_dlp_admission_refusal_makes_zero_transport_calls(prepared: Setup) -> None:
    prepared.authority.runtime.admission_error = PermissionError("native admission denied")
    with pytest.raises(SudographError) as failure:
        await prepared.client.contract()
    assert failure.value.code == "ACCESS_DENIED"
    assert prepared.transport.calls == []


async def test_reply_after_registry_change_is_refused_without_retry(prepared: Setup) -> None:
    async def change() -> None:
        _change(prepared.database, "UPDATE ontology_projects SET generation = 8")

    prepared.transport.before_return = change
    with pytest.raises(SudographError):
        await prepared.client.contract()
    assert len(prepared.transport.calls) == 1
    assert rows(prepared)[0]["status"] == "error"


async def test_write_arguments_are_snapshotted_before_grant_wait(prepared: Setup) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    prepared.grants.entered, prepared.grants.release = entered, release
    request = {"base_revision": "a" * 64, "operation_id": "original-operation", "model": "old"}
    task = asyncio.create_task(
        prepared.ports.call_tool(prepared.ports.binding, "sudograph_model_apply", request)
    )
    await entered.wait()
    request.update(operation_id="replacement", model="changed")
    release.set()
    await task
    assert prepared.transport.calls[0][2]["operation_id"] == "original-operation"
    assert prepared.transport.calls[0][2]["model"] == "old"


async def test_mcp_error_status_is_preserved(prepared: Setup) -> None:
    prepared.transport.result = ToolResult(
        [
            {
                "type": "text",
                "text": '{"code":"STALE_REVISION","reason":"changed","recovery":"read"}',
            }
        ],
        is_error=True,
    )
    with pytest.raises(SudographError) as failure:
        await prepared.client.apply(
            {"base_revision": "a" * 64, "operation_id": "original-operation", "model": "model"}
        )
    assert failure.value.code == "STALE_REVISION"
    assert len(prepared.transport.calls) == 1
    assert rows(prepared)[0]["status"] == "error"


async def test_audit_failure_after_possible_write_never_replays(prepared: Setup) -> None:
    _change(prepared.database, "DROP TABLE mcp_audit_log")
    with pytest.raises(SudographError) as failure:
        await prepared.client.apply(
            {"base_revision": "a" * 64, "operation_id": "original-operation", "model": "model"}
        )
    assert failure.value.code == "AUDIT_UNAVAILABLE"
    assert len(prepared.transport.calls) == 1
    assert prepared.transport.calls[0][2]["operation_id"] == "original-operation"


@pytest.mark.parametrize("audit_available", [True, False])
async def test_cancelled_task_keeps_cancellation_through_actual_sql_audit(
    prepared: Setup, audit_available: bool
) -> None:
    entered = asyncio.Event()

    async def pending_reply() -> None:
        entered.set()
        await asyncio.Event().wait()

    prepared.transport.before_return = pending_reply
    task = asyncio.create_task(
        prepared.client.apply(
            {"base_revision": "a" * 64, "operation_id": "original-operation", "model": "model"}
        )
    )
    await entered.wait()
    if not audit_available:
        _change(prepared.database, "DROP TABLE mcp_audit_log")
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert task.cancelled()
    assert len(prepared.transport.calls) == 1
    assert prepared.transport.calls[0][2]["operation_id"] == "original-operation"
    if audit_available:
        assert rows(prepared)[0]["status"] == "error"
    else:
        assert cancelled.value.__notes__ == [
            "AUDIT_UNAVAILABLE: canonical model audit did not complete"
        ]


async def test_review_transport_keeps_original_resource_and_envelope(prepared: Setup) -> None:
    model = prepared.ports.binding
    prepared.transport.response = {
        "format": "sudograph-worker-review/v2",
        "authority_ref": model.authority_ref,
        "model_ref": model.model_ref,
        "source_refs": list(model.source_refs),
        "scope": "full-registered-model",
        "resource_ref": model.resource_ref,
        "result": {"revision": "a" * 64},
    }
    result = await prepared.client.review({"operation": "head"})
    assert result["result"] == {"revision": "a" * 64}
    assert prepared.transport.calls == [
        (prepared.original, "review", {"operation": "head", "resource": model.resource_ref})
    ]
    assert rows(prepared)[0]["action"] == "ontology.review.read"


async def test_context_returns_same_text_and_retains_semantic_pointer(prepared: Setup) -> None:
    compiled_context(prepared)
    recorded: list[tuple[HostBinding, RetainedOntologyBinding, ModelPointer]] = []

    async def record(host: HostBinding, original: RetainedOntologyBinding, pointer: ModelPointer):
        recorded.append((host, original, pointer))

    resolve = prepared.ports.host_context_resolver(record)
    text = await resolve(
        {
            "sessionId": "product-thread",
            "nativeSessionId": prepared.authority.current.native.durable_session_id,
        }
    )
    assert text == "Exact compiled Unicode context"
    assert recorded == [
        (prepared.authority.current, prepared.original, ModelPointer("a" * 64, "b" * 64))
    ]
    assert hashlib.sha256(text.encode()).hexdigest() != recorded[0][2].context_digest
    assert len(prepared.transport.calls) == 1 and len(rows(prepared)) == 1


@pytest.mark.parametrize(
    ("failure", "error"),
    [("error", HostUnavailable), ("verdict", HostUnavailable), ("revoke", PermissionError)],
)
async def test_context_record_failure_or_revoke_never_returns_cached_text(
    prepared: Setup, failure: str, error: type[Exception]
) -> None:
    compiled_context(prepared)

    async def record(host: HostBinding, original: RetainedOntologyBinding, pointer: ModelPointer):
        if failure == "error":
            raise RuntimeError("private context store diagnostic")
        if failure == "verdict":
            return False
        prepared.grants.allowed.clear()
        return None

    resolve = prepared.ports.host_context_resolver(record)
    with pytest.raises(error):
        await resolve(
            {
                "sessionId": "product-thread",
                "nativeSessionId": prepared.authority.current.native.durable_session_id,
            }
        )
    assert len(prepared.transport.calls) == 1


async def test_foreign_context_session_cannot_read_or_record(prepared: Setup) -> None:
    async def record(host: HostBinding, original: RetainedOntologyBinding, pointer: ModelPointer):
        raise AssertionError("Must not record a foreign session")

    resolve = prepared.ports.host_context_resolver(record)
    with pytest.raises(PermissionError):
        await resolve({"sessionId": "product-thread", "nativeSessionId": "other-durable"})
    assert prepared.transport.calls == [] and prepared.grants.checks == []


async def test_original_reference_is_not_normalized_into_another_model(prepared: Setup) -> None:
    bindings, registry = (
        RetainedCohostBindingRepo(prepared.database),
        RetainedOntologyBindingRepo(prepared.database),
    )
    original = bindings.get_for_resume("app-original", organization_id="org", owner_id="owner")
    with pytest.raises(ValueError):
        RegisteredSudographPorts(
            await resolve_trusted_host(
                prepared.authority.principal,
                prepared.authority.current.selection,
                prepared.authority,
            ),
            original,
            replace(prepared.original, model_ref="model.v1"),
            bindings=bindings,
            registry=registry,
            audit=RetainedModelAuditRepo(prepared.database),
            actor_id="authenticated-actor-member",
            require_model_access=prepared.grants,
            transport=prepared.transport,
        )
    assert prepared.transport.calls == []
