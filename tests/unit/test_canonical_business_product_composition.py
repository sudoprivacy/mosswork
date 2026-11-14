"""Real human issuer/API/queue/selection; SMS, exact grants and native are fixtures."""

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from octop.api.app import build_app
from octop.api.common.canonical_identity import canonical_authenticated_request
from octop.config import MossworkConfig
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.execution import ExecutionRepo
from octop.infra.db.repos.retained_cohost import RetainedCohostBindingRepo
from octop.infra.db.repos.retained_model_audit import RetainedModelAuditRepo
from octop.infra.db.repos.retained_ontology import RetainedOntologyBindingRepo
from octop.infra.db.repos.retained_session_projection import (
    RetainedSessionProjectionRepo,
    SessionProjectionUnavailable,
)
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.errors import MossworkError
from octop.infra.execution.admission import CanonicalRunAdmissions
from octop.infra.execution.selection import ProductCohostSelections
from octop.infra.identity.composition import build_canonical_business
from octop.infra.native_host.authority import RetainedCohostAuthority
from octop.infra.native_host.context import HostSelection, HostUnavailable, resolve_trusted_host
from octop.infra.native_host.sudograph import RegisteredSudographPorts
from octop.infra.server import MossworkServer
from octop.infra.sudograph.client import SudographClient
from octop.infra.sudograph.protocol import ModelBinding, SudographError
from tests.unit.identity.test_canonical_business_composition import environment as environment
from tests.unit.identity.test_canonical_business_composition import legacy_db as legacy_db
from tests.unit.identity.test_canonical_business_composition import product as product
from tests.unit.identity.test_human_sessions import _IDS, _NUMBER
from tests.unit.native_host.test_context import Runtime
from tests.unit.native_host.test_sudograph import Transport


@pytest.fixture
def application(environment, tmp_path):
    server = MossworkServer(tmp_path / "product-home")
    cfg = MossworkConfig(enable_api_docs=True, enable_dashboard=False)
    server.services = build_shared_services(db=environment.product, paths=server.paths, config=cfg)
    server.config = cfg
    server._started = True
    return build_app(server, **environment.bundle.app_kwargs())


async def http_login(app, environment):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sent = await client.post("/api/identity/phone/send-code", json={"phone": _NUMBER})
        assert sent.status_code == 200 and sent.headers["cache-control"] == "no-store"
        logged = await client.post(
            "/api/identity/phone/login",
            json={"phone": _NUMBER, "code": environment.sender.sent[-1][1]},
        )
        assert logged.status_code == 200 and logged.headers["cache-control"] == "no-store"
        return logged.json()


def authenticated_request(app, token):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/explicit-test-ingress",
            "headers": [(b"authorization", ("Bearer " + token).encode())],
            "app": app,
        }
    )


async def test_actual_bundle_login_profile_and_docs_use_same_factory_without_local_admin(
    application, environment
):
    assert environment.bundle.requests._snapshots is application.state.canonical_identity
    assert UserRepo(environment.product).count() == 0
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        status = await client.get("/api/identity/status")
        assert status.status_code == 200
        assert status.json() == {
            "mode": "canonical",
            "phone_login_available": True,
            "password_login_available": True,
        }
        assert (await client.get("/api/docs")).status_code == 200
        schema = (await client.get("/api/openapi.json")).json()
        operation = schema["paths"]["/api/identity/phone/login"]["post"]
        assert operation["summary"] and operation["requestBody"] and operation["responses"]
        assert "/api/identity/api-key/exchange" in schema["paths"]
    pair = await http_login(application, environment)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        me = await client.get(
            "/api/identity/me", headers={"Authorization": "Bearer " + pair["access_token"]}
        )
        assert me.status_code == 200 and me.json()["account_id"] == _IDS["account"]
        assert me.json()["member"]["id"] == _IDS["homeMember"]
        assert "x-mosswork-access-token" not in me.headers
        assert me.headers["cache-control"] == "no-store"
        # The canonical snapshot does not enter existing local initial-admin APIs.
        adjacent = await client.get(
            "/api/agents", headers={"Authorization": "Bearer " + pair["access_token"]}
        )
        assert adjacent.status_code == 503
    assert UserRepo(environment.product).count() == 0
    assert environment.native.calls == []


