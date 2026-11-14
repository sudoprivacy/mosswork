"""Canonical API-key management, kept separate from human login adapters."""

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from octop.api.routers.canonical_auth import _access, _CanonicalAuthRoute, _error
from octop.api.routers.canonical_identity import _BEARER
from octop.infra.errors import MossworkError
from octop.infra.identity.canonical_api_keys import CanonicalApiKeyService

_NO_STORE = {"Cache-Control": "no-store"}


class CanonicalApiKeyResponse(BaseModel):
    id: str
    organization_id: str
    member_id: str
    name: str
    prefix: str
    scopes: tuple[str, ...]
    status: Literal["active", "revoked"]
    created_at: int
    last_used_at: int | None


class CanonicalApiKeysResponse(BaseModel):
    api_keys: list[CanonicalApiKeyResponse]


class CreateCanonicalApiKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    scopes: list[str] = Field(min_length=1)


class CreatedCanonicalApiKeyResponse(BaseModel):
    api_key: CanonicalApiKeyResponse
    plain_text_key: str = Field(repr=False, description="Returned only by this creation operation.")


def build_canonical_api_keys_router(service: CanonicalApiKeyService) -> APIRouter:
    router = APIRouter(route_class=_CanonicalAuthRoute)

    @router.get(
        "/api-keys",
        response_model=CanonicalApiKeysResponse,
        summary="List API keys visible to the current organization administrator",
        description=(
            "Requires signed admin:api_keys and current organization or department authority. "
            "Returns safe key metadata, never plaintext secrets or stored hashes."
        ),
    )
    async def list_keys(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            keys = await service.list_keys(_access(credentials))
            return JSONResponse(
                CanonicalApiKeysResponse(
                    api_keys=[CanonicalApiKeyResponse(**asdict(key)) for key in keys]
                ).model_dump(),
                headers=_NO_STORE,
            )
        except MossworkError as error:
            return _error(request, error)

    @router.post(
        "/api-keys",
        response_model=CreatedCanonicalApiKeyResponse,
        summary="Create a canonical machine API key for an authorized member",
        description=(
            "Requires the current signed admin:api_keys management scope and exclusive writer. "
            "Current organization/department visibility and issuer scope limits apply. The "
            "plaintext key is returned once; only its hash is stored. This creates neither a "
            "human login nor native owner credentials."
        ),
    )
    async def create_key(
        request: Request,
        body: CreateCanonicalApiKeyBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            result = await service.create_key(
                _access(credentials), body.member_id, body.name, body.scopes
            )
            return JSONResponse(
                CreatedCanonicalApiKeyResponse(
                    api_key=CanonicalApiKeyResponse(**asdict(result.api_key)),
                    plain_text_key=result.plain_text_key,
                ).model_dump(),
                headers=_NO_STORE,
            )
        except MossworkError as error:
            return _error(request, error)

    @router.delete(
        "/api-keys/{key_id}",
        status_code=204,
        summary="Revoke an authorized member's raw API key",
        description=(
            "Requires current signed admin:api_keys management authority and exclusive writer. "
            "Prevents new raw-key use. Preserves original already-issued token expiry/revocation "
            "semantics; does not claim immediate cancellation of every issued machine token."
        ),
    )
    async def revoke_key(
        request: Request,
        key_id: str,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> Response:
        try:
            await service.revoke_key(_access(credentials), key_id)
            return Response(status_code=204, headers=_NO_STORE)
        except MossworkError as error:
            return _error(request, error)

    return router
