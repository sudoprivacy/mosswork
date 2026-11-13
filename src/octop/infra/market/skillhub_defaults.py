"""Seed an optional, build-time SkillHub snapshot into the local market.

The snapshot is intentionally separate from the repository's built-in expert
library.  It is only present when a maintainer has run the build-time fetcher
and accepted the upstream redistribution terms; runtime startup never calls
SkillHub or any other external URL.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from octop.infra.db.services import SharedServices
from octop.infra.market.defaults import DefaultRelease
from octop.infra.market.packages import parse_package
from octop.infra.utils.immutable_files import store_object

SNAPSHOT_ROOT = Path(__file__).with_name("skillhub_snapshot")


def _read_index(root: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((root / "index.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def build_skillhub_releases(snapshot_root: Path | None = None) -> list[DefaultRelease]:
    root = snapshot_root or SNAPSHOT_ROOT
    index = _read_index(root)
    if not index or index.get("format") != "mosswork.skillhub.snapshot.v1":
        return []
    releases: list[DefaultRelease] = []
    for kind, records in (("skill", index.get("skills")), ("expert", index.get("skillsets"))):
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict) or record.get("status") != "downloaded":
                continue
            archive_name = record.get("archive")
            source_slug = str(record.get("source_slug") or "").strip()
            local_slug = str(record.get("local_slug") or "").strip()
            if not isinstance(archive_name, str) or not source_slug or not local_slug:
                continue
            archive = root / archive_name
            try:
                package = parse_package(archive.read_bytes())
            except (OSError, ValueError):
                continue
            resource_id = f"skillhub-{kind}-{source_slug}"
            releases.append(DefaultRelease(resource_id, local_slug, kind, package))
    return releases


def seed_skillhub_market(services: SharedServices, snapshot_root: Path | None = None) -> int:
    """Publish downloaded SkillHub snapshot releases, if one is bundled."""
    root = snapshot_root or SNAPSHOT_ROOT
    seeded = 0
    objects = services.paths.root / "resource-market" / "objects"
    for release in build_skillhub_releases(root):
        payload = release.package.archive()
        digest = store_object(objects, payload)
        manifest = release.package.manifest.model_dump_json()
        if services.resource_market_repo.seed_release(
            resource_id=release.resource_id,
            slug=release.slug,
            kind=release.kind,
            name=release.package.manifest.name,
            description=release.package.manifest.description,
            version_id=f"{release.resource_id}-v{release.package.manifest.version}",
            draft_id=f"{release.resource_id}-draft-{release.package.manifest.version}",
            version=release.package.manifest.version,
            digest=digest,
            manifest=manifest,
            inventory=json.dumps(release.package.inventory()),
            file_count=len(release.package.files),
            total_bytes=sum(len(data) for data in release.package.files.values()),
        ):
            seeded += 1
    return seeded


__all__ = ["SNAPSHOT_ROOT", "build_skillhub_releases", "seed_skillhub_market"]
