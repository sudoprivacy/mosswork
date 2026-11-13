"""Original canonical SQLite/profile policy with fixture allocation/native ports."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest
from tests.unit.native_host.test_authority import FixturePorts, _select_other_owner, _sql
from tests.unit.native_host.test_authority import bound_store as bound_store
from tests.unit.native_host.test_authority import canonical_db as canonical_db

from octop.infra.browser.worker import BrowserWorkerClient, WorkerBinding
from octop.infra.errors import MossworkError
from octop.infra.native_host.browser import CohostBrowserAdmission, browser_controller
from octop.infra.native_host.context import HostUnavailable, resolve_trusted_host


class BrowserServices:
    def __init__(self, ports: FixturePorts) -> None:
        self.ports = ports
        self.allow = True
        self.grant_completion: Any = None
        self.allocation_completion: Any = None
        self.allocation_current = True
        self.actor_override: Any = None
        self.worker_calls: list[tuple[WorkerBinding, str]] = []
        self.action_calls: list[dict[str, Any]] = []
        self.gate: str | None = None
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.mutate_action = False

    async def current_actor(self):
        if self.actor_override is not None:
            return self.actor_override
        return await self.ports.current_actor(self.ports.principal)

    async def wait(self, phase: str) -> None:
        if self.gate == phase:
            self.gate = None
            self.entered.set()
            await self.release.wait()

    async def action_access(self, host, actor, binding, action):
        self.action_calls.append(dict(action))
        await self.wait("grant")
        assert actor.account_id == host.account_id
        assert actor.member.owner_id == binding.user_id != actor.account_id
        if not self.allow:
            raise PermissionError("fixture exact browser action denied")
        if self.mutate_action:
            action.update(kind="restart", url="https://changed.invalid")
        return self.grant_completion

    async def allocation(self, binding, controller):
        await self.wait("allocation")
        if not self.allocation_current:
            raise HostUnavailable("Original registered browser allocation changed")
        return self.allocation_completion

    async def worker(self, binding, controller):
        self.worker_calls.append((binding, controller))
        await self.wait("worker")
        # Exercise the actual existing client constructor without any HTTP call.
        return BrowserWorkerClient(
            cast(aiohttp.ClientSession, object()),
            "http://127.0.0.1:14301",
            "fixture-host-credential",
            binding,
            controller,
        )


async def admission(ports: FixturePorts, services: BrowserServices) -> CohostBrowserAdmission:
    ports.allowed.update(
        ("browser." + kind, "original-app")
        for kind in ("navigate", "take", "return", "observe", "stop")
    )
    host = await resolve_trusted_host(ports.principal, ports.selection, ports.authority())
    return CohostBrowserAdmission(
        host,
        ports.selected,
        bindings=ports.bindings,
        current_actor=services.current_actor,
        require_action=services.action_access,
        assert_allocation=services.allocation,
        resolve_worker=services.worker,
    )


def test_controller_matches_original_typescript_vectors() -> None:
    fixture = Path(__file__).parent / "fixtures/browser_controller_103.json"
    values = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(values["vectors"]) == 5
    for row in values["vectors"]:
        binding = WorkerBinding(row["orgId"], row["userId"], "application-a")
        assert browser_controller(binding) == row["controller"]
        assert browser_controller(replace(binding, session_id="application-b")) == row["controller"]


async def test_original_application_binding_is_separate_from_native_and_product(
    bound_store: Path,
) -> None:
    ports = FixturePorts(bound_store)
    services = BrowserServices(ports)
    guard = await admission(ports, services)
    result = await guard.prepare({"kind": "navigate", "url": "https://example.test"})
    assert result.binding.session_id == "original-app"
    assert result.binding.session_id != ports.selection.thread_id
    assert result.binding.session_id != ports.selected.durable_session_id
    assert result.binding.user_id == ports.selected.owner_id
    assert services.worker_calls == [(result.binding, result.controller)]
    await result.before_effect()
    assert len(services.worker_calls) == 1
    assert result.client is not None


async def test_administrator_cannot_borrow_another_members_browser(bound_store: Path) -> None:
    ports = FixturePorts(bound_store)
    _select_other_owner(ports)
    ports.allow_other_owner = True
    services = BrowserServices(ports)
    guard = await admission(ports, services)
    with pytest.raises(PermissionError):
        await guard.prepare({"kind": "observe"})
    assert services.worker_calls == [] and services.action_calls == []


@pytest.mark.parametrize("change", ["account", "organization", "member", "inactive", "dictionary"])
async def test_wrong_canonical_actor_never_resolves_worker(bound_store: Path, change: str) -> None:
    ports = FixturePorts(bound_store)
    services = BrowserServices(ports)
    guard = await admission(ports, services)
    actor = await services.current_actor()
    if change == "account":
        actor = replace(actor, account_id="other-human-account")
    elif change == "organization":
        actor = replace(actor, organization_id="another-org")
    elif change == "member":
        actor = replace(actor, member=replace(actor.member, owner_id="another-member"))
    elif change == "inactive":
        actor = replace(actor, member=replace(actor.member, status="locked"))
    else:
        actor = {"account_id": actor.account_id, "member_id": actor.member.owner_id}
    services.actor_override = actor
    with pytest.raises(PermissionError):
        await guard.prepare({"kind": "observe"})
    assert services.worker_calls == []


async def test_exact_action_refusal_precedes_private_connection_resolution(bound_store: Path):
    ports = FixturePorts(bound_store)
    services = BrowserServices(ports)
    guard = await admission(ports, services)
    services.allow = False
    with pytest.raises(PermissionError):
        await guard.prepare({"kind": "navigate", "url": "https://example.test"})
    assert services.worker_calls == []


@pytest.mark.parametrize("phase", ["grant", "allocation"])
@pytest.mark.parametrize("completion", [False, True, {}, "allowed"])
async def test_non_none_admission_completion_is_not_a_grant(
    bound_store: Path, phase: str, completion: Any
) -> None:
    ports = FixturePorts(bound_store)
    services = BrowserServices(ports)
    guard = await admission(ports, services)
    if phase == "grant":
        services.grant_completion = completion
    else:
        services.allocation_completion = completion
    with pytest.raises(HostUnavailable):
        await guard.prepare({"kind": "observe"})
    assert services.worker_calls == []


async def test_action_is_frozen_before_wait_and_callback_cannot_retarget(bound_store: Path) -> None:
    ports = FixturePorts(bound_store)
    services = BrowserServices(ports)
    services.gate, services.mutate_action = "grant", True
    guard = await admission(ports, services)
    request = {"kind": "navigate", "url": "https://original.example"}
    task = asyncio.create_task(guard.prepare(request))
    await services.entered.wait()
    request.update(kind="stop", url="https://replacement.example")
    services.release.set()
    prepared = await task
    assert prepared.action == {"kind": "navigate", "url": "https://original.example"}
    prepared.action.update(kind="stop")
    assert prepared.action["kind"] == "navigate"
    await prepared.before_effect()
    assert all(action["kind"] == "navigate" for action in services.action_calls)


async def test_registry_change_during_resolution_never_publishes_guard(bound_store: Path) -> None:
    ports = FixturePorts(bound_store)
    services = BrowserServices(ports)
    services.gate = "worker"
    guard = await admission(ports, services)
    task = asyncio.create_task(guard.prepare({"kind": "observe"}))
    await services.entered.wait()
    services.allocation_current = False
    services.release.set()
    with pytest.raises(HostUnavailable):
        await task
    assert len(services.worker_calls) == 1


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE human_accounts SET status='disabled'",
        "UPDATE users SET membership_revision=membership_revision+1",
        "UPDATE cohost_sessions SET durable_session_id='replacement'",
    ],
)
async def test_effect_guard_rechecks_original_authority_after_barrier_wait(
    bound_store: Path, sql: str
) -> None:
    ports = FixturePorts(bound_store)
    services = BrowserServices(ports)
    guard = await admission(ports, services)
    prepared = await guard.prepare({"kind": "observe"})
    _sql(bound_store, sql)
    with pytest.raises((PermissionError, HostUnavailable, MossworkError)):
        await prepared.before_effect()
    assert len(services.worker_calls) == 1


@pytest.mark.parametrize("kind", ["take", "return", "stop"])
async def test_control_kind_does_not_imply_native_admission(bound_store: Path, kind: str) -> None:
    ports = FixturePorts(bound_store)
    services = BrowserServices(ports)
    guard = await admission(ports, services)
    ports.allowed.remove(("browser." + kind, "original-app"))
    with pytest.raises(PermissionError):
        await guard.prepare({"kind": kind, "controlEpoch": 1, "targetEpoch": 2})
    assert services.worker_calls == []
