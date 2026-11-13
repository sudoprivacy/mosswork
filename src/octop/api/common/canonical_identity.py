"""Bearer-only dependency for explicitly adapted canonical effect routes.

Trusted app composition sets app.state.canonical_requests to the concrete
resolver. Read-only identity/me deliberately keeps its existing snapshot path;
this allocation dependency is not a global current_user or middleware bypass.
"""

from __future__ import annotations

from fastapi import Request, WebSocket
from fastapi.security import HTTPBearer

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.projection import CanonicalRequestIdentityResolver
from octop.infra.identity.request_principal import CanonicalAuthenticatedRequest, authenticate

_BEARER = HTTPBearer(
    scheme_name="CanonicalRequestBearer",
    description="Canonical human Bearer verified by the explicitly injected request resolver.",
    auto_error=False,
)


async def canonical_authenticated_request(request: Request) -> CanonicalAuthenticatedRequest:
    """Use one Authorization header only; never accept query/body/cookie/meta credentials."""
    if len(request.headers.getlist("authorization")) != 1:
        raise MossworkError.localized(ErrorCode.AUTH_FAILED)
    credentials = await _BEARER(request)
    if credentials is None:
        raise MossworkError.localized(ErrorCode.AUTH_FAILED)
    resolver = getattr(request.app.state, "canonical_requests", None)
    if type(resolver) is not CanonicalRequestIdentityResolver:
        raise MossworkError.localized(
            ErrorCode.INTERNAL_ERROR,
            status=503,
            details={"dependency": "canonical_request_resolver"},
        )
    return await authenticate(resolver, credentials.credentials)


async def canonical_authenticated_websocket(websocket: WebSocket) -> CanonicalAuthenticatedRequest:
    """Verify the existing chat WebSocket's single opaque ``token`` parameter.

    Browser WebSockets use the current chat transport rather than an HTTP
    Authorization dependency. This does not relax HTTP authentication, accept
    frame metadata as a user, mount a route or authorize an Agent/session.
    The adapted consumer retains this sealed principal outside serialized
    messages, revalidates after waits and applies current resource policy.
    """
    tokens = websocket.query_params.getlist("token")
    if (
        len(tokens) != 1
        or not tokens[0]
        or websocket.headers.getlist("authorization")
        or websocket.query_params.getlist("access_token")
    ):
        raise MossworkError.localized(ErrorCode.AUTH_FAILED)
    resolver = getattr(websocket.app.state, "canonical_requests", None)
    if type(resolver) is not CanonicalRequestIdentityResolver:
        raise MossworkError.localized(
            ErrorCode.INTERNAL_ERROR,
            status=503,
            details={"dependency": "canonical_request_resolver"},
        )
    return await authenticate(resolver, tokens[0])
