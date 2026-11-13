"""Separate real product/original SQLite stores; runtime and action grants stay fixtures."""

import sqlite3
from dataclasses import replace

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.canonical_identity import (
    CanonicalIdentityProjectionRepo,
    CanonicalProjectionUserChanged,
)
from octop.infra.db.repos.retained_cohost import (
    RetainedBindingInvalid,
    RetainedBindingMissing,
    RetainedSessionUnavailable,
)
from octop.infra.db.repos.retained_session_projection import (
    RetainedSessionProjectionRepo,
    SessionProjectionUnavailable,
)
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.errors import MossworkError
from octop.infra.execution.selection import ProductCohostSelections
from octop.infra.native_host.context import HostSelection, resolve_trusted_host
from tests.unit.identity.test_canonical_human_request_projection import product as product
from tests.unit.native_host.test_authority import FixturePorts, _sql
from tests.unit.native_host.test_authority import bound_store as bound_store
from tests.unit.native_host.test_authority import canonical_db as canonical_db


@pytest.fixture
async def mapped(bound_store, product):
    ports = FixturePorts(bound_store)
    actor = await ports.current_actor(ports.principal)
    owner = CanonicalIdentityProjectionRepo(product).get_or_create(
        authority_id=actor.authority_id,
        account_id=actor.account_id,
        organization_id=actor.organization_id,
        member_id=actor.member.owner_id,
    )
    AgentRepo(product).create(
        agent_id=ports.selection.agent_id, user_id=owner.local_user_id, name="Original reference"
    )
    ThreadRepo(product).insert(
        thread_id=ports.selection.thread_id,
        agent_id=ports.selection.agent_id,
        user_id=owner.local_user_id,
        channel_type="dashboard",
        session_key="original-reference",
    )
    products = RetainedSessionProjectionRepo(product)
    selections = ProductCohostSelections(products, ports.bindings)
    return ports, actor, owner, products, selections


async def record(mapped):
    ports, actor, _owner, _products, selections = mapped
    return await selections.record_existing(
        ports.selection,
        authority_id=actor.authority_id,
        original_app_session_id="original-app",
        organization_id=actor.organization_id,
        owner_id=actor.member.owner_id,
    )


async def test_exact_existing_reference_survives_reopen_without_original_writes(mapped, product):
    ports, actor, owner, products, selections = mapped
    before = ports.path.read_bytes()
    projection = await record(mapped)
    assert await record(mapped) == projection
    reopened = ProductCohostSelections(RetainedSessionProjectionRepo(product), ports.bindings)
    assert await reopened.current(actor, ports.selection) == ports.selected
    assert (
        products.get(actor.authority_id, *(ports.selection.agent_id, ports.selection.thread_id))
        == projection
    )
    assert projection.thread_id != projection.original.application_session_id
    assert projection.original.durable_session_id != projection.thread_id
    assert type(owner.local_user_id) is int
    assert str(owner.local_user_id) != projection.original.owner_id != actor.account_id
    with product.connect() as conn:
        for table in ("users", "agents", "threads", "retained_session_projections"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    assert ports.path.read_bytes() == before


async def test_actual_owner6_authority_consumes_current_selection_without_callback_stub(
    mapped, monkeypatch
):
    ports, _actor, _owner, _products, selections = mapped
    await record(mapped)
    monkeypatch.setattr(ports, "current_selection", selections.current)
    host = await resolve_trusted_host(ports.principal, ports.selection, ports.authority())
    assert host.binding.native.durable_session_id == ports.selected.durable_session_id
    assert host.binding.selection == ports.selection
    assert ("session_connect", ports.selected.application_session_id) in ports.allowed
    assert ports.runtime_calls == 1
    # Exact caller/action/native policy in this test remains the existing fixture.
    ports.allowed.clear()
    with pytest.raises(PermissionError):
        await host.authorize("session_connect", "original-app")
    assert ports.runtime_calls == 1


async def test_ordinary_selection_does_not_confer_or_require_personal_moss(mapped):
    ports, actor, _owner, _products, selections = mapped
    await record(mapped)
    _sql(
        ports.path,
        "INSERT INTO moss_member_access VALUES (?, ?, 0, ?, 9)",
        (actor.organization_id, actor.member.owner_id, actor.member.owner_id),
    )
    current = await ports.current_actor(ports.principal)
    assert not current.moss_access.is_enabled
    assert await selections.current(current, ports.selection) == ports.selected
    assert ports.runtime_calls == 0
    # The personal-Moss host policy stays strict; this facts reader is no grant.
    with pytest.raises(MossworkError):
        await ports.owner_access(current, ports.selected)


@pytest.mark.parametrize(
    "field", ["application_session_id", "durable_session_id", "repository_path"]
)
async def test_existing_projection_never_retargets_original_history(mapped, field):
    _ports, actor, _owner, products, _selections = mapped
    original = await record(mapped)
    changed = replace(
        original, original=replace(original.original, **{field: "different-original"})
    )
    with pytest.raises(SessionProjectionUnavailable, match="immutable"):
        products.record_existing(changed)
    assert products.get(actor.authority_id, original.agent_id, original.thread_id) == original


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("DELETE FROM cohost_sessions", RetainedBindingMissing),
        ("UPDATE sessions SET runtime_type='host'", RetainedSessionUnavailable),
        ("UPDATE sessions SET runtime_type='k8s'", RetainedSessionUnavailable),
        ("UPDATE sessions SET deleted_at=8", RetainedSessionUnavailable),
        ("UPDATE users SET status='disabled'", RetainedSessionUnavailable),
    ],
)
async def test_unmapped_original_types_refuse_without_allocating_or_deleting(
    mapped, product, sql, expected
):
    ports, _actor, _owner, _products, _selections = mapped
    _sql(ports.path, sql)
    before = ports.path.read_bytes()
    with pytest.raises(expected):
        await record(mapped)
    with product.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM retained_session_projections").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1
    assert ports.path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["durable", "product_owner", "disabled_product"])
