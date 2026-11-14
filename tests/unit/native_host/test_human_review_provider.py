"""Real sealed actor/retained SQL/HTTP assembly; grants and writer are fixtures."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3

import httpx
import pytest
from fastapi import FastAPI
from tests.unit.connectors.test_fixed_json import server
from tests.unit.native_host.test_human_review import (
    _ENDPOINT,
    _FIXTURE,
    _IDS,
    _POINTER,
    change,
    response,
    review_row,
)
from tests.unit.native_host.test_human_review import human as human
from tests.unit.native_host.test_human_review_documents import Writer
from tests.unit.native_host.test_human_review_documents import pages as pages

from octop.api.routers.canonical_human_review import build_canonical_human_review_router
from octop.infra.db.repos.retained_mcp import RetainedReviewedMcpRepo
from octop.infra.db.repos.retained_model_audit import RetainedModelAuditRepo
from octop.infra.db.repos.retained_ontology import RetainedModelInvalid
from octop.infra.errors import MossworkError
from octop.infra.identity.request_principal import CanonicalAuthenticatedRequest, authenticate
from octop.infra.native_host.human_review import HumanReviewReader
from octop.infra.native_host.human_review_provider import CanonicalHumanReviewReaders
from octop.infra.sudograph.protocol import SudographError


def provider(human, **overrides):
    return CanonicalHumanReviewReaders(
        **{
            "projects": human.projects,
            "reviews": RetainedReviewedMcpRepo(human.path),
            "require_review_access": human.grant,
            "audit_review": human.audit,
            "allowed_addresses": ("127.0.0.1",),
            **overrides,
        }
    )


async def resolve(human, actual=None, *, endpoint=_ENDPOINT):
    return await (actual or provider(human))(
        human.principal, project_id="project", endpoint=endpoint
    )


@pytest.mark.parametrize("after_reply", [False, True])
async def test_plain_document_grant_refusal_preserves_original_domain_outcome(human, after_reply):
    def respond(_):
        human.allowed = False
        return (
            200,
            {"Content-Type": "application/json"},
            json.dumps(response({**_POINTER, "html": "<p>original</p>"})).encode(),
        )

    async with server(respond) as (port, calls):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        reader = await resolve(human)
        if not after_reply:
            human.allowed = False
        with pytest.raises(SudographError) as caught:
            await reader.review_document(_ENDPOINT)
        assert caught.value.code == (
            "MODEL_OPERATION_UNAVAILABLE" if after_reply else "ACCESS_DENIED"
        )
        assert len(calls) == int(after_reply)
    assert human.audits == ["error"]


async def test_original_reader_captures_then_restores_without_worker_retry(pages):
    human = pages.human
    html = "<html><body>same original HTML 🐈</body></html>"
    async with server(
        lambda _: (
            200,
            {"Content-Type": "application/json"},
            json.dumps(response({**_POINTER, "html": html})).encode(),
        )
    ) as (port, requests):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        reader = await resolve(human)
        assert type(reader) is HumanReviewReader
        assert reader._human.principal is human.principal
        assert requests == []  # Constructing a reader does not fetch or issue anything.
        captured = await reader.capture_review(_ENDPOINT, pages.store, writer=Writer())
        assert len(requests) == 1
    # A fresh resolver invocation recovers the same bytes with the worker offline.
    restored = await (await resolve(human)).restore_review(_ENDPOINT, pages.store)
    assert restored == captured and restored.html == html
    with sqlite3.connect(human.path) as db:
        assert (
            db.execute("SELECT session_id,user_id,status FROM mcp_audit_log").fetchall()
            == [(None, _IDS["homeMember"], "success")] * 2
        )
        assert db.execute("SELECT count(*) FROM ontology_review_values").fetchone() == (1,)
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('sessions','agents')"
        ).fetchall()


@pytest.mark.parametrize("verdict", [False, True, {"allowed": True}, {"llm_key": "fixture"}])
async def test_missing_full_model_grant_cannot_create_reader(human, verdict):
    human.verdict = verdict
    with pytest.raises(PermissionError):
        await resolve(human)
    assert human.audits == ["error"] and len(human.grant_requests) == 1


async def test_independent_graph_deny_precedes_secret_or_network(human):
    human.allowed = False

    async def secrets(*_):
        pytest.fail("No secret capability may run before the independent grant")

    with pytest.raises(PermissionError):
        await resolve(human, provider(human, list_secrets=secrets))
    assert human.audits == ["error"]


async def test_revoked_actor_refuses_before_project_read(human, monkeypatch):
    change(human.path, "UPDATE users SET status='disabled'")

    def never_read(*_, **__):
        pytest.fail("No inventory read for a revoked actor")

    monkeypatch.setattr(type(human.projects), "get", never_read)
    with pytest.raises(MossworkError):
        await resolve(human)


async def test_project_changed_during_grant_is_not_retargeted(human):
    original = human.project

    async def grant(*args, **kwargs):
        await human.grant(*args, **kwargs)
        change(human.path, "UPDATE ontology_projects SET generation=generation+1")

    with pytest.raises(RetainedModelInvalid):
        await resolve(human, provider(human, require_review_access=grant))
    assert human.project == original


async def test_revocation_during_grant_releases_no_reader(human):
    async def grant(*args, **kwargs):
        await human.grant(*args, **kwargs)
        change(human.path, "UPDATE users SET status='disabled'")

    with pytest.raises(MossworkError):
        await resolve(human, provider(human, require_review_access=grant))
    assert human.audits == ["error"]


@pytest.mark.parametrize("operation", ["head", "page", "another-document"])
async def test_resolved_reader_cannot_change_original_document_scope(human, operation):
    reader = await resolve(human)
    selected = {
        "head": {"operation": "head"},
        "page": {"operation": "page", "params": {}},
        "another-document": {"operation": "document", "endpoint": "/review/" + "d" * 32 + "/v1"},
    }[operation]
    with pytest.raises(SudographError, match="denied"):
        await reader.review(selected)
    assert human.audits == ["error"]


async def test_concurrent_document_construction_never_uses_latest_request(human):
    observed = []

    async def grant(*args, **kwargs):
        observed.append(copy.deepcopy(kwargs["request"]))
        await human.grant(*args, **kwargs)
        await asyncio.sleep(0)

    factory = provider(human, require_review_access=grant)
    other = "/review/" + "d" * 32 + "/v1"
    first, second = await asyncio.gather(
        resolve(human, factory), resolve(human, factory, endpoint=other)
    )
    assert first is not second
    assert {entry["endpoint"] for entry in observed} == {_ENDPOINT, other}
    with pytest.raises(SudographError, match="denied"):
        await first.review_document(other)
    with pytest.raises(SudographError, match="denied"):
        await second.review_document(_ENDPOINT)


async def test_grant_cannot_mutate_retained_document_request(human):
    async def grant(*args, **kwargs):
        await human.grant(*args, **kwargs)
        kwargs["request"]["endpoint"] = "/review/" + "d" * 32 + "/v1"

    reader = await resolve(human, provider(human, require_review_access=grant))
    with pytest.raises(SudographError, match="denied"):
        await reader.review_document("/review/" + "d" * 32 + "/v1")
    assert human.grant_requests[0]["endpoint"] == _ENDPOINT


async def test_actual_task_cancel_during_authorization_stays_cancelled(human):
    started, held = asyncio.Event(), asyncio.Event()

    async def grant(*args, **kwargs):
        await human.grant(*args, **kwargs)
        started.set()
        await held.wait()

    task = asyncio.create_task(resolve(human, provider(human, require_review_access=grant)))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled() and human.audits == []


@pytest.mark.parametrize("principal", [None, {}, {"actor": "fixture"}])
async def test_metadata_cannot_substitute_for_sealed_principal(human, principal):
    with pytest.raises(PermissionError):
        await provider(human)(principal, project_id="project", endpoint=_ENDPOINT)
    assert human.grant_requests == []


async def test_raw_unsealed_canonical_object_refuses(human):
    forged = object.__new__(CanonicalAuthenticatedRequest)
    with pytest.raises(MossworkError):
        await provider(human)(forged, project_id="project", endpoint=_ENDPOINT)
    assert human.grant_requests == []


@pytest.mark.parametrize(
    "endpoint", ["https://worker.example/review/a/v1", "/review/a/v1", _ENDPOINT + "?x=1"]
)
async def test_invalid_document_selector_refuses_before_grant(human, endpoint):
    with pytest.raises(ValueError):
        await resolve(human, endpoint=endpoint)
    assert human.grant_requests == []


@pytest.mark.parametrize("name", ["require_review_access", "audit_review", "list_secrets"])
def test_absent_or_malformed_authority_has_no_default(human, name):
    with pytest.raises(ValueError):
        provider(human, **{name: False})


def test_different_canonical_store_cannot_supply_review_registration(human, tmp_path):
    with pytest.raises(ValueError):
        provider(human, reviews=RetainedReviewedMcpRepo(tmp_path / "other.sqlite"))


async def test_equal_identity_from_two_real_seals_keeps_each_original_principal(human):
    requests = human.principal._CanonicalAuthenticatedRequest__resolver
    token = next(item["token"] for item in _FIXTURE["cases"] if item["name"] == "sms_home")
    second = await authenticate(requests, token)
    assert second is not human.principal and second.identity == human.principal.identity
    observed = []

    async def grant(principal, project, model, *, operation, request):
        assert principal is human.principal or principal is second
        assert project == human.project and model.resource_ref == "project"
        assert operation == "review" and request["endpoint"] == _ENDPOINT
        observed.append(principal)
        await asyncio.sleep(0)

    factory = provider(human, require_review_access=grant)
    first_reader, second_reader = await asyncio.gather(
        factory(human.principal, project_id="project", endpoint=_ENDPOINT),
        factory(second, project_id="project", endpoint=_ENDPOINT),
    )
    assert first_reader._human.principal is human.principal
    assert second_reader._human.principal is second
    assert any(value is human.principal for value in observed)
    assert any(value is second for value in observed)


def require_worker_secret(human):
    with sqlite3.connect(human.path) as db:
        db.execute(
            "UPDATE mcp_servers SET auth_type='secret_ref',secret_ref='system:worker-secret'"
        )
        db.execute(
            "INSERT INTO config_items(id,name,pinyin,scope,scheme,status,org_id,created_at,updated_at) "
            "VALUES (1,'worker','worker-secret','system','bearer',1,?,1,1)",
            (_IDS["homeOrg"],),
        )
        db.execute(
            "INSERT INTO config_entries(config_item_id,config_key,name,created_at,updated_at) "
            "VALUES (1,'token','token',1,1)"
        )


async def test_secret_reference_without_authorized_capability_sends_nothing(human):
    async with server(lambda _: pytest.fail("No unauthenticated fallback")) as (port, calls):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        require_worker_secret(human)
        reader = await resolve(human)
        with pytest.raises(SudographError):
            await reader.review_document(_ENDPOINT)
        assert calls == []


@pytest.mark.parametrize("withdraw", [False, True])
async def test_original_secret_namespace_is_passed_through_current_grant_fences(human, withdraw):
    secret_calls = []

    async def secrets(namespace, subject):
        assert namespace == f"org:{_IDS['homeOrg']}:system:worker-secret"
        assert subject == f"system:moss:{_IDS['homeOrg']}"
        secret_calls.append((namespace, subject))
        if withdraw:
            human.allowed = False
        return [{"key": "token", "value": "fixture-secret"}]

    payload = response({**_POINTER, "html": "<p>original</p>"})
    async with server(
        lambda _: (200, {"Content-Type": "application/json"}, json.dumps(payload).encode())
    ) as (port, calls):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        require_worker_secret(human)
        reader = await resolve(human, provider(human, list_secrets=secrets))
        assert secret_calls == []
        if withdraw:
            with pytest.raises(SudographError):
                await reader.review_document(_ENDPOINT)
            assert calls == [] and secret_calls
        else:
            assert (await reader.review_document(_ENDPOINT)).html == "<p>original</p>"
            assert len(calls) == 1
            # The existing loopback helper lowercases the complete header line.
            assert calls[0]["headers"]["authorization"] == "bearer fixture-secret"


async def test_existing_http_leaf_uses_real_resolver_and_same_capture_recovery_tuple(pages):
    human = pages.human
    actors = []

    async def grant(principal, project, model, *, operation, request):
        assert type(principal) is CanonicalAuthenticatedRequest
        assert principal.identity == human.principal.identity
        assert project == human.project and model.resource_ref == project.project_id
        assert operation == "review" and request["endpoint"] == _ENDPOINT
        actors.append(principal)

    async def audit(principal, project, model, status):
        assert any(principal is value for value in actors)
        await asyncio.to_thread(
            RetainedModelAuditRepo(human.path).record_project_access,
            project,
            actor_id=principal.identity.snapshot.member.owner_id,
            operation="review",
            status=status,
        )

    factory = provider(human, require_review_access=grant, audit_review=audit)
    requests = human.principal._CanonicalAuthenticatedRequest__resolver
    writer = Writer()
    app = FastAPI(redirect_slashes=False)
    app.state.canonical_requests = requests
    app.state.canonical_human_review_ports = (factory, requests, pages.store, writer)
    app.include_router(
        build_canonical_human_review_router(
            factory, requests=requests, documents=pages.store, writer=writer
        ),
        prefix="/api/identity",
    )
    token = next(item["token"] for item in _FIXTURE["cases"] if item["name"] == "sms_home")
    url = "/api/identity/ontology-projects/project/reviews/" + "a" * 32 + "/v1"
    body = response({**_POINTER, "html": "<p>same retained page</p>"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={"Authorization": "Bearer " + token},
    ) as client:
        async with server(
            lambda _: (200, {"Content-Type": "application/json"}, json.dumps(body).encode())
        ) as (port, worker_calls):
            review_row(human, f"http://127.0.0.1:{port}/mcp")
            captured = await client.post(url)
            assert captured.status_code == 200
            assert len(worker_calls) == 1
        restored = await client.get(url)
    assert restored.status_code == 200
    assert restored.json() == captured.json()
    assert restored.headers["cache-control"] == "no-store"
    assert actors[0] is not human.principal
    assert any(value is not actors[0] for value in actors)


@pytest.mark.parametrize("audit_result", [False, True, {"saved": True}])
async def test_denial_audit_requires_strict_completion_and_original_actor(human, audit_result):
    human.allowed = False
    calls = []

    async def audit(principal, project, model, status):
        assert principal is human.principal and project == human.project
        assert model.resource_ref == "project" and status == "error"
        calls.append(status)
        return audit_result

    with pytest.raises(PermissionError):
        await resolve(human, provider(human, audit_review=audit))
    assert calls == ["error"]


async def test_error_audit_failure_releases_no_reader_or_retry(human):
    human.allowed = False
    calls = []

    async def audit(principal, project, model, status):
        calls.append((principal, project, model, status))
        raise RuntimeError("fixture audit writer unavailable")

    with pytest.raises(RuntimeError, match="fixture audit writer unavailable"):
        await resolve(human, provider(human, audit_review=audit))
    assert len(calls) == 1 and calls[0][0] is human.principal
