from __future__ import annotations

import json

import pytest
from tests.unit.market.test_resource_market import market_env as market_env
from tests.unit.market.test_resource_market import release, rewrite

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.market.expert_installation import LocalExpertMarket
from octop.infra.market.service import ResourceMarket, example_package


def named_skill(slug, *, dependencies=None):
    return rewrite(
        example_package("skill"),
        updates={"slug": slug, "name": slug, "dependencies": dependencies or []},
        files={
            "SKILL.md": f"---\nname: {slug}\ndescription: test skill\n---\nA test skill.".encode()
        },
    )


def test_transitive_skills_are_locked_and_conflicting_versions_fail(market_env):
    services, users = market_env
    market = ResourceMarket(services, users["admin"])
    leaf, old = release(market, named_skill("leaf-skill"))
    release(market, rewrite(named_skill("leaf-skill"), version="2.0.0"), leaf["resource_id"])
    dep = {"resource_id": leaf["resource_id"], "version": "1.0.0"}
    branch, _ = release(market, named_skill("branch-skill", dependencies=[dep]))
    expert, version = release(
        market,
        rewrite(
            example_package("expert"),
            updates={
                "dependencies": [{"resource_id": branch["resource_id"], "version": "1.0.0"}, dep]
            },
        ),
    )
    consumer = LocalExpertMarket(services)
    lock, files = consumer.installation_content(version["version_id"])
    assert len(lock) == 3
    assert "skills/leaf-skill/SKILL.md" in files
    assert "skills/branch-skill/SKILL.md" in files
    assert (
        next(x for x in lock if x["resource_id"] == leaf["resource_id"])["version_id"]
        == old["version_id"]
    )
    _, conflict = release(
        market,
        rewrite(
            example_package("expert"),
            version="1.1.0",
            updates={
                "dependencies": [
                    {"resource_id": branch["resource_id"], "version": "1.0.0"},
                    {"resource_id": leaf["resource_id"], "version": "2.0.0"},
                ]
            },
        ),
        expert["resource_id"],
    )
    with pytest.raises(MossworkError) as error:
        consumer.installation_content(conflict["version_id"])
    assert error.value.code == ErrorCode.MARKET_DEPENDENCY_UNAVAILABLE


@pytest.mark.parametrize(
    "updates",
    [
        {"runtime": {"execution_targets": ["desktop"], "platforms": ["any"]}},
        {"runtime": {"execution_targets": ["server"], "platforms": ["linux-amd64"]}},
        {"runtime": {"execution_targets": ["server"], "platforms": ["any"], "python": ">=3.12"}},
        {
            "runtime": {
                "execution_targets": ["server"],
                "platforms": ["any"],
                "runtime_profile": "custom",
            }
        },
        {"required_capabilities": ["enterprise-database"]},
    ],
)
def test_installer_does_not_claim_unavailable_runtime_capabilities(market_env, updates):
    services, users = market_env
    market = ResourceMarket(services, users["admin"])
    _, version = release(market, rewrite(example_package("expert"), updates=updates))
    with pytest.raises(MossworkError) as error:
        LocalExpertMarket(services).installation_content(version["version_id"])
    assert error.value.code == ErrorCode.MARKET_INSTALL_UNSUPPORTED


def test_local_detail_normalizes_welcome_without_trusting_manifest_shape(market_env):
    services, users = market_env
    market = ResourceMarket(services, users["admin"])
    _, version = release(
        market,
        rewrite(
            example_package("expert"),
            files={
                "manifest.json": json.dumps(
                    {
                        "id": "inspection-assistant",
                        "quick_prompts": "wrong shape",
                        "welcome_message": "Hello",
                    }
                ).encode()
            },
        ),
    )
    detail = LocalExpertMarket(services).detail(version["version_id"])
    assert detail["welcome_message"]["zh"] == "Hello"
    assert detail["quick_prompts"] == []