async def test_current_selection_refuses_original_or_product_owner_changes(
    mapped, product, mutation
):
    ports, actor, owner, _products, selections = mapped
    await record(mapped)
    if mutation == "durable":
        _sql(ports.path, "UPDATE cohost_sessions SET durable_session_id='replacement_history'")
        expected = SessionProjectionUnavailable
    else:
        with product.transaction() as conn:
            if mutation == "product_owner":
                conn.execute("UPDATE threads SET user_id=1 WHERE thread_id='product-thread'")
                # Fixture's canonical allocation may itself be 1; detach its Agent instead.
                conn.execute("UPDATE agents SET user_id=NULL WHERE agent_id='product-agent'")
            else:
                conn.execute("UPDATE users SET disabled=1 WHERE id=?", (owner.local_user_id,))
        expected = (
            SessionProjectionUnavailable
            if mutation == "product_owner"
            else CanonicalProjectionUserChanged
        )
    with pytest.raises(expected):
        await selections.current(actor, ports.selection)
    assert ports.runtime_calls == 0


@pytest.mark.parametrize(
    "foreign", ["authority", "organization", "member_org", "inactive", "thread"]
)
async def test_foreign_actor_or_selection_refuses(mapped, foreign):
    ports, actor, _owner, _products, selections = mapped
    await record(mapped)
    selected = ports.selection
    if foreign == "authority":
        actor = replace(actor, authority_id="other-retained-store")
    elif foreign == "organization":
        actor = replace(actor, organization_id="other-organization")
    elif foreign == "member_org":
        actor = replace(actor, member=replace(actor.member, organization_id="other-organization"))
    elif foreign == "inactive":
        actor = replace(actor, member=replace(actor.member, status="locked"))
    else:
        selected = HostSelection(selected.agent_id, "unknown-thread")
    with pytest.raises((PermissionError, SessionProjectionUnavailable)):
        await selections.current(actor, selected)
    assert ports.runtime_calls == 0


async def test_change_during_product_commit_refuses_and_keeps_original_reference(
    mapped, monkeypatch
):
    ports, actor, _owner, products, selections = mapped
    persist = products.record_existing

    def change_after_commit(projection):
        persist(projection)
        _sql(ports.path, "UPDATE cohost_sessions SET durable_session_id='replacement_history'")

    monkeypatch.setattr(products, "record_existing", change_after_commit)
    with pytest.raises(RetainedBindingInvalid):
        await record(mapped)
    assert (
        products.get(
            actor.authority_id, ports.selection.agent_id, ports.selection.thread_id
        ).original
        == ports.selected
    )
    with pytest.raises(SessionProjectionUnavailable):
        await selections.current(actor, ports.selection)


