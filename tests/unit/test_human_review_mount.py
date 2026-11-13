"""Actual single startup, canonical middleware and original SQL/HTTP review.

Full graph access, writer custody and backend are explicit fixtures.
"""

import asyncio
import json
import sqlite3
from dataclasses import replace

import httpx
import pytest
from fastapi.routing import iter_route_contexts

from octop.infra.db.repos.retained_mcp import RetainedReviewedMcpRepo
from octop.infra.db.repos.retained_model_audit import RetainedModelAuditRepo
from octop.infra.native_host.human_review_audit import CanonicalHumanReviewAudit
from octop.infra.native_host.human_review_provider import CanonicalHumanReviewReaders
from octop.infra.sudograph.protocol import ModelBinding
from tests.unit.api.test_canonical_human_review import _AUTH, _URL
from tests.unit.api.test_canonical_human_review import review_http as review_http
from tests.unit.connectors.test_fixed_json import server as worker
from tests.unit.native_host.test_human_review import _FIXTURE, _POINTER, response, review_row
from tests.unit.native_host.test_human_review import human as human
from tests.unit.native_host.test_human_review_documents import _HTML, document, row_count
from tests.unit.native_host.test_human_review_documents import pages as pages
from tests.unit.test_canonical_product_startup import _policy_foreground
from tests.unit.test_canonical_product_startup import configuration as configuration
from tests.unit.test_canonical_product_startup import environment as environment
from tests.unit.test_canonical_product_startup import isolated_boot as isolated_boot
from tests.unit.test_canonical_product_startup import legacy_db as legacy_db
from tests.unit.test_canonical_product_startup import product as product


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    path = tmp_path / "fixture-source" / "octop" / "dashboard"
    path.mkdir(parents=True)
    (path / "index.html").write_text("<!doctype html><title>Existing dashboard</title>")
    monkeypatch.setattr("octop.api.app.__file__", str(path.parent / "api" / "app.py"))


def selected(configuration, state, *, configured=True):
    return replace(
        configuration,
        database_path=state.pages.human.path,
        secret=_FIXTURE["dummySecret"],
        issuer=_FIXTURE["issuer"],
        clock=lambda: _FIXTURE["verificationTime"],
        phone_config=None,
        phone_enabled=lambda: False,
        sms_sender=None,
        feishu_channel=None,
        feishu_plugin_id=None,
        feishu_connection_owner_id=None,
        feishu_sessions=None,
        writer=state.writer,
        human_review_reader=state.ports[0] if configured else None,
    )


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize("resolver", [False, object(), "reader"])
def test_invalid_resolver_refuses_before_boot(configuration, resolver):
    with pytest.raises(TypeError, match="actual current human review reader"):
        replace(configuration, human_review_reader=resolver)
    assert configuration.backend_factory.inputs == []


