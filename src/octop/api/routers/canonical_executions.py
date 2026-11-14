"""Fresh human authentication for the same original unsent queue request."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from octop.api.common.canonical_identity import _BEARER
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.execution.admission import CanonicalRunAdmissions, CurrentAdmissionRequired
from octop.infra.execution.service import ExecutionService
from octop.infra.identity.projection import CanonicalRequestIdentityResolver
from octop.infra.utils.locale import resolve_request_locale

if TYPE_CHECKING:
    from octop.infra.server import MossworkServer


def build_canonical_execution_router(
    server: MossworkServer,
    resolver: CanonicalRequestIdentityResolver,
    admissions: CanonicalRunAdmissions,
) -> APIRouter:
    """Use the configured human verifier and the existing server-owned queue.

    This authenticates a caller without allocating a Product user. The gate
    reads the original actor from trusted storage and permits only the same
    never-claimed request. No input, effect, native cleanup or delivery ACK is
    supplied by this endpoint.
    """
    router = APIRouter()

    def require_current_queue() -> None:
        execution = server.execution
        services = server.services
        if (
            not server._started
            or type(execution) is not ExecutionService
            or execution.human_admissions is not admissions
            or execution.repo is not admissions.repo
            or services is None
            or services.db is not admissions.repo.db
            or resolver._projections._db is not admissions.repo.db
            or server._managed_backend is None
        ):
            raise MossworkError.localized(ErrorCode.INTERNAL_ERROR, status=503)

    @router.post(
        "/{run_id}/reauthenticate",
        status_code=204,
        response_class=Response,
        summary="Reauthenticate the same unsent execution after a process restart",
        responses={
            401: {"description": "Current human credential required"},
            403: {"description": "Current actor or projection does not match the original"},
            409: {"description": "Original request cannot be reauthenticated"},
            503: {"description": "Configured admission is unavailable"},
        },
    )
    async def reauthenticate(
        run_id: str,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> Response:
        try:
            require_current_queue()
            if len(request.headers.getlist("authorization")) != 1 or credentials is None:
                raise MossworkError.localized(ErrorCode.AUTH_FAILED)

            # Verify before reading any run, without the allocating authenticate
            # helper. The bridge repeats fresh verification against the stored
            # original actor after its queue/storage waits.
            await resolver._snapshots.snapshot(credentials.credentials)
            require_current_queue()
            await admissions.reauthenticate_human(
                run_id, credentials.credentials, resolver=resolver
            )
            require_current_queue()
        except CurrentAdmissionRequired:
            error = MossworkError.localized(ErrorCode.FORBIDDEN, status=409)
        except MossworkError as caught:
            error = caught
        except Exception:
            # Private storage/verifier diagnostics must not become HTTP text.
            # CancelledError remains cancellation; it cannot become a success.
            error = MossworkError.localized(ErrorCode.INTERNAL_ERROR, status=503)
        else:
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        return JSONResponse(
            status_code=error.status,
            content=error.to_envelope(locale=resolve_request_locale(request)),
            headers={"Cache-Control": "no-store"},
        )

    return router
