"""Original SQLite facts and fixture grant/native ports; no production claim."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.unit.identity.test_canonical import (
    _HOME_MEMBER,
    _HOME_ORG,
    _service,
)
from tests.unit.identity.test_canonical import canonical_db as canonical_db
from tests.unit.native_host.test_context import Runtime

from octop.infra.agents.personal_moss_access import (
    MossMemberSnapshot,
    require_personal_moss_access,
)
from octop.infra.db.repos.retained_cohost import (
    RetainedBindingInvalid,
    RetainedBindingMissing,
    RetainedCohostBindingRepo,
)
from octop.infra.errors import MossworkError
from octop.infra.identity.canonical import CanonicalIdentitySnapshot
from octop.infra.native_host.authority import RetainedCohostAuthority
from octop.infra.native_host.context import HostSelection, HostUnavailable, resolve_trusted_host


def _sql(path: Path, sql: str, parameters: tuple[Any, ...] = ()) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(sql, parameters)


@pytest.fixture
def bound_store(canonical_db: Path) -> Path:
    with closing(sqlite3.connect(canonical_db)) as connection, connection:
        connection.executescript(
            (Path(__file__).parents[1] / "db/fixtures/retained_cohost_103.sql").read_text(
                encoding="utf-8"
            )
        )
        connection.execute(
            """INSERT INTO sessions (session_id, transcript_session_id, org_id, user_id,
                role, scopes_json, cwd, runtime_type, status, desired_state,
                transcript_path, created_at, last_active_at)
                VALUES ('original-app', 'original-transcript', ?, ?, 'user', '[]',
                '/original/repo', 'cohost', 'ended', 'stopped', '/original/history', 1, 2)""",
            (_HOME_ORG, _HOME_MEMBER),
        )
        connection.execute(
            "INSERT INTO cohost_sessions VALUES ('original-app', ?, 'original-agent',"
            " 'original_native_history', '/original/repo')",
            (_HOME_MEMBER,),
        )
    return canonical_db


class FixturePorts:
    """Real canonical SQL service with isolated verifier/grant/native fixtures."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.actor_service = _service(path)
        self.principal = object()
        self.selection = HostSelection("product-agent", "product-thread")
        self.bindings = RetainedCohostBindingRepo(path)
        self.selected = self.bindings.get_for_resume(
            "original-app", organization_id=_HOME_ORG, owner_id=_HOME_MEMBER
        )
        self.trace: list[str] = []
        self.runtime_calls = 0
        self.runtime: Runtime | None = None
        self.actor_override: Any = None
        self.owner_completion: Any = None
        self.grant_completion: Any = None
        self.allow_other_owner = False
        self.allowed = {("session_connect", "original-app")}
        self.gate: str | None = None
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def wait(self, phase: str) -> None:
        if self.gate == phase:
            self.entered.set()
            await self.release.wait()

    async def current_actor(self, principal: object) -> CanonicalIdentitySnapshot:
        self.trace.append("current_actor")
        if principal is not self.principal:
            raise PermissionError("Not the authenticated fixture carrier")
        if self.actor_override is not None:
            return self.actor_override
        return await self.actor_service.snapshot("opaque-fixture-verified-credential")

    async def current_selection(self, actor, selection):
        self.trace.append("current_selection")
        if actor.organization_id != _HOME_ORG or selection != self.selection:
            raise PermissionError("Not the original product selection")
        return self.selected

    async def owner_access(self, actor, binding):
        self.trace.append("owner_access")
        await self.wait("owner")
        if self.owner_completion is not None:
            return self.owner_completion

        def read_owner() -> None:
            with closing(sqlite3.connect(self.path)) as connection:
                row = connection.execute(
                    """SELECT u.role, u.status, g.enabled FROM users u
                    LEFT JOIN moss_member_access g ON g.org_id=u.org_id AND g.user_id=u.id
                    WHERE u.id=? AND u.org_id=?""",
                    (binding.owner_id, binding.organization_id),
                ).fetchone()
                if row is None:
                    raise PermissionError("Target owner absent")
                require_personal_moss_access(
                    MossMemberSnapshot(binding.organization_id, binding.owner_id, row[0], row[1]),
                    organization_id=actor.organization_id,
                    owner_id=binding.owner_id,
                    explicit_grant=None if row[2] is None else bool(row[2]),
                )

        await asyncio.to_thread(read_owner)

    async def operation_access(self, actor, binding, operation, resource):
        self.trace.append("operation_access")
        await self.wait("operation")
        if self.grant_completion is not None:
            return self.grant_completion
        if binding.owner_id != actor.member.owner_id and not self.allow_other_owner:
            raise PermissionError("No independent target-owner action grant")
        if (operation, resource) not in self.allowed:
            raise PermissionError("Exact action/resource not granted")

    async def resolve_runtime(self, binding):
        self.trace.append("resolve_runtime")
        self.runtime_calls += 1
        await self.wait("runtime")
        self.runtime = Runtime(binding)
        return self.runtime

    def authority(self) -> RetainedCohostAuthority:
        return RetainedCohostAuthority(
            self.bindings,
            current_actor=self.current_actor,
            current_selection=self.current_selection,
            require_owner_access=self.owner_access,
            require_operation=self.operation_access,
            resolve_runtime=self.resolve_runtime,
        )


