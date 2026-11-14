"""Thin HTTP adapters for the maintained canonical human login lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from octop.api.routers.canonical_identity import _BEARER
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.human_sessions import CanonicalHumanSessionService, CanonicalSessionTokens
from octop.infra.identity.phone_auth import PhoneAuthError
from octop.infra.utils.locale import resolve_request_locale

_NO_STORE = {"Cache-Control": "no-store"}


class _CanonicalAuthRoute(APIRoute):
    """Keep rejected credential bodies out of validation responses."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return _error(
                    request,
                    MossworkError.localized(
                        ErrorCode.IDENTITY_REQUEST_INVALID,
                        status=422,
                        details={"reason": "invalid_request"},
                    ),
                )

        return handle


class IdentityStatusResponse(BaseModel):
    mode: Literal["standalone", "canonical"]
    phone_login_available: bool
    password_login_available: bool


class PhoneCodeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(min_length=1, max_length=64)


class PhoneLoginBody(PhoneCodeBody):
    code: str = Field(min_length=1, max_length=32)


class RefreshBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=1, max_length=16_384)


class SwitchOrganizationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: str = Field(min_length=1, max_length=256)


class LogoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str | None = Field(default=None, min_length=1, max_length=16_384)


class PhoneCodeResponse(BaseModel):
    next_send_in: int
    delivery: Literal["tencent"]


class CanonicalTokensResponse(BaseModel):
    access_token: str = Field(repr=False)
    refresh_token: str = Field(repr=False)
    expires_in: int
    token_type: str
    account_id: str
    organization_id: str
    member_id: str


def _tokens(result: CanonicalSessionTokens) -> JSONResponse:
    payload = CanonicalTokensResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        token_type=result.token_type,
        account_id=result.account_id,
        organization_id=result.organization_id,
        member_id=result.member_id,
    )
    return JSONResponse(payload.model_dump(), headers=_NO_STORE)


def _error(request: Request, error: MossworkError | PhoneAuthError) -> JSONResponse:
    headers = dict(_NO_STORE)
    if isinstance(error, PhoneAuthError):
        if error.retry_after_sec is not None:
            headers["Retry-After"] = str(error.retry_after_sec)
        error = MossworkError.localized(
            ErrorCode.PHONE_AUTH_FAILED,
            status=error.status,
            details={"reason": error.reason},
        )
    return JSONResponse(
        status_code=error.status,
        content=error.to_envelope(locale=resolve_request_locale(request)),
        headers=headers,
    )


def _access(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise MossworkError.localized(ErrorCode.AUTH_FAILED)
    return credentials.credentials


def build_identity_status_router(
    *,
    canonical_enabled: bool,
    phone_login_available: bool | Callable[[], bool],
    password_login_available: bool = False,
) -> APIRouter:
    """Publish only the trusted composition mode, including before local setup."""
    router = APIRouter()

    @router.get(
        "/status",
        response_model=IdentityStatusResponse,
        summary="Read the configured human authentication mode",
        description=(
            "Public boot metadata. Canonical mode uses the original human authority and "
            "does not require creating a local administrator. Availability means the "
            "lifecycle provider and effective phone setting permit it. Actual delivery "
            "and writer readiness are checked by the lifecycle operation."
        ),
    )
    async def status(request: Request) -> JSONResponse:
        try:
            available = (
                phone_login_available()
                if callable(phone_login_available)
                else phone_login_available
            )
            if type(available) is not bool:
                raise MossworkError.localized(ErrorCode.INTERNAL_ERROR, status=503)
            return JSONResponse(
                IdentityStatusResponse(
                    mode="canonical" if canonical_enabled else "standalone",
                    phone_login_available=available,
                    password_login_available=password_login_available,
                ).model_dump(),
                headers=_NO_STORE,
            )
        except MossworkError as error:
            return _error(request, error)

    return router


def build_canonical_auth_router(service: CanonicalHumanSessionService) -> APIRouter:
    """Install one trusted issuer; requests cannot select a DB, issuer or writer."""
    router = APIRouter(route_class=_CanonicalAuthRoute)

    @router.post(
        "/phone/send-code", response_model=PhoneCodeResponse, summary="Send a phone login code"
    )
    async def send_code(request: Request, body: PhoneCodeBody) -> JSONResponse:
        try:
            sent = await service.send_code(body.phone)
            return JSONResponse(
                PhoneCodeResponse(
                    next_send_in=sent.next_send_in, delivery=sent.delivery
                ).model_dump(),
                headers=_NO_STORE,
            )
        except (MossworkError, PhoneAuthError) as error:
            return _error(request, error)

    @router.post(
        "/phone/login", response_model=CanonicalTokensResponse, summary="Sign in with a phone code"
    )
    async def login(request: Request, body: PhoneLoginBody) -> JSONResponse:
        try:
            return _tokens(await service.login(body.phone, body.code))
        except (MossworkError, PhoneAuthError) as error:
            return _error(request, error)

    @router.post(
        "/refresh",
        response_model=CanonicalTokensResponse,
        summary="Refresh a canonical human login",
    )
    async def refresh(request: Request, body: RefreshBody) -> JSONResponse:
        try:
            return _tokens(await service.refresh(body.refresh_token))
        except MossworkError as error:
            return _error(request, error)

    @router.post(
        "/switch-organization",
        response_model=CanonicalTokensResponse,
        summary="Select another existing membership of this verified phone account",
        responses={401: {"description": "Current human login required"}},
    )
    async def switch_organization(
        request: Request,
        body: SwitchOrganizationBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            return _tokens(
                await service.switch_organization(_access(credentials), body.organization_id)
            )
        except MossworkError as error:
            return _error(request, error)

    @router.post("/logout", status_code=204, summary="Revoke this canonical login's credentials")
    async def logout(
        request: Request,
        body: LogoutBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> Response:
        try:
            access_token = _access(credentials)
            # Preserve the original authenticated HTTP boundary. The internal
            # revocation domain also supports credential cleanup; an arbitrary
            # Bearer value is not a public human login.
            await service.snapshots.snapshot(access_token)
            await service.logout(access_token, body.refresh_token)
            return Response(status_code=204, headers=_NO_STORE)
        except MossworkError as error:
            return _error(request, error)

    return router
