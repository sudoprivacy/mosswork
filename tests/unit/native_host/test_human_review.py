"""Real sealed JWT actor/SQLite/HTTP human review; explicit graph/writer fixtures."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.connectors.test_fixed_json import server
from tests.unit.identity.test_canonical import _FROZEN_SCHEMA

from octop.infra.connectors.fixed_json import FixedJsonTransport
from octop.infra.connectors.registered_model import RegisteredModelTransport
from octop.infra.connectors.tool_protocol import content_digest
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.canonical_identity import CanonicalIdentityProjectionRepo
from octop.infra.db.repos.retained_mcp import RetainedReviewedMcpRepo
from octop.infra.db.repos.retained_model_audit import RetainedModelAuditRepo
from octop.infra.db.repos.retained_ontology import RetainedModelInvalid, RetainedModelUnavailable
from octop.infra.db.repos.retained_ontology_project import RetainedOntologyProjectRepo
from octop.infra.errors import MossworkError
from octop.infra.identity.canonical import CanonicalSnapshotService, SqliteCanonicalSnapshotStore
from octop.infra.identity.human_tokens import CanonicalHumanTokenVerifier
from octop.infra.identity.projection import CanonicalRequestIdentityResolver
from octop.infra.identity.request_principal import CanonicalAuthenticatedRequest, authenticate
from octop.infra.native_host.human_review import HumanReviewReader, HumanReviewResolvers
from octop.infra.sudograph.protocol import REVIEW_PROFILE, ModelBinding, SudographError

_FIXTURE = json.loads(
    (Path(__file__).parents[1] / "identity/fixtures/human_tokens_legacy.json").read_text(
        encoding="utf-8"
    )
)
_IDS = _FIXTURE["ids"]
_ENDPOINT = "/review/" + "a" * 32 + "/v1"
_POINTER = {"revision": "b" * 64, "context_digest": "c" * 64}


def change(path, sql, parameters=()):
    with sqlite3.connect(path) as db:
        db.execute(sql, parameters)


@pytest.fixture
async def human(tmp_path):
    path = tmp_path / "canonical.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(_FROZEN_SCHEMA + "ALTER TABLE users ADD COLUMN phone TEXT;")
        db.execute("INSERT INTO organizations VALUES (?, 'Personal', 1)", (_IDS["homeOrg"],))
        db.execute(
            "INSERT INTO users(id,org_id,email,name,role,status,created_at,phone) "
            "VALUES (?,?,'local@example.test','human','admin','active',1,'13900000001')",
            (_IDS["homeMember"], _IDS["homeOrg"]),
        )
        db.execute(
            "INSERT INTO human_accounts VALUES (?, '13900000001','active',1)", (_IDS["account"],)
        )
        db.execute(
            "INSERT INTO human_account_memberships VALUES (?,?,?,1)",
            (_IDS["account"], _IDS["homeOrg"], _IDS["homeMember"]),
        )
        fixture = Path(__file__).parents[1] / "db/fixtures"
        # Only the original project CREATE/INDEX; no native/session table exists.
        db.executescript(
            (fixture / "retained_ontology_103.sql")
            .read_text()
            .split("CREATE TABLE IF NOT EXISTS ontology_session_bindings")[0]
        )
        db.executescript((fixture / "retained_mcp_103.sql").read_text())
        db.executescript((fixture / "retained_mcp_audit_103.sql").read_text())
        db.execute(
            "INSERT INTO ontology_projects VALUES ('project',?,'authority','worker','model',"
            "'[\"source\"]',1,1,1)",
            (_IDS["homeOrg"],),
        )
        db.execute(
            "INSERT INTO mcp_servers(id,org_id,name,scope,owner_type,owner_id,mcp_type,url,"
            "status,created_by,created_at,updated_at) VALUES "
            "('worker',?,'worker','org','system',?,'http','http://127.0.0.1:1/mcp',"
            "'enabled','fixture-admin',1,1)",
            (_IDS["homeOrg"], _IDS["homeOrg"]),
        )
        db.execute(
            "INSERT INTO mcp_policies(id,org_id,allow_http_sse_mcp,allow_external_network,"
            "domain_whitelist_json,created_by,created_at,updated_at) "
            "VALUES ('policy',?,1,1,'[\"127.0.0.1\"]','fixture-admin',1,1)",
            (_IDS["homeOrg"],),
        )
    product = SqlitePool(tmp_path / "separate-product.sqlite")
    run_migrations(product)
    now = _FIXTURE["verificationTime"]
    snapshots = CanonicalSnapshotService(
        SqliteCanonicalSnapshotStore(path, authority_id="retained-moss"),
        CanonicalHumanTokenVerifier(
            path,
            authority_id="retained-moss",
            secret=_FIXTURE["dummySecret"],
            issuer=_FIXTURE["issuer"],
            expected_audience=None,
            clock=lambda: now,
        ),
        clock=lambda: now,
    )
    token = next(item["token"] for item in _FIXTURE["cases"] if item["name"] == "sms_home")
    principal = await authenticate(
        CanonicalRequestIdentityResolver(snapshots, CanonicalIdentityProjectionRepo(product)),
        token,
    )
    projects = RetainedOntologyProjectRepo(path)
    project = projects.get(organization_id=_IDS["homeOrg"], project_id="project")
    state = SimpleNamespace(
        path=path,
        principal=principal,
        projects=projects,
        project=project,
        allowed=True,
        verdict=None,
        requests=[],
        audits=[],
        grant_requests=[],
    )

    async def grant(actual_principal, actual_project, model, *, operation, request):
        assert actual_principal is principal
        assert actual_project == project
        assert model == ModelBinding("authority", "model", "project", ("source",))
        assert operation == "review"
        state.grant_requests.append(copy.deepcopy(request))
        if not state.allowed:
            raise PermissionError("fixture independent graph deny")
        return state.verdict

    async def audit(actual_principal, actual_project, model, status):
        assert actual_principal is principal and actual_project == project
        actor = principal.identity.snapshot.member.owner_id
        state.audits.append(status)
        await asyncio.to_thread(
            RetainedModelAuditRepo(path).record_project_access,
            project,
            actor_id=actor,
            operation="review",
            status=status,
        )

    state.grant, state.audit = grant, audit
    try:
        yield state
    finally:
        product.close()


def review_row(human, endpoint):
    change(human.path, "UPDATE mcp_servers SET url=?", (endpoint,))
    tool = {
        "name": "sudograph_context_read",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "_meta": {"moss/host-review-profile": REVIEW_PROFILE},
    }
    review = {
        "protocol": "2025-03-26",
        "tool": tool,
        "digest": content_digest({"protocol": "2025-03-26", "tool": tool}),
        "orgId": _IDS["homeOrg"],
        "serverId": "worker",
        "resource": "mcp:worker/sudograph_context_read",
        "serverDigest": content_digest(
            {
                "id": "worker",
                "org": _IDS["homeOrg"],
                "type": "http",
                "endpoint": endpoint,
                "scope": "org",
                "owner": _IDS["homeOrg"],
            }
        ),
        "action": "read",
        "sensitive": False,
        "outbound": False,
        "enabled": True,
        "revision": "review-original",
        "reviewedBy": "fixture-admin",
        "reviewedAt": 1,
    }
    change(
        human.path,
        "INSERT OR REPLACE INTO agent_tool_reviews VALUES (?,?,?,?,?)",
        (_IDS["homeOrg"], review["resource"], "worker", "review-original", json.dumps(review)),
    )


def compose(human, *, grant=None, audit=None):
    resolvers = HumanReviewResolvers(
        principal=human.principal,
        projects=human.projects,
        reviews=RetainedReviewedMcpRepo(human.path),
        require_review_access=grant or human.grant,
        allowed_addresses=("127.0.0.1",),
    )
    transport = RegisteredModelTransport(
        resolve_tool=resolvers.resolve_tool,
        resolve_review=resolvers.resolve_review,
    )
    reader = HumanReviewReader(
        principal=human.principal,
        project=human.project,
        projects=human.projects,
        require_review_access=grant or human.grant,
        transport=transport,
        audit_review=audit or human.audit,
    )
    return resolvers, reader


def response(result):
    return {
        "format": REVIEW_PROFILE,
        "authority_ref": "authority",
        "model_ref": "model",
        "resource_ref": "project",
        "source_refs": ["source"],
        "scope": "full-registered-model",
        "result": result,
    }


async def test_human_document_uses_real_principal_http_and_null_session_audit(human):
    html = "<html><body>unchanged compiler HTML 🐈</body></html>"
    payload = response({**_POINTER, "html": html})
    async with server(
        lambda _: (200, {"Content-Type": "application/json"}, json.dumps(payload).encode())
    ) as (port, requests):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        _, reader = compose(human)
        document = await reader.review_document(_ENDPOINT)
    assert document.html == html and document.pointer.revision == _POINTER["revision"]
    assert document.pointer.context_digest == _POINTER["context_digest"]
    assert len(requests) == 1
    assert json.loads(requests[0]["body"]) == {
        "operation": "document",
        "endpoint": _ENDPOINT,
        "resource": "project",
    }
    with sqlite3.connect(human.path) as db:
        assert db.execute(
            "SELECT session_id,user_id,org_id,action,tool_name,status FROM mcp_audit_log"
        ).fetchall() == [
            (
                None,
                _IDS["homeMember"],
                _IDS["homeOrg"],
                "ontology.review.read",
                "sudograph_context_read",
                "success",
            )
        ]
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('sessions','ontology_session_bindings','agents')"
        ).fetchall()
    assert _IDS["account"] != _IDS["homeMember"]


@pytest.mark.parametrize(
    "verdict", [False, True, {"allowed": True}, {"llm_key": "not-a-graph-grant"}]
)
async def test_malformed_full_graph_grant_never_releases_or_sends(human, verdict):
    human.verdict = verdict
    async with server(lambda _: pytest.fail("No request may be sent")) as (port, requests):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        _, reader = compose(human)
        with pytest.raises(SudographError, match="denied"):
            await reader.review({"operation": "head"})
        assert requests == []
    assert human.audits == ["error"]


@pytest.mark.parametrize(
    "operation",
    [
        "sudograph_context",
        "sudograph_context_read",
        "sudograph_model_contract",
        "sudograph_model_apply",
        "review",
    ],
)
async def test_every_tool_entry_refuses_without_session_or_worker(human, operation):
    resolvers, _ = compose(human)
    with pytest.raises(PermissionError):
        await resolvers.resolve_tool(human.project, operation)
    assert human.grant_requests == human.audits == []


@pytest.mark.parametrize("mutation", ["generation", "source", "disabled", "member"])
async def test_current_dispatch_closure_rechecks_project_and_actor(human, mutation):
    review_row(human, "http://127.0.0.1:1/mcp")
    resolvers, _ = compose(human)
    _, current = await resolvers.resolve_review(
        human.project, {"resource": "project", "operation": "head"}
    )
    sql = {
        "generation": "UPDATE ontology_projects SET generation=generation+1",
        "source": "UPDATE ontology_projects SET source_refs_json='[\"other\"]'",
        "disabled": "UPDATE ontology_projects SET enabled=0",
        "member": "UPDATE users SET status='disabled'",
    }[mutation]
    change(human.path, sql)
    with pytest.raises((RetainedModelInvalid, RetainedModelUnavailable, MossworkError)):
        await current()


async def test_revoke_after_actual_http_refuses_data_but_keeps_error_audit(human):
    def respond(_):
        human.allowed = False
        return (
            200,
            {"Content-Type": "application/json"},
            json.dumps(response({"head": "value"})).encode(),
        )

    async with server(respond) as (port, requests):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        _, reader = compose(human)
        with pytest.raises(SudographError):
            await reader.review({"operation": "head"})
        assert len(requests) == 1
    assert human.audits == ["error"]


async def test_withdrawal_during_audit_wait_refuses_result(human):
    async def audit(*args):
        await human.audit(*args)
        human.allowed = False

    async with server(
        lambda _: (
            200,
            {"Content-Type": "application/json"},
            json.dumps(response({"head": "value"})).encode(),
        )
    ) as (
        port,
        requests,
    ):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        _, reader = compose(human, audit=audit)
        with pytest.raises(SudographError) as caught:
            await reader.review({"operation": "head"})
        assert caught.value.code == "AUDIT_UNAVAILABLE"
        assert len(requests) == 1


@pytest.mark.parametrize("kind", ["raises", "boolean"])
async def test_audit_failure_never_releases_data(human, kind):
    async def audit(*args):
        if kind == "raises":
            raise RuntimeError("PRIVATE WRITER CREDENTIAL")
        return True

    async with server(
        lambda _: (
            200,
            {"Content-Type": "application/json"},
            json.dumps(response({"head": "value"})).encode(),
        )
    ) as (
        port,
        requests,
    ):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        _, reader = compose(human, audit=audit)
        with pytest.raises(SudographError) as caught:
            await reader.review({"operation": "head"})
        assert caught.value.code == "AUDIT_UNAVAILABLE" and "PRIVATE" not in str(caught.value)
        assert len(requests) == 1


async def test_cancellation_and_audit_failure_stays_real_task_cancelled(human):
    entered = asyncio.Event()

    async def wait_grant(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def fail_audit(*args):
        raise RuntimeError("PRIVATE")

    _, reader = compose(human, grant=wait_grant, audit=fail_audit)
    task = asyncio.create_task(reader.review({"operation": "head"}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert task.cancelled()
    assert caught.value.__notes__ == ["AUDIT_UNAVAILABLE: canonical model audit did not complete"]


async def test_preawait_request_snapshot_and_foreign_project_refusal(human):
    review_row(human, "http://127.0.0.1:1/mcp")
    entered, release = asyncio.Event(), asyncio.Event()

    async def grant(*args, **kwargs):
        await human.grant(*args, **kwargs)
        entered.set()
        await release.wait()

    resolvers, _ = compose(human, grant=grant)
    request = {"resource": "project", "operation": "head"}
    task = asyncio.create_task(resolvers.resolve_review(human.project, request))
    await entered.wait()
    request["operation"] = "page"
    request["params"] = {"limit": 200}
    release.set()
    _, current = await task
    await current()
    assert all(item["operation"] == "head" for item in human.grant_requests)
    with pytest.raises(PermissionError):
        await resolvers.resolve_review(
            replace(human.project, organization_id="foreign"),
            {"resource": "project", "operation": "head"},
        )


@pytest.mark.parametrize(
    "fake", [None, {}, "actor-id", object.__new__(CanonicalAuthenticatedRequest)]
)
def test_unsealed_principal_never_becomes_a_human_reader(human, fake):
    with pytest.raises((PermissionError, MossworkError)):
        HumanReviewResolvers(
            principal=fake,
            projects=human.projects,
            reviews=RetainedReviewedMcpRepo(human.path),
            require_review_access=human.grant,
        )


async def test_revocation_after_address_selection_sends_zero_http_bytes(human, monkeypatch):
    original = FixedJsonTransport._selected_address

    async def selected(transport):
        address = await original(transport)
        human.allowed = False
        return address

    monkeypatch.setattr(FixedJsonTransport, "_selected_address", selected)
    async with server(lambda _: pytest.fail("Pre-byte grant must refuse")) as (port, requests):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        _, reader = compose(human)
        with pytest.raises(SudographError):
            await reader.review({"operation": "head"})
        assert requests == []
    assert human.audits == ["error"]


async def test_actual_member_revocation_after_read_still_audits_original_actor(human):
    def respond(_):
        change(human.path, "UPDATE users SET status='disabled'")
        return (
            200,
            {"Content-Type": "application/json"},
            json.dumps(response({"head": "value"})).encode(),
        )

    async with server(respond) as (port, requests):
        review_row(human, f"http://127.0.0.1:{port}/mcp")
        _, reader = compose(human)
        with pytest.raises(SudographError):
            await reader.review({"operation": "head"})
        assert len(requests) == 1
    with sqlite3.connect(human.path) as db:
        assert db.execute("SELECT user_id,session_id,status FROM mcp_audit_log").fetchall() == [
            (_IDS["homeMember"], None, "error")
        ]


async def test_current_secret_identity_rotation_invalidates_human_dispatch(human):
    review_row(human, "http://127.0.0.1:1/mcp")
    change(human.path, "UPDATE mcp_servers SET auth_type='secret_ref',secret_ref='worker-secret'")
    change(
        human.path,
        "INSERT INTO config_items(id,name,pinyin,scope,scheme,status,org_id,created_at,updated_at) VALUES (1,'worker','worker-secret','system','bearer',1,?,1,1)",
        (_IDS["homeOrg"],),
    )
    change(
        human.path,
        "INSERT INTO config_entries(config_item_id,config_key,name,created_at,updated_at) VALUES (1,'token','token',1,1)",
    )
    current_token = "synthetic-original"
    calls = []

    async def secrets(namespace, subject):
        calls.append((namespace, subject))
        return [{"key": "token", "value": current_token}]

    resolver = HumanReviewResolvers(
        principal=human.principal,
        projects=human.projects,
        reviews=RetainedReviewedMcpRepo(human.path),
        require_review_access=human.grant,
        list_secrets=secrets,
        allowed_addresses=("127.0.0.1",),
    )
    connection, current = await resolver.resolve_review(
        human.project, {"resource": "project", "operation": "head"}
    )
    assert connection.headers == (("authorization", "Bearer synthetic-original"),)
    assert set(calls) == {
        (f"org:{_IDS['homeOrg']}:system:worker-secret", f"system:moss:{_IDS['homeOrg']}")
    }
    current_token = "synthetic-rotated"
    with pytest.raises(PermissionError):
        await current()


@pytest.mark.parametrize(
    "metadata",
    [
        {"operation": "sudograph_model_apply"},
        {"status": "delivered"},
        {"actor_id": ""},
        {"status": True},
    ],
)
def test_project_audit_rejects_nonread_or_malformed_metadata(human, metadata):
    parameters = {
        "actor_id": _IDS["homeMember"],
        "operation": "review",
        "status": "success",
        **metadata,
    }
    with pytest.raises(ValueError):
        RetainedModelAuditRepo(human.path).record_project_access(human.project, **parameters)
    with sqlite3.connect(human.path) as db:
        assert db.execute("SELECT COUNT(*) FROM mcp_audit_log").fetchone()[0] == 0


async def test_independent_full_graph_deny_stays_denied_for_real_current_admin(human):
    assert human.principal.identity.snapshot.member.role == "admin"
    human.allowed = False
    _, reader = compose(human)
    with pytest.raises(SudographError) as caught:
        await reader.review({"operation": "head"})
    assert caught.value.code == "ACCESS_DENIED"
    assert human.audits == ["error"]


def test_foreign_project_cannot_enter_the_original_human_audit_scope(human):
    resolvers, _ = compose(human)
    transport = RegisteredModelTransport(
        resolve_tool=resolvers.resolve_tool, resolve_review=resolvers.resolve_review
    )
    with pytest.raises(PermissionError):
        HumanReviewReader(
            principal=human.principal,
            project=replace(human.project, organization_id="foreign-org"),
            projects=human.projects,
            require_review_access=human.grant,
            transport=transport,
            audit_review=human.audit,
        )
    assert human.audits == []
