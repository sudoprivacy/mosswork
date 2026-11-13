"""Consume released local experts without publishing permissions or network access."""

from __future__ import annotations

import json
from typing import Any

from octop.infra.agents.experts.catalog import (
    WORKSPACE_MANIFEST_PATH,
    apply_workspace_quick_prompts,
    welcome_payload_from_manifest_data,
)
from octop.infra.agents.experts.published_creation import PublishedExpertInstallOptions
from octop.infra.agents.manager import AgentCreateSpec
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.history.trajectory.settings import apply_enable_trajectory
from octop.infra.market.packages import MAX_BYTES, MAX_FILES, Package, invalid, parse_package
from octop.infra.users.expert_access import ExpertAccess
from octop.infra.users.identity import User
from octop.infra.utils.asyncio import offload
from octop.infra.utils.immutable_files import read_object


class LocalExpertMarket:
    def __init__(self, services: SharedServices) -> None:
        self.services = services
        self.repo = services.resource_market_repo
        self.objects = services.paths.root / "resource-market" / "objects"

    @staticmethod
    def card(row: dict[str, Any], *, kind: str = "expert") -> dict[str, Any]:
        manifest = json.loads(row["manifest_json"])
        return {
            "resource_id": row["resource_id"],
            "version_id": row["version_id"],
            "version": row["version"],
            "object_sha256": row["object_sha256"],
            "name": manifest["name"],
            "description": manifest["description"],
            "dependencies": manifest["dependencies"],
            "icon_url": (
                f"/{'skill-packages' if kind == 'skill' else 'experts'}/market/{row['version_id']}/icon"
                if manifest.get("icon_path")
                else None
            ),
        }

    def catalog(self, user_id: int) -> dict[str, Any]:
        return {
            "items": [self.card(row) for row in self.repo.expert_catalog()],
            "can_create": not ExpertAccess(self.services).assigned_only(user_id),
        }

    def _version(self, version_id: str, kind: str) -> dict[str, Any]:
        row = self.repo.version(version_id)
        resource = self.repo.resource(row["resource_id"]) if row else None
        if not row or not resource or not resource["listed"] or resource["kind"] != kind:
            raise MossworkError.localized(ErrorCode.NOT_FOUND)
        return row

    def _package(self, row: dict[str, Any]) -> Package:
        try:
            payload = read_object(self.objects, row["object_sha256"], limit=MAX_BYTES)
        except (OSError, ValueError) as exc:
            raise MossworkError.localized(ErrorCode.MARKET_CONTENT_CORRUPT) from exc
        return parse_package(payload)

    def icon(self, version_id: str, *, kind: str = "expert") -> tuple[str, bytes]:
        row = self._version(version_id, kind)
        package = self._package(row)
        icon_path = package.manifest.icon_path
        if not icon_path or icon_path not in package.files:
            raise MossworkError.localized(ErrorCode.NOT_FOUND)
        return icon_path, package.files[icon_path]

    def resolve(
        self, version_id: str, *, kind: str = "expert"
    ) -> tuple[dict[str, Any], list[dict[str, str]], dict[str, bytes]]:
        """Resolve the full exact dependency graph; conflicting versions never overwrite files."""
        root = self._version(version_id, kind)
        lock: dict[str, dict[str, str]] = {}
        files: dict[str, bytes] = {}
        visiting: set[str] = set()
        paths: set[str] = set()
        total_bytes = 0

        def visit(row: dict[str, Any], kind: str) -> None:
            nonlocal total_bytes
            if len(visiting) + len(lock) >= 100:
                raise invalid("dependency_limit")
            resource_id = row["resource_id"]
            if resource_id in visiting:
                raise invalid("dependency_cycle")
            if resource_id in lock:
                if lock[resource_id]["version_id"] != row["version_id"]:
                    raise MossworkError.localized(ErrorCode.MARKET_DEPENDENCY_UNAVAILABLE)
                return
            visiting.add(resource_id)
            package = self._package(row)
            manifest = package.manifest
            for dependency in manifest.dependencies:
                resource = self.repo.resource(dependency.resource_id)
                dep = next(
                    (
                        v
                        for v in self.repo.versions(dependency.resource_id)
                        if v["version"] == dependency.version
                    ),
                    None,
                )
                if not resource or not resource["listed"] or resource["kind"] != "skill" or not dep:
                    raise MossworkError.localized(ErrorCode.MARKET_DEPENDENCY_UNAVAILABLE)
                visit(dep, "skill")
            for path, data in package.files.items():
                if kind == "expert":
                    if path == "market.json":
                        continue
                    target = WORKSPACE_MANIFEST_PATH if path == "manifest.json" else path
                else:
                    if path == "market.json":
                        continue
                    target = f"skills/{manifest.slug}/{path}"
                if target.casefold() in paths:
                    raise invalid("dependency_file_collision", target)
                total_bytes += len(data)
                if len(files) >= MAX_FILES or total_bytes > MAX_BYTES:
                    raise invalid("size_limit")
                paths.add(target.casefold())
                files[target] = data
            lock[resource_id] = {
                "resource_id": resource_id,
                "version_id": row["version_id"],
                "version": row["version"],
                "object_sha256": row["object_sha256"],
            }
            visiting.remove(resource_id)

        visit(root, kind)
        return root, list(lock.values()), files

    def detail(self, version_id: str) -> dict[str, Any]:
        root, lock, files = self.resolve(version_id)
        manifest = json.loads(files[WORKSPACE_MANIFEST_PATH])
        return {
            **self.card(root),
            "lock": lock,
            "file_contents": [
                {"name": path, "content": data.decode("utf-8")}
                for path, data in files.items()
                if path.endswith(".md") and len(data) <= 256 * 1024
            ],
            **welcome_payload_from_manifest_data(manifest),
        }

    def installation_content(
        self, version_id: str, *, kind: str = "expert"
    ) -> tuple[list[dict[str, str]], dict[str, bytes]]:
        _, lock, files = self.resolve(version_id, kind=kind)
        for entry in lock:
            row = self.repo.version(entry["version_id"])
            assert row is not None
            manifest = json.loads(row["manifest_json"])
            runtime = manifest["runtime"]
            # This installer supports portable server definitions. Other declared
            # execution environments/configuration require a future validated adapter.
            if (
                "server" not in runtime["execution_targets"]
                or "any" not in runtime["platforms"]
                or runtime["python"]
                or runtime["runtime_profile"]
                or manifest["required_capabilities"]
                or any(value["required"] for value in manifest["configuration"].values())
            ):
                raise MossworkError.localized(ErrorCode.MARKET_INSTALL_UNSUPPORTED)
        return lock, files

    async def install(
        self, registry: Any, user: User, version_id: str, options: PublishedExpertInstallOptions
    ) -> dict[str, Any]:
        ExpertAccess(self.services).assert_create(user.id, options.agent_id)
        if options.skill_package_ids:
            raise invalid("locked_dependencies")
        lock, files = await offload(self.installation_content, version_id)
        root = self._version(version_id, "expert")
        root_package = self._package(root)
        config: dict[str, Any] = {}
        if options.backend:
            config["backend"] = options.backend
        if options.providers:
            config["providers"] = options.providers
        apply_enable_trajectory(config, options.enable_trajectory)
        if options.conversation_mode:
            config["conversation_mode"] = options.conversation_mode

        async def initialize(row: Any, workspace: Any) -> None:
            await workspace.aupload_many(list(files.items()))
            if options.quick_prompts is not None:
                await apply_workspace_quick_prompts(workspace, options.quick_prompts)
            await offload(self.repo.bind_agent, row.agent_id, version_id, json.dumps(lock), user.id)

        created = await registry.create(
            AgentCreateSpec(
                name=options.name,
                agent_id=options.agent_id,
                user_id=user.id,
                description=options.description,
                default_model=options.default_model,
                config=config,
                runtime_config=options.runtime_config or {},
                color=options.color,
                icon_url=(
                    f"/experts/market/{version_id}/icon"
                    if root_package.manifest.icon_path
                    else None
                ),
                welcome_message=options.welcome_message,
                knowledge_base_ids=options.knowledge_base_ids,
                mcp_servers=options.mcp_servers,
            ),
            defer_bootstrap=True,
            workspace_initializer=initialize,
        )
        return {
            "agent_id": created.agent_id,
            "name": created.name,
            "version_id": version_id,
            "bootstrap_pending": not registry.is_bootstrapped(created.agent_id),
        }
