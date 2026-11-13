"""Canonical member entitlement and self Feishu binding HTTP adapters."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from octop.api.routers.canonical_auth import _access, _CanonicalAuthRoute, _error
from octop.api.routers.canonical_identity import _BEARER
from octop.infra.agents.personal_moss_access import MossAccessDecision
from octop.infra.errors import MossworkError
from octop.infra.gateway.feishu_binding import CanonicalFeishuBindingService
from octop.infra.gateway.feishu_delivery import CanonicalFeishuDeliveryService
from octop.infra.identity.moss_entitlements import CanonicalMossEntitlementService

_NO_STORE = {"Cache-Control": "no-store"}


class MossEntitlementBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_enabled: StrictBool


class MossEntitlementResponse(BaseModel):
    organization_id: str
    member_id: str
    is_enabled: bool
    is_granted: bool
    source: Literal["role", "explicit"]
    member_status: str


def _decision(decision: MossAccessDecision) -> JSONResponse:
    return JSONResponse(
        MossEntitlementResponse(
            organization_id=decision.organization_id,
            member_id=decision.owner_id,
            is_enabled=decision.is_enabled,
            is_granted=decision.is_granted,
            source=decision.source,
            member_status=decision.member_status,
        ).model_dump(),
        headers=_NO_STORE,
    )


def build_canonical_entitlement_router(service: CanonicalMossEntitlementService) -> APIRouter:
    router = APIRouter(route_class=_CanonicalAuthRoute)

    @router.get(
        "/members/{member_id}/moss-access",
        response_model=MossEntitlementResponse,
        summary="Read an organization member's personal Moss entitlement",
        description="Requires the current selected-organization administrator and signed admin:users scope.",
    )
    async def get_access(
        request: Request,
        member_id: str,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            return _decision(await service.get_access(_access(credentials), member_id))
        except MossworkError as error:
            return _error(request, error)

    @router.put(
        "/members/{member_id}/moss-access",
        response_model=MossEntitlementResponse,
        summary="Grant or revoke an organization member's personal Moss use",
        description=(
            "Requires the current selected-organization administrator and signed admin:users scope. "
            "Explicit revocation also overrides administrator defaults. Does not grant data, "
            "model or native session access. Requires the exclusive canonical writer."
        ),
    )
    async def set_access(
        request: Request,
        member_id: str,
        body: MossEntitlementBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            return _decision(
                await service.set_access(_access(credentials), member_id, body.is_enabled)
            )
        except MossworkError as error:
            return _error(request, error)

    return router


class FeishuBindingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=32, max_length=32, pattern=r"^[A-Za-z0-9_-]{32}$", repr=False)


class FeishuBindingResponse(BaseModel):
    id: str
    sender_id: str
    plugin_id: str
    app_id: str
    is_current: bool


class FeishuBindingsResponse(BaseModel):
    bindings: list[FeishuBindingResponse]


def build_canonical_feishu_binding_router(service: CanonicalFeishuBindingService) -> APIRouter:
    router = APIRouter(route_class=_CanonicalAuthRoute)

    @router.get(
        "/feishu/bindings",
        response_model=FeishuBindingsResponse,
        summary="List this human's retained bindings for the configured Feishu bot",
    )
    async def bindings(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            current = await service.bindings(_access(credentials))
            return JSONResponse(
                FeishuBindingsResponse(
                    bindings=[
                        FeishuBindingResponse(
                            id=item.id,
                            sender_id=item.sender_id,
                            plugin_id=item.plugin_id,
                            app_id=item.app_id,
                            is_current=item.is_current,
                        )
                        for item in current
                    ]
                ).model_dump(),
                headers=_NO_STORE,
            )
        except MossworkError as error:
            return _error(request, error)

    @router.post(
        "/feishu/bind",
        status_code=204,
        summary="Link the privately verified Feishu sender to this human's personal Moss",
        description=(
            "Supply the one-time private code received from the registered bot. "
            "The current human must have Moss access in the same selected organization. "
            "Does not create a replacement native session or grant model access."
        ),
    )
    async def bind(
        request: Request,
        body: FeishuBindingBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> Response:
        try:
            await service.bind(_access(credentials), body.code)
            return Response(status_code=204, headers=_NO_STORE)
        except MossworkError as error:
            return _error(request, error)

    @router.delete(
        "/feishu/bindings/{binding_id}",
        status_code=204,
        summary="Unlink this human's Feishu binding",
        description="Allows unlinking after Moss-use revocation. Current human login is still required.",
    )
    async def unlink(
        request: Request,
        binding_id: str,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> Response:
        try:
            await service.unlink(_access(credentials), binding_id)
            return Response(status_code=204, headers=_NO_STORE)
        except MossworkError as error:
            return _error(request, error)

    return router


class FeishuConnectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def build_canonical_feishu_connection_router(service: CanonicalFeishuDeliveryService) -> APIRouter:
    router = APIRouter(route_class=_CanonicalAuthRoute)

    @router.put(
        "/feishu/connection",
        status_code=204,
        summary="Configure the registered Feishu bot's organization tenant",
        description=(
            "Requires the current same-organization administrator and signed admin:users scope. "
            "The app, plugin, connection owner and authority are selected by trusted server "
            "configuration. Preserves old bindings and delivery receipts; changed tenant pins "
            "invalidate old routes. Does not create a native Agent or session."
        ),
    )
    async def configure(
        request: Request,
        body: FeishuConnectionBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> Response:
        try:
            await service.configure(_access(credentials), body.tenant_key)
            return Response(status_code=204, headers=_NO_STORE)
        except MossworkError as error:
            return _error(request, error)

    return router