async def queued_original(application, environment):
    pair = await http_login(application, environment)
    principal = await canonical_authenticated_request(
        authenticated_request(application, pair["access_token"])
    )
    user = principal.identity.projection.local_user_id
    selection = HostSelection("existing-product-agent", "existing-product-thread")
    AgentRepo(environment.product).create(
        agent_id=selection.agent_id, user_id=user, name="Original"
    )
    ThreadRepo(environment.product).insert(
        thread_id=selection.thread_id,
        agent_id=selection.agent_id,
        user_id=user,
        channel_type="dashboard",
        session_key="original-existing-reference",
    )
    with sqlite3.connect(environment.path) as conn:
        conn.executescript(
            (Path(__file__).parent / "db/fixtures/retained_cohost_103.sql").read_text("utf-8")
        )
        conn.execute(
            "INSERT INTO sessions (session_id,transcript_session_id,org_id,user_id,role,"
            "scopes_json,cwd,runtime_type,status,desired_state,transcript_path,created_at,"
            "last_active_at) VALUES ('original-app','original-history',?,?,'user','[]',"
            "'/original/repo','cohost','ended','stopped','/original/transcript',1,2)",
            (_IDS["homeOrg"], _IDS["homeMember"]),
        )
        conn.execute(
            "INSERT INTO cohost_sessions VALUES ('original-app',?,'original-native-agent',"
            "'original_native_history','/original/repo')",
            (_IDS["homeMember"],),
        )
    bindings = RetainedCohostBindingRepo(environment.path)
    selections = ProductCohostSelections(
        RetainedSessionProjectionRepo(environment.product), bindings
    )
    await selections.record_existing(
        selection,
        authority_id=principal.identity.snapshot.authority_id,
        original_app_session_id="original-app",
        organization_id=_IDS["homeOrg"],
        owner_id=_IDS["homeMember"],
    )
    repo = ExecutionRepo(environment.product)
    repo.register("same-worker", "same-product-boot", "worker")
    carrier = CanonicalRunAdmissions(repo)
    run = await carrier.submit(
        principal,
        user=user,
        agent=selection.agent_id,
        thread=selection.thread_id,
        operation="chat",
        key="original-operation-key",
        request={"request": {"text": "original request"}},
    )
    return SimpleNamespace(
        principal=principal,
        pair=pair,
        selection=selection,
        bindings=bindings,
        selections=selections,
        repo=repo,
        carrier=carrier,
        run=run,
    )


@pytest.mark.parametrize("change", [None, "revoked_before_claim", "original_rebind"])
async def test_real_bundle_admission_queue_selection_and_owner6_authority(
    application, environment, change
):
    queued = await queued_original(application, environment)
    persisted = queued.repo.get(queued.run["run_id"])["request_json"]
    assert queued.pair["access_token"] not in persisted
    assert queued.pair["refresh_token"] not in persisted
    if change == "revoked_before_claim":
        await environment.bundle.sessions.logout(queued.pair["access_token"])
        assert await queued.carrier.claim("same-worker", "same-product-boot") is None
        assert queued.repo.get(queued.run["run_id"])["state"] == "queued"
        return
    run = await queued.carrier.claim("same-worker", "same-product-boot")
    held = await queued.carrier.current(run)
    assert held is queued.principal
    native = []

    async def current_actor(value):
        assert value is held
        return (await value.revalidate()).snapshot

    async def owner_facts(actor, original):
        assert actor.organization_id == original.organization_id
        # Facts-only ordinary-member accessor, not a token or a resource grant.
        await asyncio.to_thread(
            environment.bundle.model_store.get_for_registered_member,
            organization_id=original.organization_id,
            member_id=original.owner_id,
        )

    async def exact_action_fixture(actor, original, operation, resource):
        assert actor.member.owner_id == original.owner_id
        assert (operation, resource) == ("session_connect", "original-app")

    async def native_fixture(binding):
        native.append(binding)
        return Runtime(binding)

    authority = RetainedCohostAuthority(
        queued.bindings,
        current_actor=current_actor,
        current_selection=queued.selections.current,
        require_owner_access=owner_facts,
        require_operation=exact_action_fixture,
        resolve_runtime=native_fixture,
    )
    if change == "original_rebind":
        with sqlite3.connect(environment.path) as conn:
            conn.execute("UPDATE cohost_sessions SET durable_session_id='replacement_history'")
        with pytest.raises(SessionProjectionUnavailable):
            await queued.selections.current(held.identity.snapshot, queued.selection)
        with pytest.raises(HostUnavailable, match="Host selection is unavailable"):
            await resolve_trusted_host(held, queued.selection, authority)
        assert native == []
        return
    host = await resolve_trusted_host(held, queued.selection, authority)
    assert len(native) == 1
    assert host.binding.account_id == _IDS["account"]
    assert host.binding.native.owner_id == _IDS["homeMember"]
    assert host.binding.native.durable_session_id == "original_native_history"
    assert json.loads(persisted)["canonical_actor"]["account_id"] == host.binding.account_id
    await environment.bundle.sessions.logout(queued.pair["access_token"])
    with pytest.raises(MossworkError):
        await held.revalidate()
    with pytest.raises(HostUnavailable, match="Current host binding is unavailable"):
        await host.authorize("session_connect", "original-app")
    assert len(native) == 1 and environment.native.calls == []


def test_equal_authority_strings_cannot_mix_other_snapshot_instance(application, environment):
    other = build_canonical_business(environment.path, **environment.kwargs)
    assert other.snapshots is not environment.bundle.snapshots
    with pytest.raises(ValueError, match="same trusted authority"):
        build_app(
            application.state.mosswork_server,
            **{**environment.bundle.app_kwargs(), "canonical_requests": other.requests},
        )


