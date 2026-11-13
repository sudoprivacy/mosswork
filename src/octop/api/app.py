"""FastAPI app factory."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from scalar_fastapi import get_scalar_api_reference

from octop.api.middleware.jwt_auth import install as install_jwt_auth
from octop.api.middleware.setup_lockdown import install as install_setup_lockdown
from octop.api.openapi_meta import API_DESCRIPTION, OPENAPI_TAGS, configure_openapi
from octop.branding import SERVER_NAME
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.execution.admission import CanonicalRunAdmissions
from octop.infra.execution.service import ExecutionService
from octop.infra.gateway.feishu_binding import CanonicalFeishuBindingService
from octop.infra.gateway.feishu_delivery import CanonicalFeishuDeliveryService
from octop.infra.identity.api_key_exchange import CanonicalApiKeyExchangeService
from octop.infra.identity.canonical import CanonicalSnapshotService
from octop.infra.identity.canonical_api_keys import CanonicalApiKeyService
from octop.infra.identity.human_sessions import CanonicalHumanSessionService
from octop.infra.identity.moss_entitlements import CanonicalMossEntitlementService
from octop.infra.identity.organization_members import CanonicalOrganizationMemberService
from octop.infra.identity.password_auth import CanonicalPasswordAuthService
from octop.infra.identity.projection import CanonicalRequestIdentityResolver
from octop.infra.identity.self_profile import CanonicalSelfProfileService
from octop.infra.server import MossworkServer
from octop.infra.utils.locale import resolve_request_locale

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RouterMount:
    router: Any
    prefix: str
    tags: Sequence[str]


_NO_CACHE_DASHBOARD_NAMES = frozenset(
    {"sw.js", "manifest.json", "index.html", "desktop-login.html"}
)


def dashboard_cache_control(full_path: str) -> str | None:
    """Cache-Control for a dashboard SPA path, or ``None`` to leave unset."""
    name = Path(full_path).name.lower() if full_path else "index.html"
    if not full_path or name in _NO_CACHE_DASHBOARD_NAMES:
        return "no-cache"
    # Vite emits content-hashed files under assets/ — safe to pin forever.
    if full_path.startswith("assets/"):
        return "public, max-age=31536000, immutable"
    return None


def _dashboard_response(path: Path, full_path: str) -> FileResponse:
    response = FileResponse(path)
    cache_control = dashboard_cache_control(full_path)
    if cache_control is not None:
        response.headers["Cache-Control"] = cache_control
    return response


def dashboard_directory(root: Path, hostname: str | None, cdn_hosts: Sequence[str]) -> Path:
    """Use the optional CDN build only for explicitly configured public hosts."""
    if (
        hostname
        and hostname.lower().rstrip(".") in cdn_hosts
        and (root / "cdn" / "index.html").is_file()
    ):
        return root / "cdn"
    return root


def is_dashboard_asset_path(full_path: str) -> bool:
    """True for hashed build artifacts that must 404 instead of falling back."""
    return full_path.startswith("assets/")


def _dashboard_fallback(
    index_file: Path, full_path: str, *, public_edge_cache: bool = False
) -> FileResponse:
    """Serve the SPA shell for routes; 404 for a missing hashed asset.

    Answering ``/assets/index.<old-hash>.js`` with ``index.html`` makes the
    browser reject HTML as a module script ("not a valid JavaScript MIME
    type"), which hides the real cause — a stale shell after an upgrade.
    """
    if is_dashboard_asset_path(full_path):
        raise HTTPException(status_code=404, detail="Not Found")
    response = _dashboard_response(index_file, "")
    if public_edge_cache:
        # The shell contains no account data. Browsers still revalidate it;
        # the explicit public-host cache rule may keep it at the edge briefly.
        response.headers["Cloudflare-CDN-Cache-Control"] = "public, max-age=60"
    return response


def _mount_routers(app: FastAPI, mounts: Sequence[_RouterMount]) -> None:
    for spec in mounts:
        app.include_router(spec.router, prefix=spec.prefix, tags=list(spec.tags))


def _install_exception_handlers(app: FastAPI) -> None:
    from octop.api.common.execution import execution_error_response
    from octop.infra.db.retention import RetainedResourceError
    from octop.infra.execution.service import ExecutionConflict

    app.add_exception_handler(ExecutionConflict, execution_error_response)

    @app.exception_handler(MossworkError)
    async def _mosswork(request: Request, exc: MossworkError) -> JSONResponse:
        # Client responses are locale-localized; log the original English message
        # so INTERNAL_ERROR (and other 5xx) remain diagnosable in ~/.mosswork/logs.
        if exc.status >= 500:
            logger.error(
                "MossworkError %s in %s: %s",
                exc.code.value,
                request.url.path,
                exc.message,
                exc_info=exc,
            )
        locale = resolve_request_locale(request)
        return JSONResponse(status_code=exc.status, content=exc.to_envelope(locale=locale))

    @app.exception_handler(RetainedResourceError)
    async def _retained(request: Request, exc: RetainedResourceError) -> JSONResponse:
        return await _mosswork(request, MossworkError.localized(ErrorCode.RESOURCE_RETAINED))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception in %s", request.url.path)
        locale = resolve_request_locale(request)
        err = MossworkError.localized(ErrorCode.INTERNAL_ERROR, locale)
        return JSONResponse(status_code=err.status, content=err.to_envelope())


def build_app(
    server: MossworkServer,
    *,
    canonical_identity: CanonicalSnapshotService | None = None,
    canonical_sessions: CanonicalHumanSessionService | None = None,
    canonical_entitlements: CanonicalMossEntitlementService | None = None,
    canonical_feishu_bindings: CanonicalFeishuBindingService | None = None,
    canonical_feishu_delivery: CanonicalFeishuDeliveryService | None = None,
    canonical_requests: CanonicalRequestIdentityResolver | None = None,
    canonical_members: CanonicalOrganizationMemberService | None = None,
    canonical_password: CanonicalPasswordAuthService | None = None,
    canonical_api_keys: CanonicalApiKeyService | None = None,
    canonical_api_key_exchange: CanonicalApiKeyExchangeService | None = None,
    canonical_profile: CanonicalSelfProfileService | None = None,
    canonical_admissions: CanonicalRunAdmissions | None = None,
) -> FastAPI:
    if getattr(server, "canonical_configuration", None) is not None:
        configured = server.canonical
        supplied = locals()
        if configured is None or (
            any(
                supplied[name] is not value
                for name, value in configured.business.app_kwargs().items()
            )
            or canonical_admissions is not configured.admissions
            or server.execution is not configured.execution
        ):
            raise ValueError("Canonical startup requires its configured business and admission")
    if canonical_api_key_exchange is not None:
        machine_issuer = canonical_api_key_exchange._issuer
        if canonical_sessions is not None and canonical_sessions is not machine_issuer:
            raise ValueError("Machine and human credentials must share the canonical issuer")
        canonical_sessions = machine_issuer
        if (
            canonical_api_keys is not None
            and canonical_api_keys is not canonical_api_key_exchange._keys
        ):
            raise ValueError("Machine exchange must use the injected canonical key lifecycle")
        canonical_api_keys = canonical_api_key_exchange._keys
    if canonical_password is not None:
        password_issuer = canonical_password._issuer
        if not isinstance(password_issuer, CanonicalHumanSessionService):
            raise ValueError("Password login must use the maintained canonical human issuer")
        if canonical_sessions is not None and canonical_sessions is not password_issuer:
            raise ValueError("Canonical password and phone login must share the same issuer")
        canonical_sessions = password_issuer
    if canonical_sessions is not None:
        if (
            canonical_identity is not None
            and canonical_identity is not canonical_sessions.snapshots
        ):
            raise ValueError(
                "Canonical lifecycle and snapshots must use the same trusted authority"
            )
        canonical_identity = canonical_sessions.snapshots
    for snapshots in (
        canonical_entitlements._snapshots if canonical_entitlements is not None else None,
        canonical_feishu_bindings._snapshots if canonical_feishu_bindings is not None else None,
        canonical_feishu_delivery._snapshots if canonical_feishu_delivery is not None else None,
        canonical_requests._snapshots if canonical_requests is not None else None,
        canonical_members._snapshots if canonical_members is not None else None,
        canonical_profile._snapshots if canonical_profile is not None else None,
        canonical_api_keys._snapshots if canonical_api_keys is not None else None,
    ):
        if snapshots is not None:
            if canonical_identity is not None and canonical_identity is not snapshots:
                raise ValueError("Canonical adapters must use the same trusted authority")
            canonical_identity = snapshots
    if canonical_admissions is not None and (
        type(canonical_admissions) is not CanonicalRunAdmissions
        or type(canonical_requests) is not CanonicalRequestIdentityResolver
        or type(server.execution) is not ExecutionService
        or server.execution.human_admissions is not canonical_admissions
        or server.execution.repo is not canonical_admissions.repo
        or server.services is None
        or canonical_admissions.repo.db is not server.services.db
        or canonical_requests._projections._db is not server.services.db
        or server._managed_backend is None
    ):
        raise ValueError("Canonical recovery requires the same configured managed queue")
    cfg = server.services.config if server.services else getattr(server, "config", None)
    enable_dashboard = cfg.enable_dashboard if cfg else True
    enable_api_docs = cfg.enable_api_docs if cfg else False
    enable_mobile = (
        cfg.capabilities.mobile.enabled if cfg and cfg.capabilities.mobile.enabled else False
    )

    app = FastAPI(
        title=f"{SERVER_NAME} API",
        version="0.1.0",
        description=API_DESCRIPTION,
        openapi_url="/api/openapi.json" if enable_api_docs else None,
        openapi_tags=OPENAPI_TAGS,
    )
    configure_openapi(app)
    app.state.mosswork_server = server
    app.state.canonical_identity = canonical_identity
    app.state.canonical_sessions = canonical_sessions
    app.state.canonical_entitlements = canonical_entitlements
    app.state.canonical_feishu_bindings = canonical_feishu_bindings
    app.state.canonical_feishu_delivery = canonical_feishu_delivery
    app.state.canonical_requests = canonical_requests
    app.state.canonical_admissions = canonical_admissions
    app.state.canonical_members = canonical_members
    app.state.canonical_password = canonical_password
    app.state.canonical_api_keys = canonical_api_keys
    app.state.canonical_api_key_exchange = canonical_api_key_exchange
    app.state.canonical_profile = canonical_profile
    _install_exception_handlers(app)

    if canonical_admissions is not None:
        from octop.api.routers.canonical_executions import build_canonical_execution_router

        assert canonical_requests is not None
        app.include_router(
            build_canonical_execution_router(server, canonical_requests, canonical_admissions),
            prefix="/api/executions",
            tags=["chat"],
        )

    from octop.api.routers.canonical_auth import build_identity_status_router

    app.include_router(
        build_identity_status_router(
            canonical_enabled=canonical_identity is not None,
            phone_login_available=lambda: (
                canonical_sessions is not None and canonical_sessions.is_phone_login_available
            ),
            password_login_available=canonical_password is not None,
        ),
        prefix="/api/identity",
        tags=["auth"],
    )

    if canonical_sessions is not None:
        from octop.api.routers.canonical_auth import build_canonical_auth_router

        app.include_router(
            build_canonical_auth_router(canonical_sessions),
            prefix="/api/identity",
            tags=["auth"],
        )

    if canonical_password is not None:
        from octop.api.routers.canonical_password import build_canonical_password_router

        app.include_router(
            build_canonical_password_router(canonical_password),
            prefix="/api/identity",
            tags=["auth"],
        )

    if canonical_api_key_exchange is not None:
        from octop.api.routers.canonical_api_key_exchange import (
            build_canonical_api_key_exchange_router,
        )

        app.include_router(
            build_canonical_api_key_exchange_router(canonical_api_key_exchange),
            prefix="/api/identity",
            tags=["auth"],
        )

    if canonical_api_keys is not None:
        from octop.api.routers.canonical_api_keys import build_canonical_api_keys_router

        app.include_router(
            build_canonical_api_keys_router(canonical_api_keys),
            prefix="/api/identity",
            tags=["auth"],
        )

    if canonical_profile is not None:
        from octop.api.routers.canonical_profile import build_canonical_profile_router

        app.include_router(
            build_canonical_profile_router(canonical_profile),
            prefix="/api/identity",
            tags=["auth"],
        )

    if canonical_members is not None:
        from octop.api.routers.canonical_members import build_canonical_members_router

        app.include_router(
            build_canonical_members_router(canonical_members),
            prefix="/api/identity",
            tags=["auth"],
        )

    if canonical_entitlements is not None:
        from octop.api.routers.canonical_access import build_canonical_entitlement_router

        app.include_router(
            build_canonical_entitlement_router(canonical_entitlements),
            prefix="/api/identity",
            tags=["auth"],
        )
    if canonical_feishu_bindings is not None:
        from octop.api.routers.canonical_access import build_canonical_feishu_binding_router

        app.include_router(
            build_canonical_feishu_binding_router(canonical_feishu_bindings),
            prefix="/api/identity",
            tags=["auth"],
        )

    if canonical_feishu_delivery is not None:
        from octop.api.routers.canonical_access import build_canonical_feishu_connection_router

        app.include_router(
            build_canonical_feishu_connection_router(canonical_feishu_delivery),
            prefix="/api/identity",
            tags=["auth"],
        )

    if canonical_identity is not None:
        from octop.api.routers.canonical_identity import build_canonical_identity_router

        app.include_router(
            build_canonical_identity_router(canonical_identity),
            prefix="/api/identity",
            tags=["auth"],
        )

    if cfg and cfg.cors_origins:
        from octop.api.deps import ACCESS_TOKEN_RESPONSE_HEADER

        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[ACCESS_TOKEN_RESPONSE_HEADER],
        )

    from octop.api.middleware.bridge_proxy import install as install_bridge_proxy
    from octop.api.middleware.runtime_activity import RuntimeActivityMiddleware
    from octop.api.middleware.source_publication import SourcePublicationMiddleware

    # Bridge proxy must sit inside JWT auth so ``request.state.mosswork_user`` is set
    # (Starlette runs the last-added middleware first).
    app.add_middleware(SourcePublicationMiddleware, server=server)
    app.add_middleware(RuntimeActivityMiddleware, server=server)
    install_bridge_proxy(app, server)
    install_jwt_auth(app, server)
    install_setup_lockdown(app, server)

    if server.app_runtime is not None and server.app_runtime.bridge_manager is not None:
        from octop.api.bridge_peer import prepare_peer_dashboard_turn
        from octop.api.routers.browser.stream import run_browser_stream_session

        server.app_runtime.bridge_manager.bind_asgi_app(
            app,
            peer_turn_runner=prepare_peer_dashboard_turn,
            peer_browser_runner=run_browser_stream_session,
        )

    from octop.infra.setup.tls.challenge import challenge_store

    @app.get("/.well-known/acme-challenge/{token}", include_in_schema=False)
    async def acme_http01_challenge(token: str) -> PlainTextResponse:
        body = challenge_store.get(token)
        if body is None:
            raise HTTPException(status_code=404, detail="challenge not found")
        return PlainTextResponse(body)

    from octop.api.routers import (
        acp,
        admin,
        agent_files,
        agent_tools,
        agents,
        auth,
        auth_ldap,
        auth_oauth,
        auth_oidc,
        backup,
        bridge,
        browser,
        channels,
        chat,
        client_config,
        communication,
        company_runtime,
        connectors,
        cron,
        desktop,
        devices,
        envs,
        executions,
        expert_assignments,
        experts,
        health,
        i18n,
        internal_mcp,
        invites,
        knowledge_bases,
        mbti,
        media_generation,
        memory,
        memory_portable,
        mobile,
        ollama_models,
        onnx_models,
        plugins,
        preferences,
        proactive_care,
        providers,
        resource_market,
        search,
        settings,
        setup,
        skill_market,
        skill_packages,
        skills,
        slash,
        subagents,
        teams,
        terminal,
        update,
        uploads,
        usage,
        user_roles,
        users,
        voice,
        workspace,
    )
    from octop.api.routers.filesystem import router as filesystem_router
    from octop.api.routers.observability import router as observability_router
    from octop.api.routers.providers import admin_router as admin_providers_router
    from octop.api.routers.security import router as security_router
    from octop.api.routers.storage_backends import admin_router as admin_storage_router
    from octop.api.routers.storage_backends import user_router as storage_backends_user_router
    from octop.api.routers.tls import router as tls_router
    from octop.api.routers.voice import admin_router as admin_voice_router

    _mount_routers(
        app,
        [
            _RouterMount(setup.router, "/api", ["setup"]),
            _RouterMount(auth.router, "/api/auth", ["auth"]),
            _RouterMount(auth_oidc.router, "/api/auth", ["auth"]),
            _RouterMount(auth_oauth.router, "/api/auth", ["auth"]),
            _RouterMount(auth_ldap.router, "/api/auth", ["auth"]),
            _RouterMount(invites.public_router, "/api/auth/invite", ["auth"]),
            _RouterMount(preferences.router, "/api", ["auth"]),
            _RouterMount(i18n.router, "/api", ["i18n"]),
            _RouterMount(health.router, "/api/health", ["health"]),
            _RouterMount(invites.admin_router, "/api/users/invites", ["users"]),
            _RouterMount(user_roles.router, "/api/users/roles", ["users"]),
            _RouterMount(users.router, "/api/users", ["users"]),
            _RouterMount(expert_assignments.router, "/api/admin/expert-assignments", ["users"]),
            _RouterMount(executions.router, "/api/executions", ["chat"]),
            _RouterMount(agents.router, "/api/agents", ["agents"]),
            _RouterMount(agent_tools.router, "/api", ["agents"]),
            _RouterMount(acp.router, "/api", ["agents"]),
            _RouterMount(chat.router, "/api", ["chat"]),
            _RouterMount(slash.router, "/api", ["slash"]),
            _RouterMount(connectors.router, "/api", ["connectors"]),
            _RouterMount(bridge.router, "/api", ["bridge"]),
            _RouterMount(communication.router, "/api/im", ["communication"]),
            _RouterMount(knowledge_bases.router, "/api", ["knowledge"]),
            _RouterMount(internal_mcp.router, "/api", ["internal-mcp"]),
            _RouterMount(channels.router, "/api", ["channels"]),
            _RouterMount(cron.router, "/api", ["cron"]),
            _RouterMount(settings.router, "/api", ["settings"]),
            _RouterMount(client_config.router, "/api", ["client-config"]),
            _RouterMount(envs.router, "/api", ["envs"]),
            _RouterMount(search.router, "/api", ["search"]),
            _RouterMount(providers.router, "/api/providers", ["providers"]),
            _RouterMount(voice.router, "/api/voice", ["voice"]),
            _RouterMount(admin.router, "/api/admin", ["admin"]),
            _RouterMount(backup.router, "/api/admin", ["admin"]),
            _RouterMount(admin_providers_router, "/api/admin/providers", ["admin"]),
            _RouterMount(admin_voice_router, "/api/admin/voice/providers", ["admin"]),
            _RouterMount(observability_router, "/api/admin/observability", ["observability"]),
            _RouterMount(
                media_generation.router,
                "/api/admin/media-generation",
                ["providers"],
            ),
            _RouterMount(tls_router, "/api/admin/tls", ["tls"]),
            _RouterMount(security_router, "/api/admin/security", ["security"]),
            _RouterMount(admin_storage_router, "/api/admin/storage-backends", ["admin"]),
            _RouterMount(
                storage_backends_user_router, "/api/storage-backends", ["storage-backends"]
            ),
            _RouterMount(filesystem_router, "/api/filesystem", ["filesystem"]),
            _RouterMount(mbti.router, "/api", ["mbti"]),
            _RouterMount(experts.router, "/api", ["experts"]),
            _RouterMount(resource_market.router, "/api", ["resource-market"]),
            _RouterMount(teams.router, "/api", ["teams"]),
            _RouterMount(workspace.router, "/api", ["workspace"]),
            _RouterMount(agent_files.router, "/api", ["agent_files"]),
            _RouterMount(memory.router, "/api", ["memory"]),
            _RouterMount(memory_portable.router, "/api", ["memory"]),
            _RouterMount(proactive_care.router, "/api", ["proactive-care"]),
            _RouterMount(usage.router, "/api", ["usage"]),
            _RouterMount(usage.admin_router, "/api/admin", ["admin"]),
            _RouterMount(skill_market.router, "/api", ["skill-packages"]),
            _RouterMount(skill_packages.router, "/api", ["skill-packages"]),
            _RouterMount(skills.router, "/api", ["skills"]),
            _RouterMount(subagents.router, "/api", ["subagents"]),
            _RouterMount(terminal.router, "/api", ["terminal"]),
            _RouterMount(uploads.router, "/api", ["chat"]),
            _RouterMount(devices.router, "/api", ["devices"]),
            _RouterMount(company_runtime.router, "/api", ["devices"]),
            _RouterMount(update.router, "/api", ["update"]),
            _RouterMount(browser.router, "/api", ["browser"]),
            _RouterMount(desktop.router, "/api", ["desktop"]),
            _RouterMount(ollama_models.router, "/api", ["ollama"]),
            _RouterMount(onnx_models.router, "/api", ["onnx"]),
            _RouterMount(plugins.router, "/api", ["plugins"]),
        ],
    )

    if enable_mobile:
        _mount_routers(
            app,
            [
                _RouterMount(mobile.router, "/api", ["mobile"]),
            ],
        )

    if enable_api_docs:

        @app.get("/api/docs", include_in_schema=False)
        async def api_docs() -> HTMLResponse:
            return get_scalar_api_reference(
                openapi_url=app.openapi_url,
                title=f"{SERVER_NAME} API",
            )

    if enable_dashboard:
        dashboard_dir = Path(__file__).parent.parent / "dashboard"
        index_file = dashboard_dir / "index.html"
        if index_file.exists():

            @app.get("/{full_path:path}", include_in_schema=False)
            async def spa_fallback(request: Request, full_path: str) -> FileResponse:
                if full_path.startswith(("api/", "ws/")):
                    raise HTTPException(status_code=404, detail="Not Found")

                selected_dir = dashboard_directory(
                    dashboard_dir,
                    request.url.hostname,
                    cfg.dashboard_cdn_hosts if cfg else (),
                )
                selected_index = selected_dir / "index.html"
                public_edge_cache = selected_dir != dashboard_dir
                if full_path:
                    raw_path = Path(full_path)
                    # Reject absolute paths and parent-dir references before
                    # joining, so user input never drives a path expression.
                    if raw_path.is_absolute() or ".." in raw_path.parts:
                        return _dashboard_fallback(
                            selected_index, full_path, public_edge_cache=public_edge_cache
                        )
                    candidate = (selected_dir / Path(*raw_path.parts)).resolve()
                    try:
                        candidate.relative_to(selected_dir.resolve())
                    except ValueError:
                        return _dashboard_fallback(
                            selected_index, full_path, public_edge_cache=public_edge_cache
                        )
                    if candidate.is_file():
                        return _dashboard_response(candidate, full_path)
                    # A previously cached shell can still reference local
                    # hashes after the public host switches to the CDN build.
                    if selected_dir != dashboard_dir and is_dashboard_asset_path(full_path):
                        previous = (dashboard_dir / Path(*raw_path.parts)).resolve()
                        if previous.is_relative_to(dashboard_dir.resolve()) and previous.is_file():
                            return _dashboard_response(previous, full_path)
                return _dashboard_fallback(
                    selected_index, full_path, public_edge_cache=public_edge_cache
                )

    return app
