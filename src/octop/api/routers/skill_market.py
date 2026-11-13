"""Read and install published local skills using consumption permissions."""

from __future__ import annotations

import mimetypes
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from octop.api.deps import get_server, require_permission
from octop.infra.market.skill_installation import LocalSkillMarket
from octop.infra.server import MossworkServer
from octop.infra.users.identity import User
from octop.infra.utils.asyncio import offload

router = APIRouter(prefix="/skill-packages/market")


class SkillDependency(BaseModel):
    resource_id: str
    version: str


class SkillRelease(BaseModel):
    resource_id: str
    version_id: str
    version: str
    object_sha256: str
    name: str
    description: str
    dependencies: list[SkillDependency]
    icon_url: str | None = None


class SkillLock(SkillDependency):
    version_id: str
    object_sha256: str


class SkillCatalog(BaseModel):
    items: list[SkillRelease]
    next_offset: int | None = None


class SkillFilePreview(BaseModel):
    name: str
    content: str


class SkillReleaseDetail(SkillRelease):
    slug: str
    changelog: str
    versions: list[SkillRelease]
    lock: list[SkillLock]
    file_contents: list[SkillFilePreview]
    runtime: dict[str, Any]
    configuration: dict[str, Any]


class InstallSkillRelease(BaseModel):
    name: str = Field(
        min_length=1, max_length=200, description="Unique name for the new skill package snapshot."
    )


class InstalledSkillPackage(BaseModel):
    id: str
    name: str
    skill_count: int


class SkillInstallationSource(SkillRelease):
    lock: list[SkillLock]


class SkillInstallationResult(BaseModel):
    package: InstalledSkillPackage
    source: SkillInstallationSource


def service(server: MossworkServer = Depends(get_server)) -> LocalSkillMarket:
    assert server.services is not None
    return LocalSkillMarket(server.services)


@router.get("", response_model=SkillCatalog, summary="Browse published local skills")
async def catalog(
    offset: int = Query(0, ge=0),
    limit: int = Query(60, ge=1, le=100),
    _user: User = Depends(require_permission("skill_packages")),
    market: LocalSkillMarket = Depends(service),
) -> Any:
    return await offload(market.catalog, offset, limit)


@router.get(
    "/{version_id}",
    response_model=SkillReleaseDetail,
    summary="Inspect a skill release and its exact dependencies",
)
async def detail(
    version_id: str,
    _user: User = Depends(require_permission("skill_packages")),
    market: LocalSkillMarket = Depends(service),
) -> Any:
    return await offload(market.detail, version_id)


@router.get(
    "/{version_id}/icon",
    response_class=Response,
    summary="Serve an offline icon from a published local skill release",
)
async def icon(
    version_id: str,
    _user: User = Depends(require_permission("skill_packages")),
    server: MossworkServer = Depends(get_server),
) -> Response:
    assert server.services is not None
    path, payload = await offload(
        LocalSkillMarket(server.services).releases.icon, version_id, kind="skill"
    )
    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return Response(
        payload,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post(
    "/{version_id}/install",
    response_model=SkillInstallationResult,
    status_code=201,
    summary="Install a local skill release into a new skill package",
    description="Requires skill_packages permission, not market publishing access. Copies the exact "
    "release and all pinned dependencies without network access. Existing packages are never overwritten.",
)
async def install(
    version_id: str,
    body: InstallSkillRelease,
    user: User = Depends(require_permission("skill_packages")),
    market: LocalSkillMarket = Depends(service),
) -> Any:
    return await offload(market.install, user, version_id, body.name)