async def test_existing_v30_repair_preserves_identity_mapping(mapped, product):
    _ports, actor, owner, products, _selections = mapped
    with product.transaction() as conn:
        conn.execute("DROP TABLE retained_session_projections")
    run_migrations(product)
    run_migrations(product)
    projection = await record(mapped)
    assert products.get(actor.authority_id, projection.agent_id, projection.thread_id) == projection
    assert (
        CanonicalIdentityProjectionRepo(product)
        .get(
            authority_id=actor.authority_id,
            account_id=actor.account_id,
            organization_id=actor.organization_id,
            member_id=actor.member.owner_id,
        )
        .local_user_id
        == owner.local_user_id
    )
    with product.connect() as conn:
        assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 30


async def test_two_product_threads_cannot_claim_same_original_app(mapped, product):
    ports, actor, owner, products, selections = mapped
    first = await record(mapped)
    ThreadRepo(product).insert(
        thread_id="second-thread",
        agent_id=ports.selection.agent_id,
        user_id=owner.local_user_id,
        channel_type="dashboard",
        session_key="second-reference",
    )
    with pytest.raises(sqlite3.IntegrityError):
        await selections.record_existing(
            HostSelection(ports.selection.agent_id, "second-thread"),
            authority_id=actor.authority_id,
            original_app_session_id=first.original.application_session_id,
            organization_id=actor.organization_id,
            owner_id=actor.member.owner_id,
        )
    assert products.get(actor.authority_id, first.agent_id, first.thread_id) == first
    with pytest.raises(SessionProjectionUnavailable):
        products.get(actor.authority_id, first.agent_id, "second-thread")


async def test_projection_does_not_delete_referenced_product_history(mapped, product):
    await record(mapped)
    with pytest.raises(sqlite3.IntegrityError), product.transaction() as conn:
        conn.execute("DELETE FROM threads WHERE thread_id='product-thread'")
    with product.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM retained_session_projections").fetchone()[0] == 1


async def test_current_selection_rereads_product_after_original_io(mapped, product, monkeypatch):
    ports, actor, _owner, _products, selections = mapped
    await record(mapped)
    read = ports.bindings.get_for_resume

    def changed_product(*args, **kwargs):
        original = read(*args, **kwargs)
        with product.transaction() as conn:
            conn.execute("UPDATE retained_session_projections SET repository_path='/changed'")
        return original

    monkeypatch.setattr(ports.bindings, "get_for_resume", changed_product)
    with pytest.raises(SessionProjectionUnavailable, match="selection changed"):
        await selections.current(actor, ports.selection)
    assert ports.runtime_calls == 0


async def test_admin_actor_and_target_owner_remain_independent(mapped, product, monkeypatch):
    ports, actor, _owner, _products, selections = mapped
    target, account = "other-original-member", "other-original-account"
    _sql(
        ports.path,
        "INSERT INTO users (id,org_id,email,name,role,status,membership_revision,created_at) "
        "VALUES (?,?,'target@example.test','target','admin','active',1,1)",
        (target, actor.organization_id),
    )
    _sql(ports.path, "INSERT INTO human_accounts VALUES (?,NULL,'active',1)", (account,))
    _sql(
        ports.path,
        "INSERT INTO human_account_memberships VALUES (?,?,?,1)",
        (account, actor.organization_id, target),
    )
    _sql(ports.path, "UPDATE sessions SET user_id=?", (target,))
    _sql(ports.path, "UPDATE cohost_sessions SET owner_user_id=?", (target,))
    projected = CanonicalIdentityProjectionRepo(product).get_or_create(
        authority_id=actor.authority_id,
        account_id=account,
        organization_id=actor.organization_id,
        member_id=target,
    )
    with product.transaction() as conn:
        conn.execute("UPDATE agents SET user_id=?", (projected.local_user_id,))
        conn.execute("UPDATE threads SET user_id=?", (projected.local_user_id,))
    recorded = await selections.record_existing(
        ports.selection,
        authority_id=actor.authority_id,
        original_app_session_id="original-app",
        organization_id=actor.organization_id,
        owner_id=target,
    )
    assert (await selections.current(actor, ports.selection)).owner_id == target
    assert recorded.original.owner_id != actor.member.owner_id
    monkeypatch.setattr(ports, "current_selection", selections.current)
    with pytest.raises(PermissionError, match="independent target-owner action grant"):
        await ports.authority().resolve(ports.principal, ports.selection)
    assert ports.runtime_calls == 0
