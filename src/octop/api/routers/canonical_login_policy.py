"""Thin adapters for the selected organization's canonical login policy."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict

from octop.api.routers.canonical_auth import _access, _CanonicalAuthRoute, _error
from octop.api.routers.canonical_identity import _BEARER
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.organization_login_policy import (
    CanonicalOrganizationLoginPolicyService,
)

_NO_STORE = {"Cache-Control": "no-store"}


class OrganizationLoginMethodBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    login_method: Literal["sms", "password", "cas"]


class OrganizationLoginPolicyUpdatedResponse(BaseModel):
    success: Literal[True] = True


def _bound_service(request: Request, service: CanonicalOrganizationLoginPolicyService) -> None:
    if (
        type(service) is not CanonicalOrganizationLoginPolicyService
        or getattr(request.app.state, "canonical_organization_login_policy", None) is not service
        or getattr(request.app.state, "canonical_identity", None) is not service._snapshots
    ):
        raise MossworkError.localized(
            ErrorCode.INTERNAL_ERROR,
            status=503,
            details={"dependency": "canonical_organization_login_policy"},
        )


def _selected_access(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    service: CanonicalOrganizationLoginPolicyService,
) -> str:
    _bound_service(request, service)
    if len(request.headers.getlist("authorization")) != 1:
        raise MossworkError.localized(ErrorCode.AUTH_FAILED)
    if request.query_params:
        raise MossworkError.localized(
            ErrorCode.IDENTITY_REQUEST_INVALID,
            status=400,
            details={"reason": "invalid_request"},
        )
    return _access(credentials)


def build_canonical_organization_login_policy_router(
    service: CanonicalOrganizationLoginPolicyService,
) -> APIRouter:
    """Bind the same explicitly constructed business service at the launch root."""
    router = APIRouter(route_class=_CanonicalAuthRoute)

    @router.put(
        "/organization/login-policy",
        response_model=OrganizationLoginPolicyUpdatedResponse,
        summary="Set the selected organization's login method",
        description=(
            "Requires the current selected-organization administrator, signed administrative "
            "ceiling, canonical writer admission and configured SMS or CAS readiness. "
            "Preserves unrelated policy fields and the original organization identity."
        ),
    )
    async def set_method(
        request: Request,
        body: OrganizationLoginMethodBody,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)],
    ) -> JSONResponse:
        try:
            await service.set_method(
                _selected_access(request, credentials, service), body.login_method
            )
            _bound_service(request, service)
            return JSONResponse({"success": True}, headers=_NO_STORE)
        except MossworkError as error:
            return _error(request, error)

    @router.delete(
        "/organization/login-policy",
        response_model=OrganizationLoginPolicyUpdatedResponse,
        summary="Restore the selected organization's inherited login method",
        description=(
            "Removes the selected organization's login-method override under the current "
            "canonical administrator and writer. Retains its unrelated policy fields."
        ),
    )
    async def inherit(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)],
    ) -> JSONResponse:
        try:
            token = _selected_access(request, credentials, service)
            if await request.body():
                raise MossworkError.localized(
                    ErrorCode.IDENTITY_REQUEST_INVALID,
                    status=400,
                    details={"reason": "invalid_request"},
                )
            _bound_service(request, service)
            await service.inherit(token)
            _bound_service(request, service)
            return JSONResponse({"success": True}, headers=_NO_STORE)
        except MossworkError as error:
            return _error(request, error)

    return router