async def test_real_original_account_member_and_durable_bindings_precede_runtime(
    bound_store: Path,
) -> None:
    before = bound_store.read_bytes()
    ports = FixturePorts(bound_store)
    host = await resolve_trusted_host(ports.principal, ports.selection, ports.authority())
    assert host.binding.account_id != host.binding.native.owner_id == _HOME_MEMBER
    assert host.binding.selection.thread_id == "product-thread"
    assert host.binding.native.durable_session_id == "original_native_history"
    assert host.binding.selection.thread_id != "original-app"
    first_runtime = ports.trace.index("resolve_runtime")
    assert ports.trace[:first_runtime].count("current_actor") >= 3
    assert "owner_access" in ports.trace[:first_runtime]
    assert "operation_access" in ports.trace[:first_runtime]
    assert ports.runtime_calls == 1
    assert before == bound_store.read_bytes()


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("UPDATE human_accounts SET status='disabled'", MossworkError),
        ("UPDATE users SET status='locked'", MossworkError),
        ("UPDATE users SET membership_revision=membership_revision+1", PermissionError),
        ("INSERT INTO revoked_tokens VALUES ('verified-access-jti', 2100000000)", MossworkError),
        ("INSERT INTO moss_member_access VALUES (?, ?, 0, ?, 8)", MossworkError),
        ("DELETE FROM cohost_sessions", RetainedBindingMissing),
        (
            "UPDATE cohost_sessions SET durable_session_id='replacement_history'",
            RetainedBindingInvalid,
        ),
    ],
)
async def test_revoked_or_changed_original_facts_cannot_resolve_runtime(
    bound_store: Path, change: str, expected: type[Exception]
) -> None:
    ports = FixturePorts(bound_store)
    authority = ports.authority()
    # Capture the expected actor through the same canonical service before a
    # wait; the revision test changes it during selection, not before capture.
    if "membership_revision" in change:
        ports.gate = "operation"
        task = asyncio.create_task(authority.resolve(ports.principal, ports.selection))
        await ports.entered.wait()
        _sql(bound_store, change)
        ports.release.set()
        with pytest.raises(expected):
            await task
    else:
        _sql(
            bound_store,
            change,
            (_HOME_ORG, _HOME_MEMBER, _HOME_MEMBER) if "moss_member" in change else (),
        )
        with pytest.raises(expected):
            await authority.resolve(ports.principal, ports.selection)
    assert ports.runtime_calls == 0