@pytest.mark.parametrize("configured", [False, True])
async def test_same_foreground_capture_restore_and_private_page_contract(
    configuration, review_http, isolated_boot, monkeypatch, dashboard, configured
):
    state = review_http
    config = selected(configuration, state, configured=configured)
    payload = response({**_POINTER, "html": _HTML})
    async with worker(
        lambda _: (200, {"Content-Type": "application/json"}, json.dumps(payload).encode())
    ) as (port, upstream):
        review_row(state.pages.human, f"http://127.0.0.1:{port}/mcp")

        async def exercise(app, server):
            bound = server.canonical
            assert len(bound.business.app_kwargs()) == 11
            assert len(vars(bound.runtime_inputs)) == 5
            routes = [
                route
                for route in iter_route_contexts(app.routes)
                if route.path
                == "/api/identity/ontology-projects/{project_id}/reviews/{review_id}/v1"
            ]
            assert len(routes) == (2 if configured else 0)
            async with client(app) as http:
                docs = (await http.get("/api/openapi.json")).json()
                schema_path = "/api/identity/ontology-projects/{project_id}/reviews/{review_id}/v1"
                if not configured:
                    assert bound.human_review_ports is None
                    assert not hasattr(app.state, "canonical_human_review_ports")
                    assert schema_path not in docs["paths"]
                    for method in ["GET", "POST"]:
                        result = await http.request(method, _URL, headers=_AUTH)
                        assert result.status_code == 503
                        assert result.json() == {"canonical_resource_unavailable": True}
                    assert upstream == [] and state.calls == [] and row_count(state.pages) == 0
                    return
                ports = app.state.canonical_human_review_ports
                assert ports is bound.human_review_ports
                assert ports[0] is config.human_review_reader
                assert ports[1] is bound.business.requests is app.state.canonical_requests
                assert ports[2]._uri == config.database_path.as_uri()
                assert ports[3] is config.writer
                operations = docs["paths"][schema_path]
                assert set(operations) == {"get", "post"}
                assert all(operation["tags"] == ["auth"] for operation in operations.values())
                assert all(not operation.get("requestBody") for operation in operations.values())
                missing = await http.get(_URL, headers=_AUTH)
                assert missing.status_code == 503 and upstream == []
                captured = await http.post(_URL, headers=_AUTH)
                assert captured.status_code == 200 and len(upstream) == 1
                assert captured.json() == {"record": document(state.pages).record(), "html": _HTML}
                assert row_count(state.pages) == 1
                assert captured.headers["cache-control"] == "no-store"
                assert captured.headers["x-content-type-options"] == "nosniff"
                # Removing the upstream after capture cannot trigger a refetch.
                with sqlite3.connect(state.pages.human.path) as db:
                    db.execute("UPDATE mcp_servers SET url='http://127.0.0.1:1/mcp'")
                restored = await http.get(_URL, headers=_AUTH)
                assert restored.status_code == 200 and restored.json() == captured.json()
                assert len(upstream) == 1 and row_count(state.pages) == 1
                parent = await http.get("/reviews/project/" + "a" * 32)
                assert parent.status_code == 200 and "Existing dashboard" in parent.text
                assert _HTML not in parent.text and "record_sha256" not in parent.text
                # Normal canonical authentication allocates its existing
                # unprivileged Product compatibility row, never a local admin.
                assert server.user_manager.count() == 1
                with server.services.db.connect() as db:
                    row = db.execute(
                        "SELECT role,password_hash,permissions,expert_access_mode FROM users"
                    ).fetchone()
                assert tuple(row) == ("user", None, "[]", "assigned_only")
                assert state.calls and all(
                    principal._CanonicalAuthenticatedRequest__resolver is ports[1]
                    for principal, _, _ in state.calls
                )
                with sqlite3.connect(state.pages.human.path) as db:
                    assert db.execute(
                        "SELECT DISTINCT session_id FROM mcp_audit_log"
                    ).fetchall() == [(None,)]
                    assert not db.execute(
                        "SELECT name FROM sqlite_master WHERE name IN ('agents','sessions')"
                    ).fetchall()

        await _policy_foreground(config, isolated_boot, monkeypatch, exercise)


