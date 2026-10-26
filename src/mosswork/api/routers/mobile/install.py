"""Remote Android container install (SSE)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from mosswork.api.deps import require_permission
from mosswork.infra.mobile.setup import install_mobile_stream
from mosswork.infra.users.identity import User
from mosswork.infra.utils.locale import resolve_request_locale

router = APIRouter()


@router.post("/mobile/install")
async def install_mobile(
    request: Request,
    _user: User = Depends(require_permission("mobile")),
) -> StreamingResponse:
    locale = resolve_request_locale(request)

    async def _gen() -> AsyncIterator[str]:
        async for event in install_mobile_stream(locale=locale):
            yield event

    return StreamingResponse(_gen(), media_type="text/event-stream")
