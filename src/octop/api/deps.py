"""FastAPI Depends, JWT helpers, and auth routing."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from fastapi import Depends, Header, Query, Request

from octop.infra.auth.tokens import InvalidToken as InvalidToken
from octop.infra.auth.tokens import TokenExpired as TokenExpired
from octop.infra.auth.tokens import decode_token as decode_token
from octop.infra.auth.tokens import session_id
from octop.infra.auth.tokens import sign_token as sign_token
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import CanonicalSnapshotService
from octop.infra.users.permissions import PERMISSIONS, user_has_permission

if TYPE_CHECKING:
    from octop.infra.server import MossworkServer
    from octop.infra.users.identity import User
else:
    from octop.infra.users.identity import User


# Sliding renew: when remaining life is below this fraction of configured TTL,
# middleware issues a fresh access token via ACCESS_TOKEN_RESPONSE_HEADER.
ACCESS_TOKEN_RESPONSE_HEADER = "X-mosswork-Access-Token"
_SLIDING_RENEW_REMAINING_FRACTION = 1 / 3


# Paths that bypass JWT middleware (setup wizard, health, login).
_JWT_EXEMPT_PREFIXES = (
    "/api/company-runtime/",
    "/api/setup/",
    "/api/health/",
    "/api/i18n/",
    "/api/connectors/oauth/callback",
    "/api/internal/mcp/",
)
_JWT_EXEMPT_EXACT = (
    "/api/identity/status",
    "/api/health",
    "/api/auth/login",
    "/api/auth/captcha",
    "/api/auth/client-downloads",
    "/api/auth/oidc/status",
    "/api/auth/oidc/start",
    "/api/auth/oidc/callback",
    "/api/auth/oidc/exchange",
    "/api/auth/oauth/status",
    "/api/auth/oauth/start",
    "/api/auth/oauth/callback",
    "/api/auth/oauth/exchange",
    "/api/auth/ldap/status",
    "/api/auth/invite/validate",
    "/api/auth/invite/redeem",
    "/api/docs",
    "/api/openapi.json",
)


def is_jwt_exempt_path(path: str) -> bool:
    return path in _JWT_EXEMPT_EXACT or any(path.startswith(p) for p in _JWT_EXEMPT_PREFIXES)


def is_canonical_human_review_path(request: Request) -> bool:
    return (
        request.method in {"GET", "POST"}
        and re.fullmatch(
            r"/api/identity/ontology-projects/[a-zA-Z0-9_-]{1,128}/reviews/[a-f0-9]{32}/v1",
            request.url.path,
        )
        is not None
    ) or (
        request.method == "POST"
        and re.fullmatch(
            r"/api/identity/ontology-projects/[a-zA-Z0-9_-]{1,128}/reviews/[a-f0-9]{32}/v1/page",
            request.url.path,
        )
        is not None
    )


def is_canonical_identity_request(request: Request) -> bool:
    """Exact injected canonical routes that own their human credential checks."""
    if is_canonical_human_review_path(request):
        from octop.infra.db.repos.retained_review import RetainedHumanReviewDocumentRepo
        from octop.infra.identity.projection import CanonicalRequestIdentityResolver

        configured = getattr(getattr(request.app.state, "mosswork_server", None), "canonical", None)
        ports = getattr(request.app.state, "canonical_human_review_ports", None)
        return (
            type(ports) is tuple
            and len(ports) == 4
            and configured is not None
            and ports is configured.human_review_ports
            and callable(ports[0])
            and (
                not request.url.path.endswith("/page")
                or callable(getattr(ports[0], "read_page", None))
            )
            and ports[1] is configured.business.requests
            and ports[1] is getattr(request.app.state, "canonical_requests", None)
            and type(ports[1]) is CanonicalRequestIdentityResolver
            and type(ports[2]) is RetainedHumanReviewDocumentRepo
            # The snapshot reader appends its read-only SQLite mode to the
            # same original URI used by the immutable document repository.
            and ports[2]._uri + "?mode=ro" == configured.business.snapshots._store._uri
            and ports[1]._snapshots is getattr(request.app.state, "canonical_identity", None)
            and callable(getattr(ports[3], "assert_current_writer", None))
            and request.scope.get("raw_path") == request.url.path.encode("ascii")
        )
    if request.method in {"GET", "PUT"} and request.url.path == (
        "/api/identity/platform/sms-configuration"
    ):
        from octop.infra.identity.sms_configuration_updates import (
            CanonicalManagedSmsConfigurationService,
        )

        service = getattr(request.app.state, "canonical_sms_configuration", None)
        configured = getattr(getattr(request.app.state, "mosswork_server", None), "canonical", None)
        return (
            type(service) is CanonicalManagedSmsConfigurationService
            and configured is not None
            and service is configured.business.managed_sms_configuration
            and service._snapshots is getattr(request.app.state, "canonical_identity", None)
        )
    if request.method == "PUT" and request.url.path == "/api/identity/platform/login-policy":
        from octop.infra.identity.platform_login_policy import CanonicalPlatformLoginPolicyService

        service = getattr(request.app.state, "canonical_platform_login_policy", None)
        configured = getattr(getattr(request.app.state, "mosswork_server", None), "canonical", None)
        return (
            type(service) is CanonicalPlatformLoginPolicyService
            and configured is not None
            and service is configured.business.platform_login_policy
            and service._snapshots is getattr(request.app.state, "canonical_identity", None)
        )
    if request.method in {"PUT", "DELETE"} and request.url.path == (
        "/api/identity/organization/login-policy"
    ):
        from octop.infra.identity.organization_login_policy import (
            CanonicalOrganizationLoginPolicyService,
        )

        service = getattr(request.app.state, "canonical_organization_login_policy", None)
        configured = getattr(getattr(request.app.state, "mosswork_server", None), "canonical", None)
        return (
            type(service) is CanonicalOrganizationLoginPolicyService
            and configured is not None
            and service is configured.business.organization_login_policy
            and service._snapshots is getattr(request.app.state, "canonical_identity", None)
        )
    if request.url.path in {
        "/api/identity/profile",
        "/api/identity/profile/nickname",
        "/api/identity/profile/avatar",
        "/api/identity/password/change",
        "/api/identity/moss/password/change",
    }:
        from octop.infra.identity.self_profile import CanonicalSelfProfileService

        return isinstance(
            getattr(request.app.state, "canonical_profile", None), CanonicalSelfProfileService
        )
    parts = request.url.path.split("/")
    if (
        request.method == "POST"
        and len(parts) == 5
        and parts[:3] == ["", "api", "executions"]
        and parts[3]
        and parts[4] == "reauthenticate"
    ):
        from octop.infra.execution.admission import CanonicalRunAdmissions
        from octop.infra.identity.projection import CanonicalRequestIdentityResolver

        return type(getattr(request.app.state, "canonical_admissions", None)) is (
            CanonicalRunAdmissions
        ) and type(getattr(request.app.state, "canonical_requests", None)) is (
            CanonicalRequestIdentityResolver
        )
    if request.url.path in {"/api/identity/me", "/api/identity/organizations"}:
        return isinstance(
            getattr(request.app.state, "canonical_identity", None), CanonicalSnapshotService
        )
    from octop.infra.gateway.feishu_binding import CanonicalFeishuBindingService
    from octop.infra.gateway.feishu_delivery import CanonicalFeishuDeliveryService
    from octop.infra.identity.canonical_api_keys import CanonicalApiKeyService
    from octop.infra.identity.human_sessions import CanonicalHumanSessionService
    from octop.infra.identity.moss_entitlements import CanonicalMossEntitlementService
    from octop.infra.identity.organization_members import CanonicalOrganizationMemberService
    from octop.infra.identity.password_auth import CanonicalPasswordAuthService

    if request.url.path in {"/api/identity/api-key/exchange", "/api/identity/api-key/refresh"}:
        from octop.infra.identity.api_key_exchange import CanonicalApiKeyExchangeService

        return isinstance(
            getattr(request.app.state, "canonical_api_key_exchange", None),
            CanonicalApiKeyExchangeService,
        )
    parts = request.url.path.split("/")
    if request.url.path == "/api/identity/api-keys" or (
        len(parts) == 5 and parts[:4] == ["", "api", "identity", "api-keys"] and parts[4]
    ):
        return isinstance(
            getattr(request.app.state, "canonical_api_keys", None), CanonicalApiKeyService
        )
    if request.url.path in {"/api/identity/password/login", "/api/identity/moss/password/login"}:
        return isinstance(
            getattr(request.app.state, "canonical_password", None), CanonicalPasswordAuthService
        )
    if (
        request.url.path == "/api/identity/members"
        or (request.method == "GET" and request.url.path == "/api/identity/invitations")
        or (
            request.method == "POST"
            and len(parts) == 6
            and parts[:4] == ["", "api", "identity", "members"]
            and parts[4]
            and parts[5] == "password"
        )
        or (request.method in {"GET", "POST"} and request.url.path == "/api/identity/departments")
        or (
            request.method in {"PATCH", "DELETE"}
            and len(parts) == 5
            and parts[:4] == ["", "api", "identity", "departments"]
            and parts[4]
        )
    ):
        return isinstance(
            getattr(request.app.state, "canonical_members", None),
            CanonicalOrganizationMemberService,
        )
    if request.url.path == "/api/identity/feishu/connection":
        return isinstance(
            getattr(request.app.state, "canonical_feishu_delivery", None),
            CanonicalFeishuDeliveryService,
        )
    if (
        len(parts) == 6
        and parts[:4] == ["", "api", "identity", "members"]
        and parts[4]
        and parts[5] == "moss-access"
    ):
        return isinstance(
            getattr(request.app.state, "canonical_entitlements", None),
            CanonicalMossEntitlementService,
        )
    if request.url.path in {"/api/identity/feishu/bindings", "/api/identity/feishu/bind"} or (
        len(parts) == 6 and parts[:5] == ["", "api", "identity", "feishu", "bindings"] and parts[5]
    ):
        return isinstance(
            getattr(request.app.state, "canonical_feishu_bindings", None),
            CanonicalFeishuBindingService,
        )

    return request.url.path in {
        "/api/identity/phone/send-code",
        "/api/identity/phone/login",
        "/api/identity/refresh",
        "/api/identity/switch-organization",
        "/api/identity/logout",
    } and isinstance(
        getattr(request.app.state, "canonical_sessions", None), CanonicalHumanSessionService
    )


def is_jwt_exempt_request(request: Request) -> bool:
    # This exact endpoint verifies the original canonical human credential.
    # It cannot use the standalone integer-user JWT decoder or sliding issuer.
    if is_canonical_identity_request(request):
        return True
    return is_jwt_exempt_path(request.url.path)


def get_server(request: Request) -> MossworkServer:
    server: MossworkServer = request.app.state.mosswork_server
    if not getattr(server, "_started", False):
        raise MossworkError(ErrorCode.INTERNAL_ERROR, "server not started")
    return server


def require_database(server: MossworkServer) -> None:
    """Raise when the control-plane DB has not been bound yet (deferred setup)."""
    if not server.database_bound:
        raise MossworkError(
            ErrorCode.SETUP_REQUIRED,
            "control-plane database not configured yet",
            status=503,
        )


def extract_raw_token(
    *,
    authorization: str | None = None,
    access_token: str | None = None,
) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if access_token:
        return access_token
    return None


def _decode(server: MossworkServer, token: str) -> dict[str, Any]:
    assert server.services is not None
    secret = server.services.secret_repo.get("jwt")
    if secret is None:
        raise MossworkError(ErrorCode.INTERNAL_ERROR, "jwt secret missing")
    try:
        return decode_token(secret, token)
    except TokenExpired as exc:
        raise MossworkError(ErrorCode.TOKEN_EXPIRED, "token expired") from exc
    except InvalidToken as exc:
        raise MossworkError(ErrorCode.AUTH_FAILED, "invalid token") from exc


def resolve_user_from_token(server: MossworkServer, token: str) -> User:
    if getattr(server, "canonical_configuration", None) is not None:
        raise MossworkError(
            ErrorCode.INTERNAL_ERROR,
            "This local-user resource is unavailable in canonical mode",
            status=503,
        )
    payload = _decode(server, token)
    assert server.services is not None
    if server.services.communication_identity_repo.session_revoked(session_id(token, payload)):
        raise MossworkError(ErrorCode.AUTH_FAILED, "session revoked")
    assert server.user_manager is not None
    user = server.user_manager.get_by_id(int(payload["sub"]))
    if user is None:
        raise MossworkError(ErrorCode.USER_DISABLED, "user not active")
    return user


def maybe_sliding_renew_token(server: MossworkServer, token: str, user: User) -> str | None:
    """Return a fresh access token when ``token`` is past the sliding renew threshold.

    Threshold is one-third of ``access_token_ttl_seconds`` remaining. Active clients
    that pick up ``ACCESS_TOKEN_RESPONSE_HEADER`` stay signed in without re-login.
    """
    assert server.services is not None
    payload = _decode(server, token)
    ttl = int(server.services.config.access_token_ttl_seconds)
    remaining = int(payload["exp"]) - int(time.time())
    threshold = max(1, int(ttl * _SLIDING_RENEW_REMAINING_FRACTION))
    if remaining >= threshold:
        return None
    if server.services.communication_identity_repo.session_revoked(session_id(token, payload)):
        return None
    secret = server.services.secret_repo.get("jwt")
    if secret is None:
        raise MossworkError(ErrorCode.INTERNAL_ERROR, "jwt secret missing")
    return sign_token(
        secret,
        sub=user.id,
        uname=user.username,
        role=user.role,
        ttl_seconds=ttl,
        sid=session_id(token, payload),
    )


def authenticate_request(request: Request, server: MossworkServer) -> User:
    raw = extract_raw_token(
        authorization=request.headers.get("authorization"),
        access_token=request.query_params.get("access_token"),
    )
    if not raw:
        raise MossworkError(ErrorCode.AUTH_FAILED, "missing credentials")
    return resolve_user_from_token(server, raw)


async def current_user(
    request: Request,
    server: MossworkServer = Depends(get_server),
    authorization: str | None = Header(None),
    access_token: str | None = Query(None),
) -> User:
    cached = getattr(request.state, "mosswork_user", None)
    if cached is not None:
        return cast("User", cached)
    raw = extract_raw_token(authorization=authorization, access_token=access_token)
    if not raw:
        raise MossworkError(ErrorCode.AUTH_FAILED, "missing credentials")
    return resolve_user_from_token(server, raw)


def require_permission(key: str) -> Callable[..., Awaitable[User]]:
    """Dependency factory: require the module permission ``key``.

    ``admin`` is bypassed inside ``user_has_permission``. Unknown keys fail
    fast at construction so typos cannot silently grant access.
    """
    if key not in PERMISSIONS:
        raise RuntimeError(f"unknown permission key: {key}")

    async def _dep(user: User = Depends(current_user)) -> User:
        if not user_has_permission(user, key):
            raise MossworkError(
                ErrorCode.FORBIDDEN,
                "permission required",
                details={"permission": key},
            )
        return user

    return _dep


def require_admin() -> Callable[..., Awaitable[User]]:
    """Dependency factory: require the admin role (no module key)."""

    async def _dep(user: User = Depends(current_user)) -> User:
        if not user.is_admin:
            raise MossworkError(ErrorCode.FORBIDDEN, "admin required")
        return user

    return _dep
