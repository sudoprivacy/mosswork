"""The same foreground tuple and middleware admit only the captured page route.

The leaf tests own JSON parsing. Graph/page/writer services here are fixtures;
this module does not qualify an operational provider or private browser frame.
"""

import json
import sqlite3
from dataclasses import replace

import pytest

from octop.infra.db.repos.retained_mcp import RetainedReviewedMcpRepo
from octop.infra.db.repos.retained_model_audit import RetainedModelAuditRepo
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.native_host.human_review_provider import CanonicalHumanReviewReaders
from octop.infra.sudograph.protocol import ModelBinding
from tests.unit.api.test_canonical_human_review import _AUTH, _URL
from tests.unit.api.test_canonical_human_review import review_http as review_http
from tests.unit.connectors.test_fixed_json import server as worker
from tests.unit.native_host.test_human_review import _POINTER, response
from tests.unit.native_host.test_human_review import human as human
from tests.unit.native_host.test_human_review_documents import _HTML, document, row_count
from tests.unit.native_host.test_human_review_documents import pages as pages
from tests.unit.native_host.test_human_review_pages import page_request, reply, reviewed_pages
from tests.unit.test_canonical_product_startup import _policy_foreground
from tests.unit.test_canonical_product_startup import configuration as configuration
from tests.unit.test_canonical_product_startup import environment as environment
from tests.unit.test_canonical_product_startup import isolated_boot as isolated_boot
from tests.unit.test_canonical_product_startup import legacy_db as legacy_db
from tests.unit.test_canonical_product_startup import product as product
from tests.unit.test_human_review_mount import client, selected


class PageResolver:
    def __init__(self, state):
        self.state = state
        self.calls = []
        self.after_wait = None

    async def __call__(self, principal, *, project_id, endpoint):
        return await self.state.ports[0](principal, project_id=project_id, endpoint=endpoint)

    async def read_page(
        self, principal, *, project_id, endpoint, documents, record_sha256, request
    ):
        assert project_id == "project" and endpoint == "/review/" + "a" * 32 + "/v1"
        assert documents._uri == self.state.pages.store._uri
        assert record_sha256 == document(self.state.pages).record()["record_sha256"]
        self.calls.append((principal, documents, request))
        await principal.revalidate()
        if self.after_wait:
            self.after_wait()
        return {"result": {"rows": [{"original": 1}, {"original": None}, {"original": 3}]}}


def headers(state):
    return {
        **_AUTH,
        "If-Match": '"' + document(state.pages).record()["record_sha256"] + '"',
    }


async def test_same_foreground_page_is_one_existing_mount_and_original_four_refs(
    configuration, review_http, isolated_boot, monkeypatch
):
    resolver = PageResolver(review_http)
    config = replace(selected(configuration, review_http), human_review_reader=resolver)

    async def exercise(app, server):
        bound = server.canonical
        ports = app.state.canonical_human_review_ports
        assert ports is bound.human_review_ports and ports[0] is resolver
        assert ports[1] is bound.business.requests and ports[3] is review_http.writer
        assert len(bound.business.app_kwargs()) == 11 and len(vars(bound.runtime_inputs)) == 5
        assert configuration.backend_factory.inputs == [bound.runtime_inputs]
        async with client(app) as http:
            docs = (await http.get("/api/openapi.json")).json()
            path = "/api/identity/ontology-projects/{project_id}/reviews/{review_id}/v1/page"
            assert set(docs["paths"][path]) == {"post"}
            operation = docs["paths"][path]["post"]
            assert operation["tags"] == ["auth"]
            body = operation["requestBody"]["content"]["application/json"]["schema"]
            assert body["required"] == ["view", "operation", "params"]
            assert body["properties"]["operation"]["enum"] == ["page"]
            match = next(p for p in operation["parameters"] if p["name"] == "If-Match")
            assert match["required"] is True and match["in"] == "header"
            assert (
                "result"
                in operation["responses"]["200"]["content"]["application/json"]["schema"][
                    "properties"
                ]
            )
            result = await http.post(
                _URL + "/page", headers=headers(review_http), json={"original": True}
            )
        assert result.status_code == 200 and result.json()["result"]["rows"][1] == {
            "original": None
        }
        assert result.headers["cache-control"] == "no-store"
        assert result.headers["x-content-type-options"] == "nosniff"
        assert len(resolver.calls) == 1 and resolver.calls[0][1] is ports[2]
        assert resolver.calls[0][0]._CanonicalAuthenticatedRequest__resolver is ports[1]
        assert review_http.calls == []

    await _policy_foreground(config, isolated_boot, monkeypatch, exercise)