@pytest.mark.parametrize(
    "method,path",
    [(method, _URL) for method in ["PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]]
    + [
        ("GET", _URL + "/"),
        ("GET", _URL + "/comments"),
        ("GET", _URL.replace("/project/", "/bad.project/")),
        ("GET", _URL.replace("/project/", "/%70roject/")),
        ("GET", _URL.replace("a" * 32, "A" * 32)),
    ],
)
async def test_adjacent_routes_never_bypass_canonical_gate(
    configuration, review_http, isolated_boot, monkeypatch, method, path
):
    async def exercise(app, server):
        async with client(app) as http:
            result = await http.request(method, path, headers=_AUTH)
        assert result.status_code == 503
        assert review_http.calls == [] and row_count(review_http.pages) == 0

    await _policy_foreground(
        selected(configuration, review_http), isolated_boot, monkeypatch, exercise
    )


@pytest.mark.parametrize("replacement", ["ports", "requests", "snapshots", "configured"])
async def test_replaced_configured_instance_refuses_in_full_middleware(
    configuration, review_http, isolated_boot, monkeypatch, replacement
):
    async def exercise(app, server):
        if replacement == "ports":
            app.state.canonical_human_review_ports = (*app.state.canonical_human_review_ports,)
        elif replacement == "requests":
            app.state.canonical_requests = object()
        elif replacement == "snapshots":
            app.state.canonical_identity = object()
        else:
            server.canonical = replace(server.canonical, human_review_ports=None)
        async with client(app) as http:
            result = await http.get(_URL, headers=_AUTH)
        assert result.status_code == 503 and review_http.calls == []
        assert row_count(review_http.pages) == 0

    await _policy_foreground(
        selected(configuration, review_http), isolated_boot, monkeypatch, exercise
    )


@pytest.mark.parametrize("withdraw", ["resolver", "after-commit"])
async def test_config_withdrawn_while_waiting_withholds_page_and_get_recovers_commit(
    configuration, review_http, isolated_boot, monkeypatch, withdraw
):
    state = review_http
    payload = response({**_POINTER, "html": _HTML})
    async with worker(
        lambda _: (200, {"Content-Type": "application/json"}, json.dumps(payload).encode())
    ) as (port, upstream):
        review_row(state.pages.human, f"http://127.0.0.1:{port}/mcp")

        async def exercise(app, server):
            original = server.canonical
            if withdraw == "resolver":

                async def withdrawn(principal):
                    await asyncio.sleep(0)
                    server.canonical = replace(original, human_review_ports=None)
                    app.state.canonical_human_review_ports = None

                state.on_resolve = withdrawn
            else:
                real_writer = state.writer.assert_current_writer

                async def withdrawn_writer():
                    await real_writer()
                    if state.writer.calls == 3:
                        server.canonical = replace(original, human_review_ports=None)

                monkeypatch.setattr(state.writer, "assert_current_writer", withdrawn_writer)
            async with client(app) as http:
                result = await http.post(_URL, headers=_AUTH)
                assert result.status_code == 503 and _HTML not in result.text
                assert result.headers["cache-control"] == "no-store"
                effects = 0 if withdraw == "resolver" else 1
                assert len(upstream) == effects and row_count(state.pages) == effects
                # The one original capture may commit. Restoring admission
                # allows only GET recovery of that same persisted document.
                server.canonical = original
                app.state.canonical_human_review_ports = original.human_review_ports
                state.on_resolve = None
                recovered = await http.get(_URL, headers=_AUTH)
                assert recovered.status_code == (503 if effects == 0 else 200)
                if effects:
                    assert recovered.json()["html"] == _HTML
                assert len(upstream) == effects and row_count(state.pages) == effects

        await _policy_foreground(
            selected(configuration, state), isolated_boot, monkeypatch, exercise
        )


@pytest.mark.parametrize("failure,status", [("credentials", 401), ("grant", 403)])
async def test_current_human_and_graph_refusal_survive_main_middleware(
    configuration, review_http, isolated_boot, monkeypatch, failure, status
):
    async def exercise(app, server):
        if failure == "grant":
            review_http.allowed = False
        else:

            async def revoke(principal):
                with sqlite3.connect(review_http.pages.human.path) as db:
                    db.execute("UPDATE users SET status='disabled'")

            review_http.on_resolve = revoke
        async with client(app) as http:
            result = await http.get(_URL, headers=_AUTH)
        assert result.status_code == status
        assert "private-grant-marker" not in result.text and row_count(review_http.pages) == 0

    await _policy_foreground(
        selected(configuration, review_http), isolated_boot, monkeypatch, exercise
    )


async def test_same_foreground_uses_concrete_current_document_reader(
    configuration, review_http, isolated_boot, monkeypatch, dashboard
):
    state = review_http
    human = state.pages.human
    current_requests, granted, audited = [], [], []
    model = ModelBinding("authority", "model", "project", ("source",))

    async def grant(principal, project, actual_model, *, operation, request):
        assert principal._CanonicalAuthenticatedRequest__resolver is current_requests[0]
        assert principal is not human.principal and principal.identity == human.principal.identity
        assert project == human.project and actual_model == model and operation == "review"
        assert request == {
            "operation": "document",
            "endpoint": "/review/" + "a" * 32 + "/v1",
            "resource": "project",
        }
        granted.append(principal)
        if not state.allowed:
            raise PermissionError("fixture current graph deny")

    async def audit(principal, project, actual_model, status):
        assert principal._CanonicalAuthenticatedRequest__resolver is current_requests[0]
        assert project == human.project and actual_model == model
        audited.append(status)
        await asyncio.to_thread(
            RetainedModelAuditRepo(human.path).record_project_access,
            project,
            actor_id=principal.identity.snapshot.member.owner_id,
            operation="review",
            status=status,
        )

    actual = CanonicalHumanReviewReaders(
        projects=human.projects,
        reviews=RetainedReviewedMcpRepo(human.path),
        require_review_access=grant,
        audit_review=audit,
        allowed_addresses=("127.0.0.1",),
    )
    config = replace(selected(configuration, state), human_review_reader=actual)
    payload = response({**_POINTER, "html": _HTML})
    async with worker(
        lambda _: (200, {"Content-Type": "application/json"}, json.dumps(payload).encode())
    ) as (port, upstream):
        review_row(human, f"http://127.0.0.1:{port}/mcp")

        async def exercise(app, server):
            bound = server.canonical
            ports = app.state.canonical_human_review_ports
            assert ports is bound.human_review_ports and ports[0] is actual
            assert ports[1] is bound.business.requests is app.state.canonical_requests
            assert ports[2]._uri == config.database_path.as_uri() and ports[3] is state.writer
            assert len(bound.business.app_kwargs()) == 11 and len(vars(bound.runtime_inputs)) == 5
            assert configuration.backend_factory.inputs == [bound.runtime_inputs]
            current_requests.append(bound.business.requests)
            async with client(app) as http:
                captured = await http.post(_URL, headers=_AUTH)
                assert captured.status_code == 200
                assert captured.json() == {"record": document(state.pages).record(), "html": _HTML}
                assert len(upstream) == 1 and row_count(state.pages) == 1
                with sqlite3.connect(human.path) as db:
                    db.execute("UPDATE mcp_servers SET url='http://127.0.0.1:1/mcp'")
                restored = await http.get(_URL, headers=_AUTH)
                assert restored.status_code == 200 and restored.json() == captured.json()
                state.allowed = False
                refused = await http.get(_URL, headers=_AUTH)
                assert refused.status_code == 403 and _HTML not in refused.text
                assert len(upstream) == 1 and row_count(state.pages) == 1
            assert granted and audited == ["success", "success", "error"]
            with sqlite3.connect(human.path) as db:
                assert db.execute("SELECT DISTINCT session_id FROM mcp_audit_log").fetchall() == [
                    (None,)
                ]
                assert not db.execute(
                    "SELECT name FROM sqlite_master WHERE name IN "
                    "('agents','sessions','ontology_session_bindings')"
                ).fetchall()

        await _policy_foreground(config, isolated_boot, monkeypatch, exercise)


@pytest.mark.parametrize("withdrawal,status", [("graph", 403), ("human", 401)])
async def test_same_foreground_concrete_reader_and_canonical_audit_after_withdrawal(
    configuration, review_http, isolated_boot, monkeypatch, dashboard, withdrawal, status
):
    state = review_http
    human = state.pages.human
    held_requests, audits, withdrawn = [], [], []
    audit_repo = RetainedModelAuditRepo(human.path)

    async def grant(principal, project, model, *, operation, request):
        assert principal._CanonicalAuthenticatedRequest__resolver is held_requests[0]
        assert project == human.project and operation == "review"
        assert model == ModelBinding("authority", "model", "project", ("source",))
        if withdrawn:
            if withdrawal == "graph":
                raise PermissionError("fixture current review grant withdrawn")
            with sqlite3.connect(human.path) as db:
                db.execute("UPDATE users SET status='disabled'")

    async def audit(principal, project, model, outcome):
        producer = CanonicalHumanReviewAudit(
            principal=principal, project=project, audits=audit_repo, writer=state.writer
        )
        audits.append(producer)
        await producer(principal, project, model, outcome)

    resolver = CanonicalHumanReviewReaders(
        projects=human.projects,
        reviews=RetainedReviewedMcpRepo(human.path),
        require_review_access=grant,
        audit_review=audit,
        allowed_addresses=("127.0.0.1",),
    )
    config = replace(selected(configuration, state), human_review_reader=resolver)
    payload = response({**_POINTER, "html": _HTML})
    async with worker(
        lambda _: (200, {"Content-Type": "application/json"}, json.dumps(payload).encode())
    ) as (port, upstream):
        review_row(human, f"http://127.0.0.1:{port}/mcp")

        async def exercise(app, server):
            bound = server.canonical
            ports = app.state.canonical_human_review_ports
            assert ports is bound.human_review_ports and ports[0] is resolver
            assert ports[1] is bound.business.requests and ports[3] is state.writer
            assert len(bound.business.app_kwargs()) == 11 and len(vars(bound.runtime_inputs)) == 5
            held_requests.append(bound.business.requests)
            async with client(app) as http:
                captured = await http.post(_URL, headers=_AUTH)
                assert captured.status_code == 200 and captured.json()["html"] == _HTML
                withdrawn.append(True)
                refused = await http.get(_URL, headers=_AUTH)
                assert refused.status_code == status and _HTML not in refused.text
                assert len(upstream) == 1 and row_count(state.pages) == 1
            assert len(audits) == 2 and audits[0] is not audits[1]
            with sqlite3.connect(human.path) as db:
                recorded = db.execute(
                    "SELECT session_id,user_id,status FROM mcp_audit_log ORDER BY rowid"
                ).fetchall()
                actor = human.principal.identity.snapshot.member.owner_id
                assert recorded == [(None, actor, "success"), (None, actor, "error")]
                assert not db.execute(
                    "SELECT name FROM sqlite_master WHERE name IN "
                    "('agents','sessions','ontology_session_bindings')"
                ).fetchall()

        await _policy_foreground(config, isolated_boot, monkeypatch, exercise)


async def test_same_foreground_uses_bound_canonical_audit_constructor(
    configuration, review_http, isolated_boot, monkeypatch, dashboard
):
    state = review_http
    human = state.pages.human
    requests = []

    async def grant(principal, project, model, *, operation, request):
        assert principal._CanonicalAuthenticatedRequest__resolver is requests[0]
        assert project == human.project and operation == "review"
        assert model == ModelBinding("authority", "model", "project", ("source",))
        assert request == {
            "operation": "document",
            "endpoint": "/review/" + "a" * 32 + "/v1",
            "resource": "project",
        }
        if not state.allowed:
            raise PermissionError("fixture current review grant withdrawn")

    resolver = CanonicalHumanReviewReaders.with_canonical_audit(
        projects=human.projects,
        reviews=RetainedReviewedMcpRepo(human.path),
        audits=RetainedModelAuditRepo(human.path),
        writer=state.writer,
        require_review_access=grant,
        allowed_addresses=("127.0.0.1",),
    )
    config = replace(selected(configuration, state), human_review_reader=resolver)
    payload = response({**_POINTER, "html": _HTML})
    async with worker(
        lambda _: (200, {"Content-Type": "application/json"}, json.dumps(payload).encode())
    ) as (port, upstream):
        review_row(human, f"http://127.0.0.1:{port}/mcp")

        async def exercise(app, server):
            bound = server.canonical
            ports = app.state.canonical_human_review_ports
            assert ports is bound.human_review_ports and ports[0] is resolver
            assert ports[1] is bound.business.requests is app.state.canonical_requests
            assert ports[3] is config.writer is state.writer
            assert len(bound.business.app_kwargs()) == 11 and len(vars(bound.runtime_inputs)) == 5
            assert configuration.backend_factory.inputs == [bound.runtime_inputs]
            requests.append(bound.business.requests)
            async with client(app) as http:
                captured = await http.post(_URL, headers=_AUTH)
                assert captured.status_code == 200
                assert captured.json() == {"record": document(state.pages).record(), "html": _HTML}
                state.allowed = False
                refused = await http.get(_URL, headers=_AUTH)
                assert refused.status_code == 403 and _HTML not in refused.text
                assert len(upstream) == 1 and row_count(state.pages) == 1
            with sqlite3.connect(human.path) as db:
                recorded = db.execute(
                    "SELECT session_id,user_id,status FROM mcp_audit_log ORDER BY rowid"
                ).fetchall()
                actor = human.principal.identity.snapshot.member.owner_id
                assert recorded == [(None, actor, "success"), (None, actor, "error")]
                assert not db.execute(
                    "SELECT name FROM sqlite_master WHERE name IN "
                    "('agents','sessions','ontology_session_bindings')"
                ).fetchall()

        await _policy_foreground(config, isolated_boot, monkeypatch, exercise)