async def test_wrong_server_carrier_and_ungranted_connection_never_touch_runtime(
    bound_store: Path,
) -> None:
    ports = FixturePorts(bound_store)
    with pytest.raises(PermissionError):
        await ports.authority().resolve(object(), ports.selection)
    ports.allowed.clear()
    with pytest.raises(PermissionError):
        await ports.authority().resolve(ports.principal, ports.selection)
    assert ports.runtime_calls == 0


@pytest.mark.parametrize("field", ["owner_completion", "grant_completion"])
@pytest.mark.parametrize("verdict", [False, True, {"allowed": True}])
async def test_non_none_assertion_result_cannot_become_connection_permission(
    bound_store: Path, field: str, verdict: Any
) -> None:
    ports = FixturePorts(bound_store)
    setattr(ports, field, verdict)
    with pytest.raises(HostUnavailable, match="Invalid host assertion completion"):
        await ports.authority().resolve(ports.principal, ports.selection)
    assert ports.runtime_calls == 0


@pytest.mark.parametrize(
    ("fact", "expected"),
    [
        ("projection", PermissionError),
        ("original_binding", RetainedBindingInvalid),
        # Here the target is also the actor: its changed entitlement is caught
        # by the earlier canonical snapshot comparison before the target read.
        ("target_grant", PermissionError),
        ("actor", MossworkError),
    ],
)
async def test_changes_during_grant_await_refuse_before_connection(
    bound_store: Path, fact: str, expected: type[Exception]
) -> None:
    ports = FixturePorts(bound_store)
    ports.gate = "operation"
    pending = asyncio.create_task(ports.authority().resolve(ports.principal, ports.selection))
    await ports.entered.wait()
    if fact == "projection":
        ports.selected = replace(ports.selected, application_session_id="another-app")
    elif fact == "original_binding":
        _sql(bound_store, "UPDATE cohost_sessions SET native_agent_id='replacement-agent'")
    elif fact == "target_grant":
        _sql(
            bound_store,
            "INSERT INTO moss_member_access VALUES (?, ?, 0, ?, 8)",
            (_HOME_ORG, _HOME_MEMBER, _HOME_MEMBER),
        )
    else:
        _sql(bound_store, "UPDATE human_accounts SET status='disabled'")
    ports.release.set()
    with pytest.raises(expected):
        await pending
    assert ports.runtime_calls == 0


async def test_changed_projection_during_target_access_refuses_before_connection(
    bound_store: Path,
) -> None:
    ports = FixturePorts(bound_store)
    ports.gate = "owner"
    pending = asyncio.create_task(ports.authority().resolve(ports.principal, ports.selection))
    await ports.entered.wait()
    ports.selected = replace(ports.selected, repository_path="/replacement/repository")
    ports.release.set()
    with pytest.raises(PermissionError):
        await pending
    assert ports.runtime_calls == 0


async def test_revocation_while_runtime_is_resolved_never_publishes_a_valid_binding(
    bound_store: Path,
) -> None:
    ports = FixturePorts(bound_store)
    authority = ports.authority()
    ports.gate = "runtime"
    pending = asyncio.create_task(authority.resolve(ports.principal, ports.selection))
    await ports.entered.wait()
    ports.allowed.clear()
    ports.release.set()
    with pytest.raises(PermissionError):
        await pending
    assert ports.runtime_calls == 1
    assert ports.runtime is not None
    # The borrowing authority cannot stop the manager-owned original writer or
    # turn a local refusal into native cleanup/replacement permission.
    with pytest.raises(HostUnavailable, match="already used"):
        await authority.resolve(ports.principal, ports.selection)
    with pytest.raises(PermissionError):
        await authority.check_current(ports.principal, ports.runtime.original)
    assert ports.runtime_calls == 1


