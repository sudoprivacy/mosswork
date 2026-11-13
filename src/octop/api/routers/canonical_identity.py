"""Current canonical human identity over an explicitly injected authority."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import CanonicalSnapshotService
from octop.infra.utils.locale import resolve_request_locale

_BEARER = HTTPBearer(
    scheme_name="CanonicalHumanBearer",
    description="Human access token verified by the configured canonical account authority.",
    auto_error=False,
)


class CanonicalOrganizationResponse(BaseModel):
    id: str = Field(description="Original canonical organization UUID.")
    name: str


class CanonicalMemberResponse(BaseModel):
    id: str = Field(description="Original member UUID in the selected organization.")
    role: str
    status: str
    revision: int


class CanonicalMossAccessResponse(BaseModel):
    is_enabled: bool
    is_granted: bool
    source: Literal["role", "explicit"]


class CanonicalIdentityResponse(BaseModel):
    account_id: str = Field(description="Original human account UUID, shared across memberships.")
    organization: CanonicalOrganizationResponse
    member: CanonicalMemberResponse
    moss_access: CanonicalMossAccessResponse
    personal_agent_ref: str | None = Field(
        description="Retained personal Moss reference; null when current access is disabled."
    )
    can_switch_organization: bool = Field(
        description="Whether this original human login can select another existing phone-account membership."
    )


class CanonicalOrganizationMembershipResponse(BaseModel):
    organization: CanonicalOrganizationResponse
    member: CanonicalMemberResponse


class CanonicalOrganizationsResponse(BaseModel):
    organizations: list[CanonicalOrganizationMembershipResponse]


def build_canonical_identity_router(service: CanonicalSnapshotService) -> APIRouter:
    """Bind a single trusted authority; request data cannot choose its provider."""
    router = APIRouter()

    @router.get(
        "/me",
        response_model=CanonicalIdentityResponse,
        summary="Read current canonical account, organization and personal Moss access",
        description=(
            "Requires a current human login from the configured canonical authority. "
            "Reads current membership and explicit Moss grants for the token's selected "
            "organization. It does not select an organization, create an Agent or return "
            "model credentials. Unavailable authority or retained schema fails closed."
        ),
        responses={
            401: {"description": "Invalid or revoked human login"},
            503: {"description": "Canonical authority unavailable"},
        },
    )
    async def me(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            if credentials is None or credentials.scheme.lower() != "bearer":
                raise MossworkError.localized(ErrorCode.AUTH_FAILED)
            snapshot = await service.snapshot(credentials.credentials)
            payload = CanonicalIdentityResponse(
                account_id=snapshot.account_id,
                organization=CanonicalOrganizationResponse(
                    id=snapshot.organization_id, name=snapshot.organization_name
                ),
                member=CanonicalMemberResponse(
                    id=snapshot.member.owner_id,
                    role=snapshot.member.role,
                    status=snapshot.member.status,
                    revision=snapshot.membership_revision,
                ),
                moss_access=CanonicalMossAccessResponse(
                    is_enabled=snapshot.moss_access.is_enabled,
                    is_granted=snapshot.moss_access.is_granted,
                    source=snapshot.moss_access.source,
                ),
                personal_agent_ref=(
                    snapshot.personal_agent.reference if snapshot.personal_agent else None
                ),
                can_switch_organization=(
                    snapshot.auth_app == "sudowork" and snapshot.key_id == "phone-login"
                ),
            )
            return JSONResponse(payload.model_dump(), headers={"Cache-Control": "no-store"})
        except MossworkError as exc:
            return JSONResponse(
                status_code=exc.status,
                content=exc.to_envelope(locale=resolve_request_locale(request)),
                headers={"Cache-Control": "no-store"},
            )

    @router.get(
        "/organizations",
        response_model=CanonicalOrganizationsResponse,
        summary="List this verified phone account's existing organizations",
        description=(
            "Requires the original current phone login. Organization passwords do not "
            "prove ownership of other memberships. Returns only the person's existing "
            "active memberships, preserving each original member UUID. Listing does not "
            "switch organization, grant Moss use or authorize a native session."
        ),
        responses={
            401: {"description": "Invalid or revoked human login"},
            403: {"description": "Verified phone login required"},
            503: {"description": "Canonical authority unavailable"},
        },
    )
    async def organizations(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            if credentials is None or credentials.scheme.lower() != "bearer":
                raise MossworkError.localized(ErrorCode.AUTH_FAILED)
            memberships = await service.organizations(credentials.credentials)
            payload = CanonicalOrganizationsResponse(
                organizations=[
                    CanonicalOrganizationMembershipResponse(
                        organization=CanonicalOrganizationResponse(
                            id=item.organization_id, name=item.organization_name
                        ),
                        member=CanonicalMemberResponse(
                            id=item.member_id,
                            role=item.role,
                            status="active",
                            revision=item.membership_revision,
                        ),
                    )
                    for item in memberships
                ]
            )
            return JSONResponse(payload.model_dump(), headers={"Cache-Control": "no-store"})
        except MossworkError as exc:
            return JSONResponse(
                status_code=exc.status,
                content=exc.to_envelope(locale=resolve_request_locale(request)),
                headers={"Cache-Control": "no-store"},
            )

    return router
