"""Local market publication must lead to usable, version-locked private Agents."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from octop.infra.agents.experts.catalog import WORKSPACE_MANIFEST_PATH
from octop.infra.market.service import ResourceMarket, example_package
from octop.infra.market.skillhub_defaults import SNAPSHOT_ROOT
from octop.infra.users.identity import User
from tests.support.auth import create_user
from tests.unit.market.test_resource_market import release, rewrite


def publish(server, *, dependencies=True):
    services = server.services
    admin = services.user_repo.get_by_username("admin")
    market = ResourceMarket(services, User(admin.id, "admin", "admin", None))
    skill, skill_version = release(market, example_package("skill"))
    _, newer = release(
        market,
        rewrite(
            example_package("skill"),
            version="1.1.0",
            files={"references/new.md": b"NEW VERSION ONLY"},
        ),
        skill["resource_id"],
    )
    market.recommend(skill["resource_id"], newer["version_id"], skill_version["version_id"])
    expert, version = release(
        market,
        rewrite(
            example_package("expert"),
            updates={
                "dependencies": [{"resource_id": skill["resource_id"], "version": "1.0.0"}]
                if dependencies
                else []
            },
        ),
    )
    return market, skill, expert, version


async def test_published_expert_is_visible_and_installs_exact_dependencies(env):
    client, server, admin_auth = env
    peer = await create_user(client, admin_auth, username="consumer")
    market, skill, expert, version = publish(server)
    # Consumer has expert access, but no resource publishing permission.
    assert (await client.get("/api/market/resources", headers=peer)).status_code == 403
    listed = await client.get("/api/experts/market", headers=peer)
    assert listed.status_code == 200, listed.text
    assert listed.json()["can_create"]
    # The deployment also ships bundled experts. Select the test publication
    # by its immutable version id instead of assuming the catalog is empty.
    published = [x for x in listed.json()["items"] if x["version_id"] == version["version_id"]]
    assert len(published) == 1
    route = f"/api/experts/market/{version['version_id']}"
    detail = await client.get(route, headers=peer)
    assert detail.status_code == 200, detail.text
    assert {x["version"] for x in detail.json()["lock"]} == {"1.0.0"}
    # Recommendation changes do not change the selected release.
    _, newer_expert = release(
        market, rewrite(example_package("expert"), version="2.0.0"), expert["resource_id"]
    )
    market.recommend(expert["resource_id"], newer_expert["version_id"], version["version_id"])
    created = await client.post(
        route + "/install", headers=peer, json={"name": "From local market"}
    )
    assert created.status_code == 201, created.text
    agent_id = created.json()["agent_id"]
    registry = server.app_runtime.agent_registry
    workspace = registry._backend_workspace_for_row(registry.get_row(agent_id))
    assert "inspection plans" in await workspace.aread_text("SOUL.md")
    assert "name: inspection-checklist" in await workspace.aread_text(
        "skills/inspection-checklist/SKILL.md"
    )
    assert await workspace.aread_text("skills/inspection-checklist/references/new.md") is None
    assert (
        json.loads(await workspace.aread_text(WORKSPACE_MANIFEST_PATH))["id"]
        == "inspection-assistant"
    )
    binding = server.services.resource_market_repo.installation(agent_id)
    assert binding["version_id"] == version["version_id"]
    assert len(json.loads(binding["lock_json"])) == 2
    # Unlisting stops new consumption while preserving already installed instances.
    market.listing(expert["resource_id"], False)
    remaining = (await client.get("/api/experts/market", headers=peer)).json()["items"]
    assert all(item["resource_id"] != expert["resource_id"] for item in remaining)
    assert (
        await client.post(route + "/install", headers=peer, json={"name": "hidden"})
    ).status_code == 404
    assert (await client.get(f"/api/agents/{agent_id}", headers=peer)).status_code == 200
    await client.delete(f"/api/agents/{agent_id}", headers=peer)
    assert server.services.resource_market_repo.installation(agent_id) is None


async def test_skillhub_snapshot_expert_is_local_and_serves_its_icon(env, full_skillhub_snapshot):
    client, _server, auth = env
    index = json.loads((SNAPSHOT_ROOT / "index.json").read_text(encoding="utf-8"))
    expected = {}
    for kind, records in (("skill", index["skills"]), ("expert", index["skillsets"])):
        for record in records:
            if record.get("status") == "downloaded":
                # The existing seeder preserves the first resource for a slug.
                expected.setdefault(
                    record["local_slug"], f"skillhub-{kind}-{record['source_slug']}"
                )
    with _server.services.db.connect() as conn:
        resources = {
            row["slug"]: row["resource_id"]
            for row in conn.execute("SELECT slug,resource_id FROM market_resources")
        }
        versions = {
            row["resource_id"] for row in conn.execute("SELECT resource_id FROM market_versions")
        }
    assert resources == expected
    assert versions == set(expected.values())
    response = await client.get("/api/experts/market", headers=auth)
    assert response.status_code == 200, response.text
    item = next(
        item
        for item in response.json()["items"]
        if item["resource_id"] == "skillhub-expert-tech-test-automation"
    )
    assert item["icon_url"]
    icon = await client.get(item["icon_url"], headers=auth)
    assert icon.status_code == 200, icon.text
    assert icon.content


async def test_install_rejects_missing_skills_corruption_and_mutating_composer(env):
    client, server, auth = env
    market, skill, expert, version = publish(server)
    route = f"/api/experts/market/{version['version_id']}/install"
    for extra in [
        {"file_overrides": [{"name": "SOUL.md", "content": "changed"}]},
        {"hub_skills": [{"skill_name": "external"}]},
        {"skill_package_ids": ["not-a-locked-skill"]},
    ]:
        response = await client.post(route, headers=auth, json={"name": "invalid", **extra})
        assert response.status_code == 422, response.text
    market.listing(skill["resource_id"], False)
    response = await client.post(route, headers=auth, json={"name": "missing skill"})
    assert response.status_code == 409, response.text
    market.listing(skill["resource_id"], True)
    # Deliberately corrupt this private fixture, including Windows read-only files.
    (market.objects / version["object_sha256"]).chmod(0o600)
    (market.objects / version["object_sha256"]).unlink()
    (market.objects / version["object_sha256"]).write_bytes(b"changed content")
    response = await client.post(route, headers=auth, json={"name": "corrupt"})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "MARKET_CONTENT_CORRUPT"


async def test_assigned_employee_cannot_install_even_with_module_permission(env):
    client, server, auth = env
    market, _, _, version = publish(server)
    peer = await create_user(client, auth, username="assigned_consumer")
    user = server.services.user_repo.get_by_username("assigned_consumer")
    with server.services.db.transaction() as conn:
        conn.execute("UPDATE users SET expert_access_mode='assigned_only' WHERE id=?", (user.id,))
    response = await client.get("/api/experts/market", headers=peer)
    assert response.status_code == 200, response.text
    assert not response.json()["can_create"]
    response = await client.post(
        f"/api/experts/market/{version['version_id']}/install",
        headers=peer,
        json={"name": "unauthorized"},
    )
    assert response.status_code == 403, response.text


async def test_unsupported_configuration_and_failed_workspace_do_not_leave_agent(env):
    client, server, auth = env
    market, _, expert, version = publish(server, dependencies=False)
    _, configured = release(
        market,
        rewrite(
            example_package("expert"),
            version="1.1.0",
            updates={"configuration": {"system_url": {"type": "url", "required": True}}},
        ),
        expert["resource_id"],
    )
    response = await client.post(
        f"/api/experts/market/{configured['version_id']}/install",
        headers=auth,
        json={"name": "unconfigured"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "MARKET_INSTALL_UNSUPPORTED"
    from moss_harness.backends.workspace import BackendWorkspace

    before = {x.agent_id for x in server.services.agent_repo.list_all()}
    with (
        patch.object(BackendWorkspace, "aupload_many", side_effect=OSError("upload failed")),
        pytest.raises(OSError, match="upload failed"),
    ):
        await client.post(
            f"/api/experts/market/{version['version_id']}/install",
            headers=auth,
            json={"name": "failed upload"},
        )
    assert {x.agent_id for x in server.services.agent_repo.list_all()} == before
    await asyncio.sleep(0)
