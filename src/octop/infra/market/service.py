"""Local market lifecycle; a release never mutates an Agent workspace."""

from __future__ import annotations

import json
from typing import Any, Literal

from octop.infra.db.repos.resource_market import MarketConflict, MarketDependencyConflict
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.market.packages import MAX_BYTES, invalid, parse_package, semver_key
from octop.infra.users.identity import User
from octop.infra.users.permissions import user_has_permission
from octop.infra.utils.immutable_files import read_object, store_object
from octop.infra.utils.ulid import new_ulid


class ResourceMarket:
    def __init__(self, services: SharedServices, user: User) -> None:
        if not user_has_permission(user, "resource_market"):
            raise MossworkError.localized(ErrorCode.FORBIDDEN)
        self.services = services
        self.repo = services.resource_market_repo
        self.user = user
        self.objects = services.paths.root / "resource-market" / "objects"

    def require_admin(self) -> None:
        if not self.user.is_admin:
            raise MossworkError.localized(ErrorCode.FORBIDDEN)

    def _resource(self, resource_id: str, *, manage: bool = False) -> dict[str, Any]:
        row = self.repo.resource(resource_id)
        if row is None:
            raise MossworkError.localized(ErrorCode.NOT_FOUND)
        owns = self.user.is_admin or row["owner_user_id"] == self.user.id
        if manage and not owns:
            raise MossworkError.localized(ErrorCode.FORBIDDEN)
        if not owns and (not row["listed"] or not row["recommended_version_id"]):
            raise MossworkError.localized(ErrorCode.NOT_FOUND)
        return row

    def _draft(self, draft_id: str) -> dict[str, Any]:
        draft = self.repo.draft(draft_id)
        if draft is None:
            raise MossworkError.localized(ErrorCode.NOT_FOUND)
        self._resource(draft["resource_id"], manage=True)
        return draft

    @staticmethod
    def _item(row: dict[str, Any]) -> dict[str, Any]:
        item = {
            key: value for key, value in row.items() if key != "id" and not key.endswith("_json")
        }
        manifest_json = row.get("manifest_json") or row.get("recommended_manifest_json")
        if manifest_json:
            item["manifest"] = json.loads(manifest_json)
        if "inventory_json" in row:
            item["files"] = json.loads(row["inventory_json"])
        recommended_version_id = row.get("recommended_version_id")
        manifest = item.get("manifest")
        if recommended_version_id and isinstance(manifest, dict) and manifest.get("icon_path"):
            # Dashboard API helpers add the /api prefix. Keep resource URLs
            # relative to that prefix so clients do not request /api/api/… .
            item["icon_url"] = f"/market/versions/{recommended_version_id}/icon"
        else:
            item["icon_url"] = None
        return item

    def icon(self, version_id: str) -> tuple[str, bytes]:
        row = self.repo.version(version_id)
        if row is None:
            raise MossworkError.localized(ErrorCode.NOT_FOUND)
        self._resource(row["resource_id"])
        package = parse_package(self._read(row["object_sha256"]))
        icon_path = package.manifest.icon_path
        if not icon_path or icon_path not in package.files:
            raise MossworkError.localized(ErrorCode.NOT_FOUND)
        return icon_path, package.files[icon_path]

    def list(self, offset: int = 0) -> list[dict[str, Any]]:
        return [
            self._item(row)
            for row in self.repo.resources(self.user.id, admin=self.user.is_admin, offset=offset)
        ]

    def detail(self, resource_id: str) -> dict[str, Any]:
        resource = self._resource(resource_id)
        resource_item = self._item(resource)
        if resource.get("recommended_version_id"):
            recommended = self.repo.version(resource["recommended_version_id"])
            if recommended:
                manifest = json.loads(recommended["manifest_json"])
                resource_item["icon_url"] = (
                    f"/market/versions/{recommended['version_id']}/icon"
                    if manifest.get("icon_path")
                    else None
                )
        versions = sorted(
            self.repo.versions(resource_id),
            key=lambda row: semver_key(row["version"]),
            reverse=True,
        )
        drafts = (
            self.repo.drafts(resource_id)
            if self.user.is_admin or resource["owner_user_id"] == self.user.id
            else []
        )
        return {
            "resource": resource_item,
            "versions": [self._item(row) for row in versions],
            "drafts": [self._item(row) for row in drafts],
        }

    def import_package(self, payload: bytes, resource_id: str | None = None) -> dict[str, Any]:
        package = parse_package(payload)
        manifest = package.manifest
        if manifest.resource_id and manifest.resource_id != resource_id:
            raise invalid("resource_identity")
        if resource_id:
            resource = self._resource(resource_id, manage=True)
            if resource["kind"] != manifest.kind or resource["slug"] != manifest.slug:
                raise invalid("resource_identity")
        else:
            resource_id = new_ulid()
        if any(item.resource_id == resource_id for item in manifest.dependencies):
            raise invalid("self_dependency")
        # Store complete, verified bytes before committing a discoverable draft.
        archive = package.archive()
        if len(archive) > MAX_BYTES:
            raise invalid("size_limit")
        digest = store_object(self.objects, archive)
        draft_id = new_ulid()
        try:
            self.repo.import_draft(
                resource_id=resource_id,
                draft_id=draft_id,
                slug=manifest.slug,
                kind=manifest.kind,
                name=manifest.name,
                description=manifest.description,
                user_id=self.user.id,
                version=manifest.version,
                digest=digest,
                manifest=manifest.model_dump_json(),
                inventory=json.dumps(package.inventory()),
                file_count=len(package.files),
                total_bytes=sum(map(len, package.files.values())),
            )
        except MarketConflict as exc:
            raise MossworkError.localized(ErrorCode.MARKET_CONFLICT) from exc
        return self._item(self._draft(draft_id))

    def _read(self, digest: str) -> bytes:
        try:
            return read_object(self.objects, digest, limit=MAX_BYTES)
        except (OSError, ValueError) as exc:
            raise MossworkError.localized(ErrorCode.MARKET_CONTENT_CORRUPT) from exc

    def submit(self, draft_id: str, digest: str) -> dict[str, Any]:
        draft = self._draft(draft_id)
        self._read(draft["object_sha256"])
        try:
            self.repo.submit(draft_id, digest, self.user.id)
        except MarketConflict as exc:
            raise MossworkError.localized(ErrorCode.MARKET_CONFLICT) from exc
        return self._item(self._draft(draft_id))

    def review(self, draft_id: str, digest: str, *, publish: bool, note: str) -> dict[str, Any]:
        self.require_admin()
        if not publish and not note.strip():
            raise invalid("review_note_required")
        draft = self._draft(draft_id)
        # Recheck the bytes that were previewed. Approval never accepts changed content.
        package = parse_package(self._read(draft["object_sha256"])) if publish else None
        try:
            self.repo.review(
                draft_id,
                digest,
                self.user.id,
                publish=publish,
                version_id=new_ulid(),
                note=note,
                dependencies=[item.model_dump() for item in package.manifest.dependencies]
                if package
                else [],
            )
        except MarketDependencyConflict as exc:
            raise MossworkError.localized(ErrorCode.MARKET_DEPENDENCY_UNAVAILABLE) from exc
        except MarketConflict as exc:
            raise MossworkError.localized(ErrorCode.MARKET_CONFLICT) from exc
        return self._item(self._draft(draft_id))

    def listing(self, resource_id: str, listed: bool) -> None:
        self.require_admin()
        self._resource(resource_id, manage=True)
        self.repo.listing(resource_id, listed, self.user.id)

    def recommend(self, resource_id: str, version_id: str, expected: str | None) -> None:
        self.require_admin()
        self._resource(resource_id, manage=True)
        try:
            self.repo.recommend(resource_id, version_id, expected, self.user.id)
        except MarketConflict as exc:
            raise MossworkError.localized(ErrorCode.MARKET_CONFLICT) from exc

    def download(self, version_id: str) -> tuple[str, bytes]:
        row = self.repo.version(version_id)
        if row is None:
            raise MossworkError.localized(ErrorCode.NOT_FOUND)
        resource = self._resource(row["resource_id"])
        return f"{resource['slug']}-{row['version']}.zip", self._read(row["object_sha256"])

    def preview(self, draft_id: str, path: str) -> str:
        row = self._draft(draft_id)
        package = parse_package(self._read(row["object_sha256"]))
        data = package.files.get(path)
        if data is None:
            raise MossworkError.localized(ErrorCode.NOT_FOUND)
        if len(data) > 256 * 1024:
            raise invalid("preview_limit", path)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise invalid("preview_binary", path) from exc


