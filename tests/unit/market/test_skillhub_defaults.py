from __future__ import annotations

from collections import Counter

from octop.infra.market.skillhub_defaults import build_skillhub_releases


def test_skillhub_snapshot_contains_only_valid_offline_releases() -> None:
    releases = build_skillhub_releases()
    counts = Counter(item.kind for item in releases)
    # The snapshot is refreshed from the public SkillHub catalog.  Keep the
    # original expert-linked baseline while allowing the full market catalog
    # to grow as upstream publishes more skills.
    assert counts["skill"] >= 300
    assert counts["expert"] >= 48
    assert all(item.package.manifest.runtime.execution_targets == ["server"] for item in releases)
    assert all(
        dependency.resource_id.startswith("skillhub-skill-")
        for item in releases
        if item.kind == "expert"
        for dependency in item.package.manifest.dependencies
    )


def test_skillhub_expert_icons_are_packaged_offline() -> None:
    experts = [item for item in build_skillhub_releases() if item.kind == "expert"]
    with_icons = [item for item in experts if item.package.manifest.icon_path]
    assert len(with_icons) >= 40
    for item in with_icons:
        assert item.package.manifest.icon_path in item.package.files
