"""Normal and dedicated Moss password routes use the same canonical issuer."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from octop.api.routers.canonical_auth import (
    CanonicalTokensResponse,
    _CanonicalAuthRoute,
    _error,
    _tokens,
)
from octop.infra.errors import MossworkError
from octop.infra.identity.password_auth import CanonicalPasswordAuthService


class CanonicalPasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str | None = Field(default=None, repr=False)
    email: str | None = Field(default=None, repr=False)
    password: str = Field(min_length=1, repr=False)


def build_canonical_password_router(service: CanonicalPasswordAuthService) -> APIRouter:
    router = APIRouter(route_class=_CanonicalAuthRoute)

    @router.post(
        "/password/login",
        response_model=CanonicalTokensResponse,
        summary="Sign in under the organization's current password policy",
        description=(
            "Verifies the existing canonical password and active human membership. "
            "Requires the current organization's effective password login policy. "
            "Uses the configured canonical issuer and exclusive writer; creates no account."
        ),
    )
    async def login(request: Request, body: CanonicalPasswordBody) -> JSONResponse:
        try:
            return _tokens(await service.login_sudowork(**body.model_dump(exclude_none=True)))
        except MossworkError as error:
            return _error(request, error)

    @router.post(
        "/moss/password/login",
        response_model=CanonicalTokensResponse,
        summary="Sign in to the dedicated Moss password realm",
        description=(
            "Preserves the existing dedicated Moss login independently of the organization's "
            "SMS/password policy. Current Moss, resource, model and native execution grants "
            "remain required after login. Does not grant other company memberships."
        ),
    )
    async def login_moss(request: Request, body: CanonicalPasswordBody) -> JSONResponse:
        try:
            return _tokens(await service.login_moss(**body.model_dump(exclude_none=True)))
        except MossworkError as error:
            return _error(request, error)

    return router
