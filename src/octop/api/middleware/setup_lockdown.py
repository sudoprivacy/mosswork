"""Lock down all non-/setup endpoints while no users exist.

Returns ``503 {"setup_required": true}`` so the SPA can hard-redirect
to ``/setup``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from octop.api.deps import is_canonical_human_review_path, is_canonical_identity_request

_OPEN_PREFIXES = (
    "/api/setup/",
    "/api/health/",
)


def install(app: Any, server: Any) -> None:
    @app.middleware("http")  # type: ignore[untyped-decorator]
    async def _lockdown(
        request: Request,
        call_next: Callable[[Request], Awaitable[Any]],
    ) -> Any:
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        cfg = server.services.config if server.services else getattr(server, "config", None)
        open_exact = ["/api/health", "/api/identity/status"]
        if cfg and cfg.enable_api_docs:
            open_exact.extend(("/api/docs", "/api/openapi.json"))
        if getattr(server, "canonical_configuration", None) is not None:
            if path in open_exact or is_canonical_identity_request(request):
                response = await call_next(request)
                # A capture may already have committed. Withhold its page if
                # the configured binding was withdrawn during awaited work.
                if is_canonical_human_review_path(request) and not is_canonical_identity_request(
                    request
                ):
                    return JSONResponse(
                        status_code=503,
                        content={"canonical_resource_unavailable": True},
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                    )
                return response
            return JSONResponse(status_code=503, content={"canonical_resource_unavailable": True})
        if path in open_exact or any(path.startswith(p) for p in _OPEN_PREFIXES):
            return await call_next(request)
        # Canonical human login does not create a standalone local admin.
        # This does not open any adjacent product or setup endpoint.
        if is_canonical_identity_request(request):
            return await call_next(request)
        if server.user_manager is None or server.user_manager.count() == 0:
            return JSONResponse(
                status_code=503,
                content={"setup_required": True},
            )
        return await call_next(request)
