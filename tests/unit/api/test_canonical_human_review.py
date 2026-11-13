"""Real canonical JWT/SQL/HTTP with explicit fixture graph and writer grants."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from tests.unit.connectors.test_fixed_json import server
from tests.unit.native_host.test_human_review import (
    _ENDPOINT,
    _FIXTURE,
    _POINTER,
    compose,
    response,
    review_row,
)
from tests.unit.native_host.test_human_review import human as human
from tests.unit.native_host.test_human_review_documents import (
    _HTML,
    Writer,
    document,
    persist,
    row_count,
)
from tests.unit.native_host.test_human_review_documents import pages as pages

from octop.api.routers.canonical_human_review import build_canonical_human_review_router
from octop.infra.connectors.tool_protocol import content_digest
from octop.infra.db.repos.retained_model_audit import RetainedModelAuditRepo
from octop.infra.db.repos.retained_review import RetainedHumanReviewDocumentRepo
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.request_principal import CanonicalAuthenticatedRequest
from octop.infra.native_host.human_review import HumanReviewReader
from octop.infra.sudograph.protocol import ModelBinding, SudographError
from octop.infra.sudograph.review_read import review_page_request

_URL = "/api/identity/ontology-projects/project/reviews/" + "a" * 32 + "/v1"
_TOKEN = next(item["token"] for item in _FIXTURE["cases"] if item["name"] == "sms_home")
_AUTH = {"Authorization": "Bearer " + _TOKEN}


@pytest.fixture
def review_http(pages):
    state = SimpleNamespace(
        pages=pages,
        calls=[],
        readers=[],
        grants=[],
        allowed=True,
        on_resolve=None,
        after_reader=None,
        reader_override=None,
        provider_error=None,
        writer=Writer(),
    )
    # Reuse the actual concrete test resolver/snapshot/Product projection, not
    # a forged principal, dependency override or DTO-to-principal conversion.
    requests = pages.human.principal._CanonicalAuthenticatedRequest__resolver

    async def resolve_reader(principal, *, project_id, endpoint):
        assert type(principal) is CanonicalAuthenticatedRequest
        assert principal is not pages.human.principal
        assert principal.identity == pages.human.principal.identity
        state.calls.append((principal, project_id, endpoint))
        if state.on_resolve:
            await state.on_resolve(principal)
        if state.provider_error:
            raise state.provider_error
        if state.reader_override is not None:
            return state.reader_override

        async def grant(actual, project, model, *, operation, request):
            assert actual is principal and project == pages.human.project
            assert model == ModelBinding("authority", "model", "project", ("source",))
            assert operation == "review"
            state.grants.append((actual, request))
            if not state.allowed:
                raise PermissionError("private-grant-marker")

        async def audit(actual, project, model, status):
            assert actual is principal and project == pages.human.project
            assert model.resource_ref == project_id
            await asyncio.to_thread(
                RetainedModelAuditRepo(pages.human.path).record_project_access,
                project,
                actor_id=principal.identity.snapshot.member.owner_id,
                operation="review",
                status=status,
            )

        proxy = SimpleNamespace(**vars(pages.human))
        proxy.principal = principal
        _, reader = compose(proxy, grant=grant, audit=audit)
        if state.after_reader:
            state.after_reader(reader)
        state.readers.append(reader)
        return reader

    # Product composition owns the final exact-path middleware/mount. This
    # small app exercises the leaf and the real Bearer dependency only.
    app = FastAPI(redirect_slashes=False)
    state.ports = (resolve_reader, requests, pages.store, state.writer)
    app.state.canonical_requests = requests
    app.state.canonical_human_review_ports = state.ports
    app.include_router(
        build_canonical_human_review_router(
            resolve_reader, requests=requests, documents=pages.store, writer=state.writer
        ),
        prefix="/api/identity",
    )
    state.app = app
    return state


def client(state):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=state.app), base_url="http://test")


def private(result):
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["x-content-type-options"] == "nosniff"


async def capture(state):
    payload = response({**_POINTER, "html": _HTML})
    async with server(
        lambda _: (200, {"Content-Type": "application/json"}, json.dumps(payload).encode())
    ) as (port, requests):
        review_row(state.pages.human, f"http://127.0.0.1:{port}/mcp")
        async with client(state) as http:
            result = await http.post(_URL, headers=_AUTH)
        assert len(requests) <= 1  # No automatic capture retry after possible persistence.
        return result, requests


async def test_capture_persists_exact_document_before_response_and_get_restores_offline(
    review_http,
):
    state = review_http
    result, upstream = await capture(state)
    assert result.status_code == 200 and len(upstream) == 1
    private(result)
    original = document(state.pages)
    assert result.json() == {"record": original.record(), "html": _HTML}
    assert row_count(state.pages) == 1 and state.writer.calls == 3
    assert state.calls[0][1:] == ("project", _ENDPOINT)
    assert all(principal is state.calls[0][0] for principal, _ in state.grants)
    with sqlite3.connect(state.pages.human.path) as db:
        stored = json.loads(
            db.execute("SELECT value_json FROM ontology_review_values").fetchone()[0]
        )
        assert stored["record"] == original.record() and stored["html"] == _HTML
        assert db.execute("SELECT DISTINCT session_id FROM mcp_audit_log").fetchall() == [(None,)]
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('agents','sessions','ontology_session_bindings')"
        ).fetchall()
    async with client(state) as http:
        restored = await http.get(_URL, headers=_AUTH)
    assert restored.status_code == 200 and restored.json() == result.json()
    private(restored)
    assert state.writer.calls == 3 and row_count(state.pages) == 1


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Authorization", "Bearer " + _TOKEN)] * 2,
        {"Authorization": "Basic private-marker"},
        {"Authorization": "Bearer invalid-private-marker"},
    ],
)
async def test_real_bearer_dependency_refuses_before_reader(review_http, headers):
    async with client(review_http) as http:
        result = await http.get(_URL, headers=headers)
    assert result.status_code == 401 and review_http.calls == []
    private(result)
    assert "private-marker" not in result.text and row_count(review_http.pages) == 0


@pytest.mark.parametrize(
    "verb,query,body",
    [
        ("GET", "?token=private-marker", None),
        ("POST", "?organization_id=foreign", None),
        ("GET", "", b'{"resource":"private-marker"}'),
        ("POST", "", b'{"access_token":"private-marker"}'),
        ("POST", "", b" "),
    ],
)
async def test_selectors_and_payloads_are_rejected_without_echo_or_io(
    review_http, verb, query, body
):
    async with client(review_http) as http:
        result = await http.request(verb, _URL + query, headers=_AUTH, content=body)
    assert result.status_code == 400 and review_http.calls == []
    private(result)
    assert "private-marker" not in result.text and row_count(review_http.pages) == 0


async def test_cookie_or_query_credential_does_not_authenticate(review_http):
    async with client(review_http) as http:
        result = await http.get(_URL + "?token=" + _TOKEN, headers={"Cookie": "token=" + _TOKEN})
    assert result.status_code == 401 and review_http.calls == []
    private(result)


@pytest.mark.parametrize(
    "url",
    [
        _URL.replace("/project/", "/bad.project/"),
        _URL.replace("/project/", "/" + "a" * 129 + "/"),
        _URL.replace("a" * 32, "A" * 32),
        _URL.replace("a" * 32, "a" * 31),
    ],
)
async def test_malformed_original_selectors_refuse_before_resolver(review_http, url):
    async with client(review_http) as http:
        result = await http.get(url, headers=_AUTH)
    assert result.status_code == 400 and review_http.calls == []
    private(result)


@pytest.mark.parametrize(
    "verb,suffix,status",
    [
        ("PUT", "", 405),
        ("DELETE", "", 405),
        ("HEAD", "", 405),
        ("GET", "/comments", 404),
        ("GET", "/", 404),
    ],
)
async def test_only_exact_get_and_post_route_is_exposed(review_http, verb, suffix, status):
    async with client(review_http) as http:
        result = await http.request(verb, _URL + suffix, headers=_AUTH)
    assert result.status_code == status and review_http.calls == []


@pytest.mark.parametrize("slot", [0, 1, 2, 3, "requests", "missing"])
async def test_missing_or_replaced_mount_objects_refuse_before_auth_and_io(review_http, slot):
    state = review_http
    if slot == "requests":
        state.app.state.canonical_requests = object()
    elif slot == "missing":
        del state.app.state.canonical_human_review_ports
    else:
        changed = list(state.ports)
        changed[slot] = object()
        state.app.state.canonical_human_review_ports = tuple(changed)
    async with client(state) as http:
        result = await http.get(_URL, headers=_AUTH)
    assert result.status_code == 503 and state.calls == []
    private(result)


async def test_mount_withdrawal_during_resolver_wait_refuses_before_worker(review_http):
    state = review_http

    async def withdraw(principal):
        state.app.state.canonical_requests = object()

    state.on_resolve = withdraw
    result, upstream = await capture(state)
    assert result.status_code == 503 and upstream == [] and row_count(state.pages) == 0
    private(result)


async def test_reader_for_another_sealed_principal_is_not_accepted(review_http):
    review_http.reader_override = review_http.pages.reader
    result, upstream = await capture(review_http)
    assert result.status_code == 403 and upstream == []
    private(result)


async def test_foreign_project_selector_does_not_release_another_project(review_http):
    async with client(review_http) as http:
        result = await http.get(_URL.replace("/project/", "/foreign/"), headers=_AUTH)
    assert result.status_code == 403 and row_count(review_http.pages) == 0
    private(result)


async def test_foreign_organization_reader_refuses_before_worker(review_http):
    def foreign(reader):
        reader._project = replace(reader._project, organization_id="foreign")

    review_http.after_reader = foreign
    result, upstream = await capture(review_http)
    assert result.status_code == 403 and upstream == []
    private(result)


async def test_initial_full_graph_denial_retains_forbidden_http_status(review_http):
    review_http.allowed = False
    result, upstream = await capture(review_http)
    assert result.status_code == 403 and upstream == []
    private(result)


async def test_real_member_revocation_during_resolver_wait_retains_authentication_status(
    review_http,
):
    state = review_http

    async def revoke(principal):
        with sqlite3.connect(state.pages.human.path) as db:
            db.execute(
                "UPDATE users SET status='disabled' WHERE id=?",
                (principal.identity.snapshot.member.owner_id,),
            )
        with pytest.raises(MossworkError) as caught:
            await principal.revalidate()
        state.current_auth_status = caught.value.status

    state.on_resolve = revoke
    result, upstream = await capture(state)
    assert state.current_auth_status in {401, 403}
    assert result.status_code == state.current_auth_status and upstream == []
    private(result)


@pytest.mark.parametrize("slot", [0, 1, 2, 3, "requests"])
async def test_post_commit_mount_withdrawal_releases_no_page_and_original_get_recovers(
    review_http, monkeypatch, slot
):
    state = review_http
    original = RetainedHumanReviewDocumentRepo.save

    def save_then_withdraw(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if slot == "requests":
            state.app.state.canonical_requests = object()
        else:
            changed = list(state.ports)
            changed[slot] = object()
            state.app.state.canonical_human_review_ports = tuple(changed)

    monkeypatch.setattr(RetainedHumanReviewDocumentRepo, "save", save_then_withdraw)
    result, upstream = await capture(state)
    assert result.status_code == 503 and len(upstream) == 1 and row_count(state.pages) == 1
    assert "html" not in result.json()
    private(result)
    state.app.state.canonical_requests = state.ports[1]
    state.app.state.canonical_human_review_ports = state.ports
    async with client(state) as http:
        recovered = await http.get(_URL, headers=_AUTH)
    assert recovered.status_code == 200 and recovered.json()["html"] == _HTML


async def test_grant_revocation_after_commit_releases_no_page(review_http, monkeypatch):
    original = RetainedHumanReviewDocumentRepo.save

    def revoke(self, *args, **kwargs):
        original(self, *args, **kwargs)
        review_http.allowed = False

    monkeypatch.setattr(RetainedHumanReviewDocumentRepo, "save", revoke)
    result, upstream = await capture(review_http)
    assert result.status_code == 403 and len(upstream) == 1 and row_count(review_http.pages) == 1
    assert "private-grant-marker" not in result.text and "html" not in result.json()
    private(result)


@pytest.mark.parametrize("fail_at,rows", [(1, 0), (2, 0), (3, 1)])
async def test_writer_withdrawal_preserves_commit_boundary_without_retry(
    review_http, fail_at, rows
):
    review_http.writer.fail_at = fail_at
    result, upstream = await capture(review_http)
    assert result.status_code == 503 and row_count(review_http.pages) == rows
    assert len(upstream) == (0 if fail_at == 1 else 1)
    private(result)


async def test_missing_store_never_releases_fetched_html(review_http):
    with sqlite3.connect(review_http.pages.human.path) as db:
        db.execute("DROP TABLE ontology_review_values")
    result, upstream = await capture(review_http)
    assert result.status_code == 503 and len(upstream) == 1
    assert "ontology_review_values" not in result.text and "html" not in result.json()
    private(result)


async def test_missing_get_does_not_capture_or_contact_worker(review_http):
    async with client(review_http) as http:
        result = await http.get(_URL, headers=_AUTH)
    assert result.status_code == 503 and row_count(review_http.pages) == 0
    assert review_http.writer.calls == 0
    private(result)


@pytest.mark.parametrize("field", ["html", "record", "account_id"])
async def test_get_rejects_tampered_original_bytes_instead_of_refetching(review_http, field):
    persist(review_http.pages)
    with sqlite3.connect(review_http.pages.human.path) as db:
        value = json.loads(
            db.execute("SELECT value_json FROM ontology_review_values").fetchone()[0]
        )
        value[field] = "private-tamper-marker"
        db.execute("UPDATE ontology_review_values SET value_json=?", (json.dumps(value),))
    async with client(review_http) as http:
        result = await http.get(_URL, headers=_AUTH)
    assert result.status_code == 503 and review_http.writer.calls == 0
    assert "private-tamper-marker" not in result.text
    private(result)


@pytest.mark.parametrize("field", ["binding", "endpoint"])
async def test_provider_document_must_match_exact_project_and_original_endpoint(
    review_http, monkeypatch, field
):
    original = document(review_http.pages)
    wrong = replace(
        original,
        **{
            field: ModelBinding("authority", "other", "project", ("source",))
            if field == "binding"
            else "/review/" + "d" * 32 + "/v1"
        },
    )

    async def substituted(self, *args, **kwargs):
        return wrong

    monkeypatch.setattr(HumanReviewReader, "capture_review", substituted)
    async with client(review_http) as http:
        result = await http.post(_URL, headers=_AUTH)
    assert result.status_code == 503 and "html" not in result.json()
    private(result)


@pytest.mark.parametrize(
    "failure,status",
    [
        (PermissionError("private-provider-marker"), 403),
        (RuntimeError("private-provider-marker"), 503),
        (
            MossworkError(
                ErrorCode.INTERNAL_ERROR,
                "private-provider-marker",
                503,
                {"secret": "private-provider-marker"},
            ),
            503,
        ),
    ],
)
async def test_provider_failures_are_sanitized(review_http, failure, status):
    review_http.provider_error = failure
    async with client(review_http) as http:
        result = await http.get(_URL, headers=_AUTH)
    assert result.status_code == status and "private-provider-marker" not in result.text
    private(result)


async def test_actual_cancel_after_original_commit_propagates_and_get_recovers(
    review_http, monkeypatch
):
    state = review_http
    original = RetainedHumanReviewDocumentRepo.save
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def commit_then_wait(self, *args, **kwargs):
        original(self, *args, **kwargs)
        entered.set()
        try:
            assert release.wait(5)
        finally:
            finished.set()

    monkeypatch.setattr(RetainedHumanReviewDocumentRepo, "save", commit_then_wait)
    task = asyncio.create_task(capture(state))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled() and row_count(state.pages) == 1
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    async with client(state) as http:
        restored = await http.get(_URL, headers=_AUTH)
    assert restored.status_code == 200 and restored.json()["html"] == _HTML
    assert state.writer.calls == 2


@pytest.fixture
def review_page_http(review_http):
    """Fixture provider only; actual sealed actor/SQL/domain/TCP reader reused."""
    state = review_http
    persist(state.pages)
    state.page_calls, state.after_page, state.before_page = [], None, None
    state.page_error, state.page_result = None, None

    async def read_page(principal, *, project_id, endpoint, documents, record_sha256, request):
        state.page_calls.append(
            (principal, project_id, endpoint, documents, record_sha256, request)
        )
        assert type(principal) is CanonicalAuthenticatedRequest
        assert documents is state.pages.store
        if state.before_page:
            await state.before_page(principal)
        if state.page_error:
            raise state.page_error
        if state.page_result is not None:
            await principal.revalidate()
            return state.page_result
        reader = await state.ports[0](principal, project_id=project_id, endpoint=endpoint)
        original = await reader.restore_review(endpoint, documents)
        if record_sha256 != original.record()["record_sha256"]:
            raise SudographError("CONTEXT_STALE", "private-pointer-detail", "private-recovery")
        selected = review_page_request(original, request)
        result = await reader.review(selected)
        if state.after_page:
            await state.after_page(principal)
        return result

    state.ports[0].read_page = read_page
    return state


def page_body():
    return {
        "view": "project",
        "operation": "page",
        "params": {**_POINTER, "id": "sg-source/source/object/widgets", "offset": 0, "limit": 50},
    }


def page_headers(state):
    return {**_AUTH, "If-Match": '"' + document(state.pages).record()["record_sha256"] + '"'}


async def page_post(state, **kwargs):
    async with client(state) as http:
        return await http.post(
            _URL + "/page", headers=kwargs.pop("headers", page_headers(state)), **kwargs
        )


async def test_page_reads_same_persisted_pointer_through_actual_review_transport_once(
    review_page_http,
):
    state = review_page_http
    data = {**_POINTER, "rows": [[1, "one"], [2, None], [3, "three"]], "has_more": False}
    payload = response(data)
    async with server(
        lambda _: (200, {"Content-Type": "application/json"}, json.dumps(payload).encode())
    ) as (port, upstream):
        review_row(state.pages.human, f"http://127.0.0.1:{port}/mcp")
        # The original document-only fixture descriptor takes no arguments.
        # Approve the actual page argument schema for this new transport test.
        # Production validation and the original document tests stay intact.
        with sqlite3.connect(state.pages.human.path) as db:
            reviewed = json.loads(
                db.execute("SELECT document FROM agent_tool_reviews").fetchone()[0]
            )
            reviewed["tool"]["inputSchema"] = {
                "type": "object",
                "properties": {
                    "revision": {"type": "string"},
                    "context_digest": {"type": "string"},
                    "id": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "records": {"type": "boolean"},
                },
                "required": ["revision", "context_digest", "id"],
                "additionalProperties": False,
            }
            reviewed["digest"] = content_digest(
                {"protocol": reviewed["protocol"], "tool": reviewed["tool"]}
            )
            db.execute("UPDATE agent_tool_reviews SET document=?", (json.dumps(reviewed),))
        result = await page_post(state, json=page_body())
    assert result.status_code == 200 and result.json() == {"result": data}
    private(result)
    assert len(upstream) == 1 and len(state.page_calls) == 1
    sent = json.loads(upstream[0]["body"])
    assert sent == {"operation": "page", "params": page_body()["params"], "resource": "project"}
    principal, project, endpoint, store, digest, original = state.page_calls[0]
    assert principal is state.calls[0][0] and principal is not state.pages.human.principal
    assert (project, endpoint, store, digest, original) == (
        "project",
        _ENDPOINT,
        state.pages.store,
        document(state.pages).record()["record_sha256"],
        page_body(),
    )
    assert row_count(state.pages) == 1 and state.writer.calls == 0
    with sqlite3.connect(state.pages.human.path) as db:
        assert db.execute("SELECT DISTINCT session_id FROM mcp_audit_log").fetchall() == [(None,)]
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('agents','sessions','ontology_session_bindings')"
        ).fetchall()


@pytest.mark.parametrize(
    "match",
    [None, "*", "a" * 64, 'W/"' + "a" * 64 + '"', '"' + "A" * 64 + '"', '"' + "a" * 63 + '"'],
)
async def test_page_requires_single_strong_original_record_digest(review_page_http, match):
    headers = dict(_AUTH)
    if match is not None:
        headers["If-Match"] = match
    result = await page_post(review_page_http, headers=headers, json=page_body())
    assert result.status_code == 400 and review_page_http.page_calls == []
    private(result)


async def test_page_duplicate_match_or_bearer_refuses_before_provider(review_page_http):
    for duplicate, expected in [("If-Match", 400), ("Authorization", 401)]:
        headers = list(page_headers(review_page_http).items())
        headers.append((duplicate, page_headers(review_page_http)[duplicate]))
        result = await page_post(review_page_http, headers=headers, json=page_body())
        assert result.status_code == expected and review_page_http.page_calls == []
        private(result)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"[]",
        b"null",
        b'{"operation":"page","operation":"head"}',
        b'{"params":{"id":"a","id":"b"}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b"\xff",
        b"{" + b" " * 65536 + b"}",
    ],
    ids=[
        "empty",
        "array",
        "null",
        "duplicate-outer",
        "duplicate-inner",
        "nan",
        "infinity",
        "non-utf8",
        "over-sdk-limit",
    ],
)
async def test_page_bounded_strict_json_refuses_before_provider(review_page_http, body):
    result = await page_post(
        review_page_http,
        headers={**page_headers(review_page_http), "Content-Type": "application/json"},
        content=body,
    )
    assert result.status_code == 400 and review_page_http.page_calls == []
    private(result)


async def test_page_mime_query_and_foreign_path_rejected_before_provider(review_page_http):
    state = review_page_http
    async with client(state) as http:
        for url, headers in [
            (_URL + "/page", {**page_headers(state), "Content-Type": "text/plain"}),
            (_URL + "/page?token=private-selector", page_headers(state)),
            (_URL.replace("a" * 32, "not-a-review") + "/page", page_headers(state)),
        ]:
            result = await http.post(url, headers=headers, json=page_body())
            assert result.status_code == 400 and state.page_calls == []
            private(result)


async def test_page_no_capability_is_unavailable_without_document_resolver_fallback(review_http):
    result = await page_post(review_http, json=page_body())
    assert result.status_code == 503 and review_http.calls == []
    private(result)


async def test_page_original_record_mismatch_is_conflict_without_worker_call(review_page_http):
    result = await page_post(
        review_page_http, headers={**_AUTH, "If-Match": '"' + "f" * 64 + '"'}, json=page_body()
    )
    assert result.status_code == 409 and len(review_page_http.page_calls) == 1
    assert "private-pointer-detail" not in result.text
    private(result)


@pytest.mark.parametrize(
    "key,value",
    [
        ("view", "model"),
        ("operation", "head"),
        ("endpoint", "/foreign"),
        ("actor", "foreign-member"),
    ],
)
async def test_page_actual_domain_validator_refuses_frame_retarget(review_page_http, key, value):
    body = page_body()
    body[key] = value
    result = await page_post(review_page_http, json=body)
    assert result.status_code == 400 and len(review_page_http.page_calls) == 1
    private(result)


@pytest.mark.parametrize(
    "code,status",
    [
        ("ACCESS_DENIED", 403),
        ("CONTEXT_STALE", 409),
        ("REVISION_UNKNOWN", 409),
        ("READ_CONTRACT", 400),
        ("AUDIT_UNAVAILABLE", 503),
    ],
)
async def test_page_existing_domain_outcomes_are_sanitized_without_retry(
    review_page_http, code, status
):
    state = review_page_http
    state.page_error = SudographError(code, "private-worker-path", "private-credential")
    result = await page_post(state, json=page_body())
    assert result.status_code == status and len(state.page_calls) == 1
    assert "private-worker-path" not in result.text and "private-credential" not in result.text
    private(result)


@pytest.mark.parametrize("slot", [0, 1, 2, 3, "requests"])
async def test_page_mount_withdrawal_during_provider_wait_releases_no_rows(review_page_http, slot):
    state = review_page_http

    async def withdraw(principal):
        if slot == "requests":
            state.app.state.canonical_requests = object()
        else:
            changed = list(state.ports)
            changed[slot] = object()
            state.app.state.canonical_human_review_ports = tuple(changed)

    state.before_page = withdraw
    state.page_result = {"result": {"rows": ["private-record"]}}
    result = await page_post(state, json=page_body())
    assert result.status_code == 503 and "private-record" not in result.text
    assert len(state.page_calls) == 1
    private(result)


async def test_page_real_member_revoke_after_provider_wait_preserves_auth_failure(review_page_http):
    state = review_page_http

    async def revoke(principal):
        with sqlite3.connect(state.pages.human.path) as db:
            db.execute(
                "UPDATE users SET status='disabled' WHERE id=?",
                (principal.identity.snapshot.member.owner_id,),
            )
        with pytest.raises(MossworkError) as caught:
            await principal.revalidate()
        state.expected_auth_status = caught.value.status

    state.before_page = revoke
    state.page_result = {"result": {"rows": ["private-record"]}}
    result = await page_post(state, json=page_body())
    assert result.status_code == state.expected_auth_status and result.status_code in {401, 403}
    assert "private-record" not in result.text and len(state.page_calls) == 1
    private(result)


async def test_page_real_task_cancellation_never_retries_or_releases_rows(review_page_http):
    state = review_page_http
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait(principal):
        entered.set()
        await release.wait()

    state.before_page = wait
    task = asyncio.create_task(page_post(state, json=page_body()))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled() and len(state.page_calls) == 1 and state.calls == []


async def test_page_has_no_unrelated_await_after_final_provider_admission(
    review_page_http, monkeypatch
):
    state = review_page_http
    original = CanonicalAuthenticatedRequest.revalidate
    finished = False

    async def current(principal):
        assert not finished, "A later await can stale the provider's final resource check"
        return await original(principal)

    async def final_provider(principal, **kwargs):
        nonlocal finished
        await principal.revalidate()
        finished = True
        return {"result": {"rows": []}}

    monkeypatch.setattr(CanonicalAuthenticatedRequest, "revalidate", current)
    state.ports[0].read_page = final_provider
    result = await page_post(state, json=page_body())
    assert result.status_code == 200 and result.json() == {"result": {"rows": []}} and finished
    private(result)


@pytest.mark.parametrize("value", [False, [], {}, {"rows": ["private-record"]}])
async def test_page_malformed_provider_completion_is_unavailable_without_data(
    review_page_http, value
):
    review_page_http.page_result = value
    result = await page_post(review_page_http, json=page_body())
    assert result.status_code == 503 and "private-record" not in result.text
    assert len(review_page_http.page_calls) == 1
    private(result)
