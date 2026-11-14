"""Thin HTTP adapter for the explicit canonical platform login default."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict

from octop.api.routers.canonical_auth import _access, _CanonicalAuthRoute, _error
from octop.api.routers.canonical_identity import _BEARER
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.platform_login_policy import CanonicalPlatformLoginPolicyService


class PlatformLoginMethodBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    login_method: Literal["sms", "password", "cas"]


class PlatformLoginPolicyUpdatedResponse(BaseModel):
    success: Literal[True] = True


def _bound_service(request: Request, service: CanonicalPlatformLoginPolicyService) -> None:
    if (
        type(service) is not CanonicalPlatformLoginPolicyService
        or getattr(request.app.state, "canonical_platform_login_policy", None) is not service
        or getattr(request.app.state, "canonical_identity", None) is not service._snapshots
    ):
        raise MossworkError.localized(
            ErrorCode.INTERNAL_ERROR,
            status=503,
            details={"dependency": "canonical_platform_login_policy"},
        )


def build_canonical_platform_login_policy_router(
    service: CanonicalPlatformLoginPolicyService,
) -> APIRouter:
    """Bind one current service; no implicit organization or platform selection."""
    router = APIRouter(route_class=_CanonicalAuthRoute)

    @router.put(
        "/platform/login-policy",
        response_model=PlatformLoginPolicyUpdatedResponse,
        summary="Set the platform default login method",
        description=(
            "Requires the current canonical super administrator, signed administrative "
            "ceiling, canonical writer admission and actual platform SMS or CAS readiness. "
            "Preserves organization overrides and unrelated platform policy fields."
        ),
    )
    async def set_method(
        request: Request,
        body: PlatformLoginMethodBody,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)],
    ) -> JSONResponse:
        try:
            _bound_service(request, service)
            if len(request.headers.getlist("authorization")) != 1:
                raise MossworkError.localized(ErrorCode.AUTH_FAILED)
            if request.query_params:
                raise MossworkError.localized(
                    ErrorCode.IDENTITY_REQUEST_INVALID,
                    status=400,
                    details={"reason": "invalid_request"},
                )
            await service.set_method(_access(credentials), body.login_method)
            _bound_service(request, service)
            return JSONResponse({"success": True}, headers={"Cache-Control": "no-store"})
        except MossworkError as error:
            return _error(request, error)

    return router
