"""Install deployment-local releases as independent, versioned skill packages."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from octop.infra.db.services import SharedServices
from octop.infra.market.expert_installation import LocalExpertMarket
from octop.infra.market.packages import invalid
from octop.infra.skills.skill_package_store import (
    SkillPackageStore,
    is_skill_package_name_conflict,
    raise_skill_package_name_taken,
)
from octop.infra.users.identity import User
from octop.infra.utils.ulid import new_short_id


class LocalSkillMarket:
    def __init__(self, services: SharedServices) -> None:
        self.releases = LocalExpertMarket(services)
        self.store = SkillPackageStore(
            repo=services.skill_package_repo, root=services.paths.skill_packages_dir
        )

    def catalog(self, offset: int = 0, limit: int = 60) -> dict[str, Any]:
        rows = self.releases.repo.catalog("skill", offset=offset, limit=limit)
        items = [self.releases.card(row, kind="skill") for row in rows]
        return {
            "items": items,
            "next_offset": offset + len(items) if len(items) == limit else None,
        }

    def detail(self, version_id: str) -> dict[str, Any]:
        root, lock, files = self.releases.resolve(version_id, kind="skill")
        manifest = json.loads(root["manifest_json"])
        return {
            **self.releases.card(root, kind="skill"),
            "slug": manifest["slug"],
            "changelog": manifest["changelog"],
            "runtime": manifest["runtime"],
            "configuration": manifest["configuration"],
            "versions": [
                self.releases.card(v, kind="skill")
                for v in self.releases.repo.versions(root["resource_id"])
            ],
            "lock": lock,
            "file_contents": [
                {"name": path, "content": data.decode("utf-8", errors="replace")}
                for path, data in files.items()
                if path.endswith(".md") and len(data) <= 256 * 1024
            ],
        }

    def install(self, user: User, version_id: str, name: str) -> dict[str, Any]:
        """Never merge into or overwrite a package currently mounted by an Agent."""
        name = name.strip()
        if not name:
            raise invalid("package_name")
        lock, files = self.releases.installation_content(version_id, kind="skill")
        root = self.releases._version(version_id, "skill")
        source = {**self.releases.card(root, kind="skill"), "lock": lock}
        grouped: dict[str, list[tuple[str, bytes]]] = {}
        for path, data in files.items():
            _, slug, relative = path.split("/", 2)
            grouped.setdefault(slug, []).append((relative, data))
        if self.store.repo.get_by_name(name) is not None:
            raise_skill_package_name_taken(name)
        self.store.root.mkdir(parents=True, exist_ok=True)
        package_id = new_short_id()
        target = self.store.root / package_id
        # Stage the entire dependency closure before the row becomes visible to
        # package lists or Agent mount requests. A failed install exposes no partial package.
        with tempfile.TemporaryDirectory(
            prefix=".market-install-", dir=self.store.root
        ) as temporary:
            staged = SkillPackageStore(repo=self.store.repo, root=Path(temporary))
            for slug, content in grouped.items():
                staged.write_skill(package_id, slug, content)
            # The receipt records original provenance; local edits can diverge.
            (staged.root / package_id / "market-source.json").write_text(
                json.dumps(source, ensure_ascii=False), encoding="utf-8"
            )
            (staged.root / package_id).rename(target)
            try:
                row = self.store.repo.create(
                    id=package_id,
                    name=name,
                    description=source["description"],
                    created_by=str(user.id),
                    skill_count=len(grouped),
                )
            except Exception as exc:
                shutil.rmtree(target)
                if is_skill_package_name_conflict(exc):
                    raise_skill_package_name_taken(name)
                raise
        package = asdict(row)
        package.pop("pk", None)
        return {"package": package, "source": source}