@pytest.mark.parametrize("graph_access", ["facts_returned_as_grant", "graph_denied"])
async def test_llm_credential_facts_cannot_grant_registered_graph_operation(
    application, environment, graph_access
):
    queued = await queued_original(application, environment)
    run = await queued.carrier.claim("same-worker", "same-product-boot")
    principal = await queued.carrier.current(run)
    original = await queued.selections.current(
        (await principal.revalidate()).snapshot, queued.selection
    )
    fixtures = Path(__file__).parent / "db/fixtures"
    with sqlite3.connect(environment.path) as conn:
        conn.executescript((fixtures / "retained_ontology_103.sql").read_text("utf-8"))
        conn.execute(
            "INSERT INTO ontology_projects (project_id,org_id,authority_ref,worker_ref,"
            "model_ref,source_refs_json,generation,created_at) "
            "VALUES ('graph-project',?,'graph-authority','worker','graph-model',"
            "'[\"registered-source\"]',7,1)",
            (original.organization_id,),
        )
        conn.execute(
            "INSERT INTO ontology_session_bindings VALUES "
            "('original-app','graph-project',?,?,'original-graph-binding',1)",
            (original.organization_id, original.owner_id),
        )
        conn.execute("CREATE TABLE mcp_servers (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO mcp_servers VALUES ('worker')")
        conn.executescript((fixtures / "retained_mcp_audit_103.sql").read_text("utf-8"))
    registry = RetainedOntologyBindingRepo(environment.path)
    registered = registry.resolve_original(
        original.application_session_id,
        organization_id=original.organization_id,
        owner_id=original.owner_id,
    )

    async def current_actor(value):
        assert value is principal
        return (await value.revalidate()).snapshot

    async def target_facts(actor, selected):
        assert selected == original and actor.organization_id == selected.organization_id
        await asyncio.to_thread(
            environment.bundle.model_store.get_for_registered_member,
            organization_id=selected.organization_id,
            member_id=selected.owner_id,
        )

    async def resource_access_fixture(actor, selected, operation, resource):
        assert actor.member.owner_id == selected.owner_id == original.owner_id
        assert (operation, resource) in {
            ("session_connect", original.application_session_id),
            ("sudograph_model_contract", registered.resource_ref),
        }

    async def native_fixture(binding):
        return Runtime(binding)

    authority = RetainedCohostAuthority(
        queued.bindings,
        current_actor=current_actor,
        current_selection=queued.selections.current,
        require_owner_access=target_facts,
        require_operation=resource_access_fixture,
        resolve_runtime=native_fixture,
    )
    host = await resolve_trusted_host(principal, queued.selection, authority)
    observed = []

    async def model_access(binding, actor_id, selected, graph, operation):
        actor = (await principal.revalidate()).snapshot
        assert binding == host.binding and actor_id == actor.member.owner_id
        assert selected == registered
        assert type(graph) is ModelBinding
        assert (graph.authority_ref, graph.model_ref, graph.resource_ref, graph.source_refs) == (
            registered.authority_ref,
            registered.model_ref,
            registered.resource_ref,
            registered.source_refs,
        )
        assert operation == "sudograph_model_contract"
        facts = await asyncio.to_thread(
            environment.bundle.model_store.get_for_registered_member,
            organization_id=original.organization_id,
            member_id=original.owner_id,
        )
        await asyncio.to_thread(
            environment.bundle.model_store.assert_registered_member_current, facts
        )
        observed.append(facts)
        if graph_access == "facts_returned_as_grant":
            # Counterexample: a successful LLM reader returned as an assertion
            # verdict must refuse, rather than authorize a graph operation.
            return facts
        raise PermissionError("The independent full graph grant is absent")

    transport = Transport()
    ports = RegisteredSudographPorts(
        host,
        original,
        registered,
        bindings=queued.bindings,
        registry=registry,
        audit=RetainedModelAuditRepo(environment.path),
        actor_id=(await principal.revalidate()).snapshot.member.owner_id,
        require_model_access=model_access,
        transport=transport,
    )
    with pytest.raises(SudographError) as failure:
        await SudographClient(ports.binding, ports).contract()
    assert failure.value.code == (
        "MODEL_OPERATION_UNAVAILABLE"
        if graph_access == "facts_returned_as_grant"
        else "ACCESS_DENIED"
    )
    assert len(observed) == 1 and not isinstance(observed[0], ModelBinding)
    assert transport.calls == [] and environment.native.calls == []
    with sqlite3.connect(environment.path) as conn:
        audit = conn.execute(
            "SELECT user_id,session_id,mcp_server_id,tool_name,status FROM mcp_audit_log"
        ).fetchall()
    assert audit == [
        (
            original.owner_id,
            original.application_session_id,
            "worker",
            "sudograph_model_contract",
            "error",
        )
    ]