def example_package(kind: Literal["skill", "expert"]) -> bytes:
    """Small self-contained template; no model, tools, network or private data."""
    slug = "inspection-checklist" if kind == "skill" else "inspection-assistant"
    manifest: dict[str, Any] = {
        "format": "mosswork.market.v1",
        "kind": kind,
        "slug": slug,
        "name": slug,
        "description": "Offline inspection checklist template",
        "version": "1.0.0",
        "changelog": "Initial release",
        "license": "MIT",
        "runtime": {"execution_targets": ["server", "desktop"], "platforms": ["any"]},
    }
    files = {"market.json": json.dumps(manifest).encode()}
    if kind == "skill":
        files["SKILL.md"] = (
            f"---\nname: {slug}\ndescription: Create an inspection checklist from user-provided data.\n---\n\n"
            "Ask for the equipment and inspection scope. Produce a checklist. "
            "Mark missing data as unknown. Do not claim to have queried a live system.\n"
        ).encode()
    else:
        files["manifest.json"] = json.dumps(
            {
                "id": slug,
                "label": {"zh": "巡检助手", "en": "Inspection Assistant"},
                "prompt_files": ["SOUL.md"],
            },
            ensure_ascii=False,
        ).encode()
        files["SOUL.md"] = (
            b"Create inspection plans from user-provided data. Ask for missing facts. Do not claim live system access.\n"
        )
    return parse_package(_example_archive(files)).archive()


def _example_archive(files: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()
