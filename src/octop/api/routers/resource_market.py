"""Authenticated HTTP adapters for the deployment-local resource market."""

from __future__ import annotations

import mimetypes
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from octop.api.deps import get_server, require_permission
from octop.infra.market.packages import MAX_BYTES, MarketManifest, invalid
from octop.infra.market.service import ResourceMarket, example_package
from octop.infra.users.identity import User
from octop.infra.utils.asyncio import offload

router = APIRouter(prefix="/market")


def market(
    user: User = Depends(require_permission("resource_market")),
    server: Any = Depends(get_server),
) -> ResourceMarket:
    assert server.services is not None
    return ResourceMarket(server.services, user)


class Resource(BaseModel):
    resource_id: str
    slug: str
    kind: Literal["skill", "expert"]
    name: str
    description: str
    owner_user_id: int | None
    recommended_version_id: str | None
    listed: bool
    created_at: int
    recommended_version: str | None = None
    version_count: int = 0
    icon_url: str | None = None


class PackageFile(BaseModel):
    path: str
    size: int
    sha256: str


class Draft(BaseModel):
    draft_id: str
    resource_id: str
    version: str
    state: Literal["draft", "submitted", "rejected", "published"]
    object_sha256: str
    manifest: MarketManifest
    files: list[PackageFile]
    file_count: int
    total_bytes: int
    created_by: int | None
    created_at: int
    reviewed_by: int | None
    review_note: str
    reviewed_at: int | None


class Version(BaseModel):
    version_id: str
    resource_id: str
    version: str
    draft_id: str
    object_sha256: str
    manifest: MarketManifest
    published_by: int | None
    published_at: int


class Detail(BaseModel):
    resource: Resource
    versions: list[Version]
    drafts: list[Draft]


class DigestBody(BaseModel):
    expected_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="Digest of the frozen package the user inspected."
    )


class ReviewBody(DigestBody):
    action: Literal["publish", "reject"]
    note: str = Field(
        default="", max_length=4000, description="Review decision; stored in the audit trail."
    )


class ListingBody(BaseModel):
    listed: bool


class RecommendBody(BaseModel):
    version_id: str = Field(max_length=100)
    expected_version_id: str | None = Field(
        default=None,
        max_length=100,
        description="Previously observed recommendation, for concurrent-change detection.",
    )


class Preview(BaseModel):
    content: str


@router.get(
    "/resources", response_model=list[Resource], summary="List visible local market resources"
)
async def resources(offset: int = Query(0, ge=0), service: ResourceMarket = Depends(market)) -> Any:
    return await offload(service.list, offset)


@router.get(
    "/resources/{resource_id}",
    response_model=Detail,
    summary="Inspect versions and authorized drafts",
)
async def detail(resource_id: str, service: ResourceMarket = Depends(market)) -> Any:
    return await offload(service.detail, resource_id)


@router.post(
    "/import",
    response_model=Draft,
    status_code=201,
    summary="Import an offline ZIP as a frozen draft",
    description="Raw application/zip body, up to 64 MiB. Does not execute code or modify Agents. New versions require resource_id and owner/admin permission.",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def import_package(
    request: Request,
    resource_id: str | None = Query(None, max_length=100),
    service: ResourceMarket = Depends(market),
) -> Any:
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > MAX_BYTES:
            raise invalid("size_limit")
        payload.extend(chunk)
    return await offload(service.import_package, bytes(payload), resource_id)


@router.post(
    "/drafts/{draft_id}/submit",
    response_model=Draft,
    summary="Freeze and submit a draft for administrator review",
)
async def submit(draft_id: str, body: DigestBody, service: ResourceMarket = Depends(market)) -> Any:
    return await offload(service.submit, draft_id, body.expected_sha256)


@router.post(
    "/drafts/{draft_id}/review",
    response_model=Draft,
    summary="Publish or reject a submitted package",
    description="Requires the actual administrator role. Publication validates fixed skill dependencies and creates an immutable version; it does not install or upgrade Agents.",
)
async def review(draft_id: str, body: ReviewBody, service: ResourceMarket = Depends(market)) -> Any:
    return await offload(
        service.review,
        draft_id,
        body.expected_sha256,
        publish=body.action == "publish",
        note=body.note,
    )


@router.get(
    "/drafts/{draft_id}/preview",
    response_model=Preview,
    summary="Preview a draft file as plain text",
)
async def preview(
    draft_id: str, path: str = Query(max_length=500), service: ResourceMarket = Depends(market)
) -> Preview:
    return Preview(content=await offload(service.preview, draft_id, path))


@router.put(
    "/resources/{resource_id}/listing",
    status_code=204,
    summary="List or unlist a resource without deleting versions",
)
async def listing(
    resource_id: str, body: ListingBody, service: ResourceMarket = Depends(market)
) -> Response:
    await offload(service.listing, resource_id, body.listed)
    return Response(status_code=204)


@router.put(
    "/resources/{resource_id}/recommendation",
    status_code=204,
    summary="Choose an existing released version as recommended",
)
async def recommend(
    resource_id: str, body: RecommendBody, service: ResourceMarket = Depends(market)
) -> Response:
    await offload(service.recommend, resource_id, body.version_id, body.expected_version_id)
    return Response(status_code=204)


@router.get(
    "/versions/{version_id}/download",
    response_class=Response,
    summary="Download a verified released ZIP",
    responses={
        200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}
    },
)
async def download(version_id: str, service: ResourceMarket = Depends(market)) -> Response:
    filename, payload = await offload(service.download, version_id)
    return Response(
        payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/versions/{version_id}/icon",
    response_class=Response,
    summary="Serve an offline icon from a published local resource",
)
async def icon(version_id: str, service: ResourceMarket = Depends(market)) -> Response:
    path, payload = await offload(service.icon, version_id)
    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return Response(
        payload,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get(
    "/examples/{kind}",
    response_class=Response,
    summary="Download a self-contained offline development template",
    responses={
        200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}
    },
)
async def example(
    kind: Literal["skill", "expert"], service: ResourceMarket = Depends(market)
) -> Response:
    payload = await offload(example_package, kind)
    return Response(
        payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="market-{kind}-example.zip"'},
    )