async def test_post_attach_exact_action_grant_and_principal_are_checked(bound_store: Path) -> None:
    ports = FixturePorts(bound_store)
    authority = ports.authority()
    host = await resolve_trusted_host(ports.principal, ports.selection, authority)
    ports.allowed.add(("model.invoke", "registered-model"))
    await host.check_ready("model.invoke", "registered-model")
    with pytest.raises(PermissionError):
        await host.check_ready("model.invoke", "ungranted-model")
    with pytest.raises(PermissionError):
        await authority.authorize(object(), host.binding, "model.invoke", "registered-model")
    ports.allowed.clear()
    with pytest.raises(PermissionError):
        await host.check_ready("model.invoke", "registered-model")
    assert ports.runtime_calls == 1


def _select_other_owner(ports: FixturePorts) -> None:
    bound_store = ports.path
    _sql(
        bound_store,
        "INSERT INTO users (id,org_id,email,name,role,status,created_at)"
        " VALUES ('target-owner',?,'target@example.test','Target','user','active',1)",
        (_HOME_ORG,),
    )
    _sql(
        bound_store,
        "INSERT INTO moss_member_access VALUES (?, 'target-owner', 1, ?, 8)",
        (_HOME_ORG, _HOME_MEMBER),
    )
    _sql(bound_store, "UPDATE sessions SET user_id='target-owner'")
    _sql(bound_store, "UPDATE cohost_sessions SET owner_user_id='target-owner'")
    ports.selected = ports.bindings.get_for_resume(
        "original-app", organization_id=_HOME_ORG, owner_id="target-owner"
    )


async def test_admin_actor_and_target_owner_remain_independently_granted(bound_store: Path) -> None:
    ports = FixturePorts(bound_store)
    _select_other_owner(ports)
    with pytest.raises(PermissionError):
        await ports.authority().resolve(ports.principal, ports.selection)
    assert ports.runtime_calls == 0
    ports.allow_other_owner = True
    host = await resolve_trusted_host(ports.principal, ports.selection, ports.authority())
    assert host.binding.native.owner_id == "target-owner"
    assert host.binding.account_id != _HOME_MEMBER != host.binding.native.owner_id
    assert ports.runtime_calls == 1


async def test_revoking_another_owner_during_grant_wait_refuses_without_changing_actor(
    bound_store: Path,
) -> None:
    ports = FixturePorts(bound_store)
    _select_other_owner(ports)
    ports.allow_other_owner = True
    original_actor = await ports.current_actor(ports.principal)
    ports.gate = "operation"
    pending = asyncio.create_task(ports.authority().resolve(ports.principal, ports.selection))
    await ports.entered.wait()
    _sql(
        bound_store,
        "UPDATE moss_member_access SET enabled=0, updated_at=9 WHERE user_id='target-owner'",
    )
    ports.release.set()
    with pytest.raises(MossworkError):
        await pending
    assert await ports.current_actor(ports.principal) == original_actor
    assert ports.runtime_calls == 0


async def test_foreign_organization_and_malformed_actor_never_reach_runtime(
    bound_store: Path,
) -> None:
    ports = FixturePorts(bound_store)
    ports.selected = replace(ports.selected, organization_id="foreign-org")
    with pytest.raises(PermissionError):
        await ports.authority().resolve(ports.principal, ports.selection)
    ports.actor_override = {"account_id": "looks-canonical"}
    with pytest.raises(PermissionError):
        await ports.authority().resolve(ports.principal, ports.selection)
    assert ports.runtime_calls == 0


async def test_cancellation_during_selection_cannot_resolve_a_replacement(
    bound_store: Path,
) -> None:
    ports = FixturePorts(bound_store)
    ports.gate = "operation"
    authority = ports.authority()
    pending = asyncio.create_task(authority.resolve(ports.principal, ports.selection))
    await ports.entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    with pytest.raises(HostUnavailable, match="already used"):
        await authority.resolve(ports.principal, ports.selection)
    assert ports.runtime_calls == 0
