"""Original SQLite reviewed MCP resolution with explicit authority fixtures."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from tests.unit.db.test_retained_cohost import retained as retained
from tests.unit.db.test_retained_ontology import registered as registered

from octop.infra.connectors.tool_protocol import content_digest
from octop.infra.db.repos.retained_mcp import RetainedReviewedMcpRepo, ReviewedMcpUnavailable
from octop.infra.db.repos.retained_ontology import RetainedOntologyBindingRepo
from octop.infra.native_host.reviewed_mcp import ReviewedModelResolvers


def change(path: Path, sql: str, params: tuple = ()) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute(sql, params)
        db.commit()


def set_review(path: Path, name: str, *, action: str = "read", **updates) -> None:
    tool = {
        "name": name,
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "_meta": {"moss/host-review-profile": "sudograph-worker-review/v2"},
    }
    review = {
        "protocol": "2025-03-26",
        "tool": tool,
        "digest": content_digest({"protocol": "2025-03-26", "tool": tool}),
        "orgId": "org",
        "serverId": "worker",
        "resource": f"mcp:worker/{name}",
        "serverDigest": content_digest(
            {
                "id": "worker",
                "org": "org",
                "type": "http",
                "endpoint": "https://worker.example/mcp",
                "scope": "org",
                "owner": "org",
            }
        ),
        "action": action,
        "sensitive": False,
        "outbound": False,
        "enabled": True,
        "revision": "review-original",
        "reviewedBy": "original-admin",
        "reviewedAt": 1,
    }
    review.update(updates)
    change(
        path,
        "INSERT OR REPLACE INTO agent_tool_reviews VALUES (?,?,?,?,?)",
        ("org", f"mcp:worker/{name}", "worker", "review-original", json.dumps(review)),
    )


@pytest.fixture
def reviewed(registered: Path) -> Path:
    with closing(sqlite3.connect(registered)) as db:
        db.executescript(
            (Path(__file__).parents[1] / "db/fixtures/retained_mcp_103.sql").read_text()
        )
        db.execute("INSERT INTO departments VALUES ('root','org',NULL,'root',1,1)")
        db.execute("INSERT INTO departments VALUES ('child','org','root','child',1,1)")
        db.execute(
            "INSERT INTO users(id,org_id,email,name,department_id,role,status,created_at) VALUES ('actor','org','actor@example.test','actor','child','admin','active',1)"
        )
        db.execute("INSERT INTO human_accounts VALUES ('human-account',NULL,'active',1)")
        db.execute("INSERT INTO human_account_memberships VALUES ('human-account','org','actor',1)")
        db.execute("""INSERT INTO mcp_servers(id,org_id,name,scope,owner_type,owner_id,mcp_type,url,status,created_by,created_at,updated_at)
            VALUES ('worker','org','original worker','org','system','org','http','https://worker.example/mcp','enabled','original-admin',1,1)""")
        db.execute("""INSERT INTO mcp_policies(id,org_id,allow_http_sse_mcp,allow_external_network,domain_whitelist_json,require_confirmation_for_write,created_by,created_at,updated_at)
            VALUES ('policy','org',1,1,'["worker.example"]',0,'original-admin',1,1)""")
        db.execute(
            "INSERT INTO config_items(id,name,pinyin,scope,scheme,status,org_id,created_at,updated_at) VALUES (1,'worker','worker-secret','system','bearer',1,'org',1,1)"
        )
        db.execute(
            "INSERT INTO config_entries(config_item_id,config_key,name,created_at,updated_at) VALUES (1,'token','token',1,1)"
        )
        db.commit()
    for name in ("sudograph_context", "sudograph_context_read", "sudograph_model_contract"):
        set_review(registered, name)
    set_review(registered, "sudograph_model_apply", action="write")
    return registered


def original(path: Path):
    return RetainedOntologyBindingRepo(path).resolve_original(
        "app-original", organization_id="org", owner_id="owner"
    )


def read(path: Path):
    return RetainedReviewedMcpRepo(path).resolve("org", "actor", "worker", "sudograph_context")


def test_original_rows_read_only_and_actor_not_target(reviewed: Path) -> None:
    before = reviewed.read_bytes()
    row = read(reviewed)
    assert row.member_id == "actor" != original(reviewed).owner_id
    assert row.timeout_ms == 15000
    assert row.endpoint == "https://worker.example/mcp"
    assert RetainedReviewedMcpRepo(reviewed).assert_current(row) is None
    assert "actor" not in repr(row)
    assert reviewed.read_bytes() == before


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE mcp_servers SET enabled=0",
        "UPDATE mcp_servers SET status='disabled'",
        "UPDATE mcp_servers SET scope='user'",
        "UPDATE mcp_servers SET mcp_type='stdio'",
        "UPDATE mcp_servers SET use_proxy=1",
        "UPDATE mcp_servers SET allow_read=0",
        "UPDATE mcp_servers SET auth_type='oauth'",
        "UPDATE mcp_servers SET url='https://foreign.example/mcp'",
        "UPDATE mcp_servers SET bound_assistants='[]'",
        "UPDATE mcp_servers SET bound_skills='[]'",
        "UPDATE mcp_policies SET allow_http_sse_mcp=0",
        "UPDATE mcp_policies SET allow_external_network=0",
        "UPDATE mcp_policies SET domain_whitelist_json='[]'",
        "DELETE FROM mcp_policies",
        "UPDATE users SET status='disabled' WHERE id='actor'",
        "UPDATE human_accounts SET status='disabled'",
        "DELETE FROM human_account_memberships",
        "DELETE FROM agent_tool_reviews",
        "UPDATE agent_tool_reviews SET revision='changed'",
        "UPDATE agent_tool_reviews SET server_id='foreign'",
        "UPDATE mcp_servers SET visible_to='malformed'",
        "UPDATE mcp_servers SET visible_to='{\"user_ids\":[]}'",
        "UPDATE departments SET parent_id='child' WHERE id='root'",
    ],
)
def test_original_denials_and_malformed_rows_refuse(reviewed: Path, sql: str) -> None:
    change(reviewed, sql)
    with pytest.raises(ReviewedMcpUnavailable):
        read(reviewed)


@pytest.mark.parametrize(
    "visible,allowed",
    [
        ({"role_ids": ["admin"]}, True),
        ({"department_ids": ["root"]}, True),
        ({"user_ids": ["actor"]}, True),
        ({"user_ids": [], "role_ids": ["admin"]}, False),
        ({"role_ids": ["user"]}, False),
        ({"department_ids": []}, False),
        ({}, False),
    ],
)
def test_actual_visibility_order_without_admin_expansion(reviewed: Path, visible, allowed) -> None:
    change(reviewed, "UPDATE mcp_servers SET visible_to=?", (json.dumps(visible),))
    if allowed:
        assert read(reviewed).member_id == "actor"
    else:
        with pytest.raises(ReviewedMcpUnavailable):
            read(reviewed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("orgId", "foreign"),
        ("serverId", "foreign"),
        ("serverDigest", "sha256:wrong"),
        ("digest", "sha256:wrong"),
        ("enabled", False),
        ("action", "other"),
        ("sensitive", "false"),
    ],
)
def test_independent_review_document_must_match_original_rows(reviewed: Path, field, value) -> None:
    set_review(reviewed, "sudograph_context", **{field: value})
    with pytest.raises(ReviewedMcpUnavailable):
        read(reviewed)


class Grants:
    def __init__(self):
        self.allowed = True
        self.calls = []

    async def __call__(self, binding, operation):
        self.calls.append((binding, operation))
        if not self.allowed:
            raise PermissionError("private grant refusal")


def resolver(path: Path, grants: Grants, **kwargs):
    return ReviewedModelResolvers(
        reviews=RetainedReviewedMcpRepo(path),
        registry=RetainedOntologyBindingRepo(path),
        actor_member_id="actor",
        check_access=grants,
        **kwargs,
    )


@pytest.mark.parametrize(
    "operation",
    [
        "sudograph_context",
        "sudograph_context_read",
        "sudograph_model_contract",
        "sudograph_model_apply",
    ],
)
async def test_actual_resolver_tuple_and_post_wait_grant(reviewed: Path, operation) -> None:
    grants = Grants()
    connection, descriptor, current = await resolver(reviewed, grants).resolve_tool(
        original(reviewed), operation
    )
    assert descriptor.tool["name"] == operation
    assert connection.endpoint == "https://worker.example/mcp"
    assert await current() is None
    grants.allowed = False
    with pytest.raises(PermissionError):
        await current()


async def test_review_requires_original_profile_and_full_read_grant(reviewed: Path) -> None:
    grants = Grants()
    producer = resolver(reviewed, grants)
    connection, current = await producer.resolve_review(
        original(reviewed), {"resource": "project-original", "operation": "head"}
    )
    assert connection.endpoint == "https://worker.example/mcp"
    assert all(operation == "review" for _, operation in grants.calls)
    set_review(reviewed, "sudograph_context_read", tool={"name": "sudograph_context_read"})
    with pytest.raises(PermissionError):
        await current()


async def test_false_grant_completion_refuses_before_secret_access(reviewed: Path) -> None:
    async def malformed(*_):
        return False

    with pytest.raises(PermissionError):
        await resolver(reviewed, malformed).resolve_tool(original(reviewed), "sudograph_context")


async def test_row_rebind_after_resolution_refuses_current(reviewed: Path) -> None:
    _, _, current = await resolver(reviewed, Grants()).resolve_tool(
        original(reviewed), "sudograph_context"
    )
    change(reviewed, "UPDATE users SET membership_revision=1 WHERE id='actor'")
    with pytest.raises(PermissionError):
        await current()


async def test_inline_original_auth_and_rotation_fence(reviewed: Path) -> None:
    config = {"header_name": "Authorization", "prefix": "Bearer", "token": "original-private"}
    change(
        reviewed,
        "UPDATE mcp_servers SET auth_type='bearer',auth_config_json=?",
        (json.dumps(config),),
    )
    connection, _, current = await resolver(reviewed, Grants()).resolve_tool(
        original(reviewed), "sudograph_context"
    )
    assert connection.headers == (("authorization", "Bearer original-private"),)
    assert "original-private" not in repr(connection)
    config["token"] = "rotated-private"
    change(reviewed, "UPDATE mcp_servers SET auth_config_json=?", (json.dumps(config),))
    with pytest.raises(PermissionError):
        await current()


async def test_secret_ref_uses_original_system_namespace_and_detects_rotation(
    reviewed: Path,
) -> None:
    change(
        reviewed, "UPDATE mcp_servers SET auth_type='secret_ref',secret_ref='system:worker-secret'"
    )
    secret = "original-secret"
    calls = []

    async def secrets(namespace, subject):
        calls.append((namespace, subject))
        return [{"key": "token", "value": secret}]

    connection, _, current = await resolver(reviewed, Grants(), list_secrets=secrets).resolve_tool(
        original(reviewed), "sudograph_context"
    )
    assert connection.headers == (("authorization", "Bearer original-secret"),)
    assert set(calls) == {("org:org:system:worker-secret", "system:moss:org")}
    secret = "rotated-secret"
    with pytest.raises(PermissionError):
        await current()


async def test_secret_definition_change_during_read_refuses_and_no_fallback(reviewed: Path) -> None:
    change(reviewed, "UPDATE mcp_servers SET auth_type='secret_ref',secret_ref='worker-secret'")

    async def secrets(*_):
        change(reviewed, "UPDATE config_entries SET config_key='changed'")
        return [{"key": "token", "value": "secret"}]

    with pytest.raises(PermissionError):
        await resolver(reviewed, Grants(), list_secrets=secrets).resolve_tool(
            original(reviewed), "sudograph_context"
        )
    with pytest.raises(PermissionError):
        await resolver(reviewed, Grants()).resolve_tool(original(reviewed), "sudograph_context")


async def test_revocation_during_secret_wait_refuses_and_cancel_remains_cancelled(
    reviewed: Path,
) -> None:
    change(reviewed, "UPDATE mcp_servers SET auth_type='secret_ref',secret_ref='worker-secret'")
    grants = Grants()

    async def secrets(*_):
        grants.allowed = False
        return [{"key": "token", "value": "secret"}]

    with pytest.raises(PermissionError):
        await resolver(reviewed, grants, list_secrets=secrets).resolve_tool(
            original(reviewed), "sudograph_context"
        )
    entered = asyncio.Event()

    async def waiting(*_):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        resolver(reviewed, Grants(), list_secrets=waiting).resolve_tool(
            original(reviewed), "sudograph_context"
        )
    )
    await entered.wait()
    task.cancel("original cancellation")
    with pytest.raises(asyncio.CancelledError, match="original cancellation"):
        await task
    assert task.cancelled()


async def test_missing_actual_grant_refuses_before_any_secret_service(reviewed: Path) -> None:
    change(reviewed, "UPDATE mcp_servers SET auth_type='secret_ref',secret_ref='worker-secret'")
    grants = Grants()
    grants.allowed = False
    calls = []

    async def secrets(*args):
        calls.append(args)
        return [{"key": "token", "value": "secret"}]

    with pytest.raises(PermissionError):
        await resolver(reviewed, grants, list_secrets=secrets).resolve_tool(
            original(reviewed), "sudograph_context"
        )
    assert calls == []


@pytest.mark.parametrize(
    "operation,action", [("sudograph_context", "write"), ("sudograph_model_apply", "read")]
)
async def test_actual_operation_requires_independent_effect_classification(
    reviewed: Path, operation, action
) -> None:
    set_review(reviewed, operation, action=action)
    with pytest.raises(PermissionError):
        await resolver(reviewed, Grants()).resolve_tool(original(reviewed), operation)


@pytest.mark.parametrize("metadata", [None, ["sudograph-worker-review/v2"]])
async def test_malformed_human_review_metadata_refuses(reviewed: Path, metadata) -> None:
    tool = {"name": "sudograph_context_read", "_meta": metadata}
    set_review(
        reviewed,
        "sudograph_context_read",
        tool=tool,
        digest=content_digest({"protocol": "2025-03-26", "tool": tool}),
    )
    with pytest.raises(PermissionError):
        await resolver(reviewed, Grants()).resolve_review(
            original(reviewed), {"resource": "project-original", "operation": "head"}
        )


async def test_duplicate_secret_keys_refuse_even_if_first_is_missing(reviewed: Path) -> None:
    change(reviewed, "UPDATE mcp_servers SET auth_type='secret_ref',secret_ref='worker-secret'")

    async def secrets(*_):
        return [{"key": "token", "value": None}, {"key": "token", "value": "secret"}]

    with pytest.raises(PermissionError):
        await resolver(reviewed, Grants(), list_secrets=secrets).resolve_tool(
            original(reviewed), "sudograph_context"
        )


def test_absent_store_refuses_without_creating_a_file(tmp_path: Path) -> None:
    path = tmp_path / "missing-original.sqlite"
    with pytest.raises(PermissionError):
        RetainedReviewedMcpRepo(path).resolve("org", "actor", "worker", "sudograph_context")
    assert not path.exists()


@pytest.mark.parametrize(
    "org,actor,worker",
    [
        ("foreign", "actor", "worker"),
        ("org", "owner", "worker"),
        ("org", "actor", "worker' OR 1=1 --"),
    ],
)
def test_exact_original_org_actor_worker_selection(reviewed: Path, org, actor, worker) -> None:
    with pytest.raises(PermissionError):
        RetainedReviewedMcpRepo(reviewed).resolve(org, actor, worker, "sudograph_context")
