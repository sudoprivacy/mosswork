"""Pinned managed-session wire cases, without production credentials or runtimes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest

from octop.infra.native_host.cohost import (
    CohostRuntimeHost,
    CohostSessionEndpoint,
    PublishedCohostExecution,
)
from octop.infra.native_host.context import (
    CohostSessionBinding,
    HostBinding,
    HostSelection,
    HostUnavailable,
    ResolvedHost,
    resolve_trusted_host,
)


class Harness:
    def __init__(self) -> None:
        self.binding = HostBinding(
            "human-account",
            "selected-org",
            HostSelection("product-agent", "product-thread"),
            CohostSessionBinding(
                "original-member", "native-agent", "durable-session", "owned-repo"
            ),
        )
        self.execution = PublishedCohostExecution(
            "123.1",
            CohostSessionEndpoint(
                "original-channel",
                "native-agent-actor",
                "owner-controller",
                f"/conversations/{'a' * 32}/transcript",
            ),
            "/proc/123.1/workspace/",
        )
        self.snapshot: dict[str, Any] = {
            "session_id": self.execution.process_id,
            "agent_id": self.binding.native.agent_id,
            "owner_id": self.binding.native.owner_id,
            "durable_session_id": self.binding.native.durable_session_id,
            "workspace_path": self.execution.workspace_path,
            "state": "ready",
            "session_endpoint": {
                "protocol": "acp-mailbox/1",
                "channel_id": self.execution.endpoint.channel_id,
                "agent": self.execution.endpoint.agent,
                "controller": self.execution.endpoint.controller,
                "transcript": self.execution.endpoint.transcript,
            },
        }
        self.calls: list[tuple[str, str, str]] = []
        self.is_current = True
        self.proof_calls = 0
        self.raw: str | None = None
        self.rpc_error: Exception | None = None
        self.admission_error: Exception | None = None
        self.rpc_entered: asyncio.Event | None = None
        self.rpc_release: asyncio.Event | None = None
        self.admission_entered: asyncio.Event | None = None
        self.admission_release: asyncio.Event | None = None
        self.host = CohostRuntimeHost(
            self.binding,
            self.execution,
            self,
            assert_current_execution=self.proof,
            assert_admission=self.admission,
        )

    async def call(self, method: str, payload: str, auth_token: str) -> str:
        self.calls.append((method, payload, auth_token))
        if self.rpc_entered is not None and self.rpc_release is not None:
            self.rpc_entered.set()
            await self.rpc_release.wait()
        if self.rpc_error is not None:
            raise self.rpc_error
        return self.raw if self.raw is not None else json.dumps(self.snapshot)

    async def proof(self, execution: PublishedCohostExecution) -> None:
        self.proof_calls += 1
        if execution != self.execution or not self.is_current:
            raise HostUnavailable("Original host/load proof revoked")

    async def admission(self, binding: HostBinding, execution: PublishedCohostExecution) -> None:
        if binding != self.binding or execution != self.execution:
            raise PermissionError("Foreign admission target")
        if self.admission_entered is not None and self.admission_release is not None:
            self.admission_entered.set()
            await self.admission_release.wait()
        if self.admission_error is not None:
            raise self.admission_error


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["ready", "busy", "awaiting_input"])
@pytest.mark.parametrize("wrapped", [True, False])
async def test_original_published_execution_uses_existing_read_rpc(
    state: str, wrapped: bool
) -> None:
    f = Harness()
    f.snapshot["state"] = state
    if wrapped:
        f.raw = json.dumps({"result": f.snapshot})
    await f.host.check_current(f.binding)
    assert f.calls == [("managed_agent.get_session_v1", json.dumps({"session_id": "123.1"}), "")]
    assert f.proof_calls == 2
    # Procfs workspace and the canonical repository locator are different fields.
    assert f.execution.workspace_path != f.binding.native.repository_path


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["owner_id", "agent_id"])
async def test_foreign_native_identity_is_denied(field: str) -> None:
    f = Harness()
    f.snapshot[field] = "foreign"
    with pytest.raises(PermissionError):
        await f.host.check_current(f.binding)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["session_id", "durable_session_id", "workspace_path"])
async def test_replacement_execution_identity_is_unavailable(field: str) -> None:
    f = Harness()
    f.snapshot[field] = "replacement"
    with pytest.raises(HostUnavailable):
        await f.host.check_current(f.binding)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["protocol", "channel_id", "agent", "controller", "transcript"])
async def test_changed_mailbox_cannot_become_current(field: str) -> None:
    f = Harness()
    f.snapshot["session_endpoint"][field] = "another-mailbox"
    with pytest.raises(HostUnavailable):
        await f.host.check_current(f.binding)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["registered", "warming_up", "terminated"])
async def test_spawned_or_retired_execution_is_not_ready(state: str) -> None:
    f = Harness()
    f.snapshot["state"] = state
    with pytest.raises(HostUnavailable):
        await f.host.check_current(f.binding)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "session_id",
        "owner_id",
        "agent_id",
        "durable_session_id",
        "workspace_path",
        "state",
        "session_endpoint",
    ],
)
async def test_missing_current_evidence_is_not_ready(field: str) -> None:
    f = Harness()
    del f.snapshot[field]
    with pytest.raises(HostUnavailable):
        await f.host.check_current(f.binding)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["session_id", "owner_id", "agent_id", "durable_session_id", "workspace_path"]
)
async def test_native_values_are_not_coerced_to_identity_strings(field: str) -> None:
    f = Harness()
    f.snapshot[field] = 1
    with pytest.raises(HostUnavailable):
        await f.host.check_current(f.binding)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "null",
        "[]",
        "{",
        '{"result":null}',
        '{"result":NaN}',
        '{"error":{"message":"private-key"}}',
    ],
)
async def test_invalid_or_error_reply_cannot_report_current(raw: str) -> None:
    f = Harness()
    f.raw = raw
    with pytest.raises(HostUnavailable) as caught:
        await f.host.check_current(f.binding)
    assert "private-key" not in str(caught.value)
    assert len(f.calls) == 1  # Never replay or initialize a replacement.


@pytest.mark.asyncio
async def test_error_and_result_together_are_refused() -> None:
    f = Harness()
    f.raw = json.dumps({"result": f.snapshot, "error": {"message": "private"}})
    with pytest.raises(HostUnavailable):
        await f.host.check_current(f.binding)


@pytest.mark.asyncio
async def test_foreign_product_binding_is_refused_before_native_access() -> None:
    f = Harness()
    with pytest.raises(PermissionError):
        await f.host.check_current(replace(f.binding, organization_id="another-org"))
    assert not f.calls
    assert f.proof_calls == 0


@pytest.mark.asyncio
async def test_repository_retarget_is_refused_before_native_access() -> None:
    f = Harness()
    changed = replace(f.binding.native, repository_path="another-repository")
    with pytest.raises(PermissionError):
        await f.host.check_current(replace(f.binding, native=changed))
    assert not f.calls


@pytest.mark.asyncio
async def test_missing_original_host_proof_never_reaches_rpc() -> None:
    f = Harness()
    f.is_current = False
    with pytest.raises(HostUnavailable):
        await f.host.check_current(f.binding)
    assert not f.calls


@pytest.mark.asyncio
async def test_public_snapshot_does_not_replace_the_original_native_proof() -> None:
    f = Harness()
    f.rpc_entered, f.rpc_release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(f.host.check_current(f.binding))
    await f.rpc_entered.wait()
    f.is_current = False
    f.rpc_release.set()
    with pytest.raises(HostUnavailable):
        await task
    assert len(f.calls) == 1


@pytest.mark.asyncio
async def test_success_is_not_cached_after_execution_changes() -> None:
    f = Harness()
    await f.host.check_current(f.binding)
    f.snapshot["session_endpoint"]["channel_id"] = "replacement-channel"
    with pytest.raises(HostUnavailable):
        await f.host.check_current(f.binding)
    assert len(f.calls) == 2


@pytest.mark.asyncio
async def test_admission_keeps_the_original_binding_and_rechecks_native_state() -> None:
    f = Harness()
    await f.host.check_admission(f.binding)
    assert len(f.calls) == 2
    assert all(
        method == "managed_agent.get_session_v1" and token == "" for method, _, token in f.calls
    )


@pytest.mark.asyncio
async def test_host_replacement_during_policy_check_cannot_report_admitted() -> None:
    f = Harness()
    f.admission_entered, f.admission_release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(f.host.check_admission(f.binding))
    await f.admission_entered.wait()
    f.is_current = False
    f.admission_release.set()
    with pytest.raises(HostUnavailable):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [PermissionError("Not applied"), TimeoutError("private")])
async def test_policy_failure_refuses_without_native_mutation(error: Exception) -> None:
    f = Harness()
    f.admission_error = error
    with pytest.raises((PermissionError, HostUnavailable)) as caught:
        await f.host.check_admission(f.binding)
    assert "private" not in str(caught.value)
    assert len(f.calls) == 1


@pytest.mark.asyncio
async def test_transport_timeout_is_not_retried_or_reported_as_absence() -> None:
    f = Harness()
    f.rpc_error = TimeoutError("private-endpoint")
    with pytest.raises(HostUnavailable) as caught:
        await f.host.check_current(f.binding)
    assert "private-endpoint" not in str(caught.value)
    assert len(f.calls) == 1


@pytest.mark.asyncio
async def test_cancellation_does_not_stop_or_replace_a_native_execution() -> None:
    f = Harness()
    f.rpc_entered, f.rpc_release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(f.host.check_current(f.binding))
    await f.rpc_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(f.calls) == 1
    assert f.execution.process_id == "123.1"


@pytest.mark.asyncio
@pytest.mark.parametrize("is_foreign", [False, True])
async def test_context_consumer_checks_the_actual_cohost_snapshot_adapter(is_foreign: bool) -> None:
    f = Harness()
    principal = object()

    class ApplicationAuthority:
        async def resolve(self, actor: object, selection: HostSelection) -> ResolvedHost:
            if actor is not principal or selection != f.binding.selection:
                raise PermissionError("Foreign selection")
            return ResolvedHost(f.binding, f.host)

        async def check_current(self, actor: object, bound: HostBinding) -> None:
            if actor is not principal or bound != f.binding:
                raise PermissionError("Foreign binding")

        async def authorize(
            self, actor: object, bound: HostBinding, operation: str, resource_ref: str
        ) -> None:
            await self.check_current(actor, bound)
            if (operation, resource_ref) != ("sudograph_context", "registered-model"):
                raise PermissionError("Fixture grant denied")

    context = await resolve_trusted_host(principal, f.binding.selection, ApplicationAuthority())
    if is_foreign:
        f.snapshot["owner_id"] = "foreign-native-owner"

    async def read_domain() -> str:
        assert not is_foreign, "Cross-owner native state must prevent the domain read"
        return "Fresh registered domain context"

    resolver = context.host_context_resolver("registered-model", read_domain)
    request = {
        "sessionId": f.binding.selection.thread_id,
        "nativeSessionId": f.binding.native.durable_session_id,
    }
    if is_foreign:
        with pytest.raises(PermissionError):
            await resolver(request)  # type: ignore[arg-type]
    else:
        assert await resolver(request) == "Fresh registered domain context"  # type: ignore[arg-type]
        await context.check_ready("sudograph_context", "registered-model")
    assert all(
        method == "managed_agent.get_session_v1" and token == "" for method, _, token in f.calls
    )