@pytest.mark.parametrize("withdrawal,status", [("none", 200), ("graph", 403), ("member", 401)])
async def test_same_foreground_concrete_page_provider_preserves_rows_and_final_auth_status(
    configuration, review_http, isolated_boot, monkeypatch, withdrawal, status
):
    state = review_http
    original = state.pages.human
    held_requests, completed = [], []

    async def grant(principal, project, model, *, operation, request):
        assert principal._CanonicalAuthenticatedRequest__resolver is held_requests[0]
        assert (
            principal.identity.snapshot.member.owner_id
            == original.principal.identity.snapshot.member.owner_id
        )
        assert project == original.project and operation == "review"
        assert model == ModelBinding("authority", "model", "project", ("source",))
        if completed and withdrawal == "graph":
            raise MossworkError.localized(ErrorCode.FORBIDDEN, status=403)

    resolver = CanonicalHumanReviewReaders.with_canonical_audit(
        projects=original.projects,
        reviews=RetainedReviewedMcpRepo(original.path),
        audits=RetainedModelAuditRepo(original.path),
        writer=state.writer,
        require_review_access=grant,
        allowed_addresses=("127.0.0.1",),
    )

    def respond(request):
        body = json.loads(request["body"])
        if body["operation"] == "document":
            return (
                200,
                {"Content-Type": "application/json"},
                json.dumps(response({**_POINTER, "html": _HTML})).encode(),
            )
        value = reply(request)
        completed.append(True)
        if withdrawal == "member":
            with sqlite3.connect(original.path) as db:
                db.execute("UPDATE users SET status='disabled'")
        return value

    async with worker(respond) as (port, upstream):
        reviewed_pages(original, f"http://127.0.0.1:{port}/mcp")

        async def exercise(app, server):
            bound = server.canonical
            ports = app.state.canonical_human_review_ports
            assert ports is bound.human_review_ports and ports[0] is resolver
            assert ports[1] is bound.business.requests and ports[3] is state.writer
            assert len(bound.business.app_kwargs()) == 11 and len(vars(bound.runtime_inputs)) == 5
            assert configuration.backend_factory.inputs == [bound.runtime_inputs]
            held_requests.append(bound.business.requests)
            async with client(app) as http:
                capture = await http.post(_URL, headers=_AUTH)
                assert capture.status_code == 200 and capture.json()["html"] == _HTML
                digest = capture.json()["record"]["record_sha256"]
                result = await http.post(
                    _URL + "/page",
                    headers={**_AUTH, "If-Match": '"' + digest + '"'},
                    json=page_request(),
                )
            assert result.status_code == status and len(upstream) == 2 and len(completed) == 1
            assert result.headers["cache-control"] == "no-store"
            if status == 200:
                assert result.json()["result"]["rows"] == [[1, "row"], [2, None], [3, "third"]]
            else:
                assert "rows" not in result.text and _HTML not in result.text
            assert row_count(state.pages) == 1
            with sqlite3.connect(original.path) as db:
                assert db.execute("SELECT DISTINCT session_id FROM mcp_audit_log").fetchall() == [
                    (None,)
                ]
                assert not db.execute(
                    "SELECT name FROM sqlite_master WHERE name IN ('agents','sessions','ontology_session_bindings')"
                ).fetchall()

        config = replace(selected(configuration, state), human_review_reader=resolver)
        await _policy_foreground(config, isolated_boot, monkeypatch, exercise)


@pytest.mark.parametrize("configured", [False, True])
async def test_absent_page_capability_refuses_instead_of_using_document_resolver(
    configuration, review_http, isolated_boot, monkeypatch, configured
):
    async def exercise(app, server):
        async with client(app) as http:
            result = await http.post(_URL + "/page", headers=headers(review_http), json={})
        assert result.status_code == 503 and review_http.calls == []

    await _policy_foreground(
        selected(configuration, review_http, configured=configured),
        isolated_boot,
        monkeypatch,
        exercise,
    )


@pytest.mark.parametrize(
    "method,path",
    [(method, _URL + "/page") for method in ("GET", "PUT", "DELETE", "OPTIONS")]
    + [("POST", _URL + suffix) for suffix in ("/page/", "/pages", "/page/child")]
    + [("POST", _URL.replace("/project/", "/%70roject/") + "/page")],
)
async def test_adjacent_page_paths_do_not_enter_canonical_gate(
    configuration, review_http, isolated_boot, monkeypatch, method, path
):
    resolver = PageResolver(review_http)

    async def exercise(app, server):
        async with client(app) as http:
            result = await http.request(method, path, headers=headers(review_http), json={})
        assert result.status_code == 503 and resolver.calls == [] and review_http.calls == []

    config = replace(selected(configuration, review_http), human_review_reader=resolver)
    await _policy_foreground(config, isolated_boot, monkeypatch, exercise)


@pytest.mark.parametrize(
    "replacement", ["tuple", "requests", "snapshots", "configured", "after-wait"]
)
async def test_page_uses_exact_configured_instances_before_and_after_waits(
    configuration, review_http, isolated_boot, monkeypatch, replacement
):
    resolver = PageResolver(review_http)

    async def exercise(app, server):
        ports = app.state.canonical_human_review_ports
        if replacement == "tuple":
            app.state.canonical_human_review_ports = (*ports,)
        elif replacement == "requests":
            app.state.canonical_requests = object()
        elif replacement == "snapshots":
            app.state.canonical_identity = object()
        elif replacement == "configured":
            server.canonical = replace(server.canonical, human_review_ports=None)
        else:
            resolver.after_wait = lambda: setattr(app.state, "canonical_human_review_ports", None)
        async with client(app) as http:
            result = await http.post(_URL + "/page", headers=headers(review_http), json={})
        assert result.status_code == 503 and "rows" not in result.text
        assert len(resolver.calls) == (1 if replacement == "after-wait" else 0)

    config = replace(selected(configuration, review_http), human_review_reader=resolver)
    await _policy_foreground(config, isolated_boot, monkeypatch, exercise)
