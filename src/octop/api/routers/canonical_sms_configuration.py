"""Explicit canonical managed-SMS metadata/save HTTP consumer.

The product composition owns the one mount and middleware registration. This
factory receives its existing same-business service; it never creates a vault,
writer, local user or runtime. Saved metadata is not active provider readiness.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from octop.api.routers.canonical_auth import _access, _CanonicalAuthRoute, _error
from octop.api.routers.canonical_identity import _BEARER
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.sms_configuration_updates import CanonicalManagedSmsConfigurationService


class ManagedSmsConfigurationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: str | None
    configuration: dict[str, object]
    secrets: dict[str, SecretStr | None] = Field(default_factory=dict, repr=False)


class SavedSmsConfigurationResponse(BaseModel):
    managed: bool
    saved_version: str | None
    configuration: dict[str, bool | int | str | list[str]] | None
    secret_references_present: dict[str, bool]
    updated_at: int | None


class SmsConfigurationSavedResponse(BaseModel):
    version: str
    restart_required: bool


def _bound_service(request: Request, service: CanonicalManagedSmsConfigurationService) -> None:
    if (
        type(service) is not CanonicalManagedSmsConfigurationService
        or getattr(request.app.state, "canonical_sms_configuration", None) is not service
        or getattr(request.app.state, "canonical_identity", None) is not service._snapshots
    ):
        raise MossworkError.localized(
            ErrorCode.INTERNAL_ERROR,
            status=503,
            details={"dependency": "canonical_sms_configuration"},
        )
    if len(request.headers.getlist("authorization")) != 1:
        raise MossworkError.localized(ErrorCode.AUTH_FAILED)
    if request.query_params:
        raise MossworkError.localized(ErrorCode.IDENTITY_REQUEST_INVALID, status=400)


def build_canonical_sms_configuration_router(
    service: CanonicalManagedSmsConfigurationService,
) -> APIRouter:
    """Bind the configured service; no issuer, provider or authority fallback."""
    router = APIRouter(route_class=_CanonicalAuthRoute)

    @router.get(
        "/platform/sms-configuration",
        response_model=SavedSmsConfigurationResponse,
        summary="Read saved managed SMS configuration metadata",
        description="Requires current platform administration. Secret-reference flags do not prove vault availability or runtime activation.",
    )
    async def inspect_saved(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)],
    ) -> JSONResponse:
        try:
            _bound_service(request, service)
            saved = await service.inspect_saved(_access(credentials))
            _bound_service(request, service)
            return JSONResponse(
                {
                    "managed": saved is not None,
                    "saved_version": saved.version if saved else None,
                    "configuration": dict(saved.configuration) if saved else None,
                    "secret_references_present": {
                        key: bool(saved and key in saved.secret_references)
                        for key in ("secretId", "secretKey")
                    },
                    "updated_at": saved.updated_at if saved else None,
                },
                headers={"Cache-Control": "no-store"},
            )
        except MossworkError as error:
            return _error(request, error)

    @router.put(
        "/platform/sms-configuration",
        response_model=SmsConfigurationSavedResponse,
        summary="Save an existing managed SMS configuration",
        description="Requires current canonical super administrator and signed administrative ceiling, original vault and exclusive writer. Returns a saved version requiring restart; does not activate a runtime or take over unmanaged legacy settings.",
    )
    async def save(
        request: Request,
        body: ManagedSmsConfigurationBody,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)],
    ) -> JSONResponse:
        try:
            _bound_service(request, service)
            result = await service.save(
                _access(credentials),
                expected_version=body.expected_version,
                configuration=body.configuration,
                secrets={
                    key: value.get_secret_value() if value is not None else None
                    for key, value in body.secrets.items()
                },
            )
            _bound_service(request, service)
            return JSONResponse(
                {"version": result.version, "restart_required": result.restart_required},
                headers={"Cache-Control": "no-store"},
            )
        except MossworkError as error:
            return _error(request, error)

    return router
