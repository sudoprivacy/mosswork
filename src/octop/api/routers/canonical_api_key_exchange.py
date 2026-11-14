"""Injected machine exchange endpoints; never authenticate a human or local user."""

from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from octop.api.routers.canonical_auth import _CanonicalAuthRoute, _error
from octop.infra.errors import MossworkError
from octop.infra.identity.api_key_exchange import CanonicalApiKeyExchangeService


class MachineExchangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    api_key: str = Field(min_length=1, repr=False)


class MachineRefreshBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    refresh_token: str = Field(min_length=1, repr=False)


class CanonicalMachineTokensResponse(BaseModel):
    access_token: str = Field(repr=False)
    refresh_token: str = Field(repr=False)
    expires_in: int
    token_type: str
    organization_id: str
    member_id: str
    key_id: str
    scopes: tuple[str, ...]


def build_canonical_api_key_exchange_router(service: CanonicalApiKeyExchangeService) -> APIRouter:
    router = APIRouter(route_class=_CanonicalAuthRoute)

    @router.post(
        "/api-key/exchange",
        response_model=CanonicalMachineTokensResponse,
        openapi_extra={"security": []},
        summary="Exchange an existing canonical machine API key",
        description=(
            "Validates the raw key and its current original owner under exclusive writer "
            "admission. Uses the same controlled issuer as human login, with a separate "
            "machine credential family. No human login, local user, native or resource "
            "authorization is created. Does not provision model or billing credentials."
        ),
    )
    async def exchange(request: Request, body: MachineExchangeBody) -> JSONResponse:
        try:
            result = await service.exchange(body.api_key)
            return JSONResponse(
                CanonicalMachineTokensResponse(**asdict(result)).model_dump(mode="json"),
                headers={"Cache-Control": "no-store"},
            )
        except MossworkError as error:
            return _error(request, error)

    @router.post(
        "/api-key/refresh",
        response_model=CanonicalMachineTokensResponse,
        openapi_extra={"security": []},
        summary="Refresh a retained canonical machine credential",
        description=(
            "Requires an original machine refresh JWT, current owner/account and a "
            "nonrevoked JTI. Retains its signed scopes and uses the current owner role. "
            "Raw-key revocation prevents new exchange; it does not revoke existing JWTs."
        ),
    )
    async def refresh(request: Request, body: MachineRefreshBody) -> JSONResponse:
        try:
            result = await service.refresh(body.refresh_token)
            return JSONResponse(
                CanonicalMachineTokensResponse(**asdict(result)).model_dump(mode="json"),
                headers={"Cache-Control": "no-store"},
            )
        except MossworkError as error:
            return _error(request, error)

    return router
