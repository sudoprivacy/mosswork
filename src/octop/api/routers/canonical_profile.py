"""Thin, explicitly injected canonical human self-profile adapters."""

from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from octop.api.routers.canonical_auth import _access, _CanonicalAuthRoute, _error
from octop.api.routers.canonical_identity import _BEARER
from octop.infra.errors import MossworkError
from octop.infra.identity.self_profile import CanonicalSelfProfileService

_NO_STORE = {"Cache-Control": "no-store"}


class CanonicalProfileResponse(BaseModel):
    account_id: str
    organization_id: str
    member_id: str
    login_name: str
    nickname: str
    avatar: str | None
    can_change_password: bool


class CanonicalNicknameBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    nickname: str = Field(repr=False)


class CanonicalPasswordChangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    old_password: str = Field(alias="oldPassword", repr=False)
    new_password: str = Field(alias="newPassword", repr=False)


class CanonicalProfileUpdatedResponse(BaseModel):
    success: bool


class CanonicalAvatarBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    avatar: str | None = Field(repr=False)


class CanonicalAvatarResponse(BaseModel):
    avatar: str | None


def build_canonical_profile_router(service: CanonicalSelfProfileService) -> APIRouter:
    router = APIRouter(route_class=_CanonicalAuthRoute)

    @router.get(
        "/profile",
        response_model=CanonicalProfileResponse,
        summary="Read the current canonical member's own profile",
        description=(
            "Requires a current verified human login. Returns retained member profile fields "
            "and its already normalized stored avatar; does not process uploads, allocate "
            "local users, return credentials, or report runtime usage."
        ),
    )
    async def profile(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            result = await service.profile(_access(credentials))
            return JSONResponse(
                CanonicalProfileResponse(**asdict(result)).model_dump(), headers=_NO_STORE
            )
        except MossworkError as error:
            return _error(request, error)

    @router.post(
        "/profile/nickname",
        response_model=CanonicalProfileUpdatedResponse,
        summary="Update this canonical member's own nickname",
        description=(
            "Keeps the login name, phone and other memberships unchanged. Requires the "
            "current human actor and actual canonical writer admission; writes no grants."
        ),
    )
    async def update_nickname(
        request: Request,
        body: CanonicalNicknameBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            await service.update_nickname(_access(credentials), body.nickname)
            return JSONResponse({"success": True}, headers=_NO_STORE)
        except MossworkError as error:
            return _error(request, error)

    @router.post(
        "/profile/avatar",
        response_model=CanonicalAvatarResponse,
        summary="Update or clear this canonical member's own avatar",
        description=(
            "Accepts one PNG, JPEG or WebP data URL up to 2 MiB and 16 million pixels, "
            "or null to clear. Re-encodes a 256 by 256 WebP with metadata removed. Requires "
            "a current human login and the actual canonical writer; selects no other member."
        ),
    )
    async def update_avatar(
        request: Request,
        body: CanonicalAvatarBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            avatar = await service.update_avatar(_access(credentials), body.avatar)
            return JSONResponse({"avatar": avatar}, headers=_NO_STORE)
        except MossworkError as error:
            return _error(request, error)

    async def change(
        request: Request,
        body: CanonicalPasswordChangeBody,
        credentials: HTTPAuthorizationCredentials | None,
        *,
        moss_only: bool,
    ) -> JSONResponse:
        try:
            await service.change_password(
                _access(credentials), body.old_password, body.new_password, moss_only=moss_only
            )
            return JSONResponse({"success": True}, headers=_NO_STORE)
        except MossworkError as error:
            return _error(request, error)

    @router.post(
        "/password/change",
        response_model=CanonicalProfileUpdatedResponse,
        summary="Change this canonical member's password using its current password",
        description=(
            "Verifies the original password hash and current human actor, then atomically "
            "updates its hash and original password identity. Preserves the original absence "
            "of an additional password-login policy gate or bulk token revocation."
        ),
    )
    async def change_password(
        request: Request,
        body: CanonicalPasswordChangeBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        return await change(request, body, credentials, moss_only=False)

    @router.post(
        "/moss/password/change",
        response_model=CanonicalProfileUpdatedResponse,
        summary="Change a member password from the dedicated Moss human realm",
        description="Requires the actual verified auth_app=moss login and current member password.",
    )
    async def change_moss_password(
        request: Request,
        body: CanonicalPasswordChangeBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        return await change(request, body, credentials, moss_only=True)

    return router
