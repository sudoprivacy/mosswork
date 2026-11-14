"""Construct the retained business services once for a trusted launch factory.

This module neither reads configuration/secrets nor starts channels, creates a
database, asserts writer takeover or grants a native/resource operation. The
caller owns those prerequisites and lifecycles. Every canonical consumer uses
the same path, snapshot instance, issuer, clock and required writer admission.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from moss_gateway.channels.feishu import FeishuChannel

from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.canonical_identity import CanonicalIdentityProjectionRepo
from octop.infra.gateway.canonical_feishu import CanonicalFeishuRouteService
from octop.infra.gateway.feishu_binding import CanonicalFeishuBindingService
from octop.infra.gateway.feishu_delivery import (
    CanonicalFeishuDeliveryService,
    FeishuSessionAssertion,
)
from octop.infra.identity.api_key_exchange import CanonicalApiKeyExchangeService
from octop.infra.identity.canonical import CanonicalSnapshotService
from octop.infra.identity.canonical_api_keys import CanonicalApiKeyService
from octop.infra.identity.human_sessions import CanonicalHumanSessionService
from octop.infra.identity.login_policy import LoginMethod, SqliteLoginPolicyReader
from octop.infra.identity.model_credentials import (
    OwnerModelCredentialService,
    SqliteOwnerModelCredentialStore,
)
from octop.infra.identity.moss_entitlements import CanonicalMossEntitlementService
from octop.infra.identity.organization_login_policy import (
    CanonicalLoginMethodReadiness,
    CanonicalOrganizationLoginPolicyService,
)
from octop.infra.identity.organization_members import CanonicalOrganizationMemberService
from octop.infra.identity.password_auth import CanonicalPasswordAuthService
from octop.infra.identity.phone_auth import (
    CanonicalPhoneAuthService,
    PhoneCodeConfig,
    SmsSender,
    SqlitePhoneCodeStore,
)
from octop.infra.identity.platform_login_policy import (
    CanonicalPlatformLoginMethodReadiness,
    CanonicalPlatformLoginPolicyService,
)
from octop.infra.identity.projection import CanonicalRequestIdentityResolver
from octop.infra.identity.self_profile import CanonicalSelfProfileService
from octop.infra.identity.sms_configuration_updates import (
    CanonicalManagedSmsConfigurationService,
    CanonicalSmsConfigurationVault,
)
from octop.infra.identity.write_authority import CanonicalWriterAdmission


class CanonicalAppServices(TypedDict):
    """The existing build_app keyword contract, without importing the HTTP layer."""

    canonical_identity: CanonicalSnapshotService
    canonical_sessions: CanonicalHumanSessionService
    canonical_entitlements: CanonicalMossEntitlementService
    canonical_feishu_bindings: CanonicalFeishuBindingService | None
    canonical_feishu_delivery: CanonicalFeishuDeliveryService | None
    canonical_requests: CanonicalRequestIdentityResolver
    canonical_members: CanonicalOrganizationMemberService
    canonical_password: CanonicalPasswordAuthService
    canonical_api_keys: CanonicalApiKeyService
    canonical_api_key_exchange: CanonicalApiKeyExchangeService
    canonical_profile: CanonicalSelfProfileService


@dataclass(frozen=True, repr=False)
class CanonicalBusinessServices:
    """Actual adapters, not a Ready result, grant or serialized configuration.

    The model store has separate ordinary-member and Moss-specific fact APIs.
    Resource kind and exact caller/model/native authorization remain the
    consumer's responsibility. self_model_credentials keeps its Moss-only rule.
    """

    snapshots: CanonicalSnapshotService
    phone: CanonicalPhoneAuthService | None
    policy: SqliteLoginPolicyReader
    organization_login_policy: CanonicalOrganizationLoginPolicyService | None
    platform_login_policy: CanonicalPlatformLoginPolicyService | None
    managed_sms_configuration: CanonicalManagedSmsConfigurationService | None
    sessions: CanonicalHumanSessionService
    password: CanonicalPasswordAuthService
    entitlements: CanonicalMossEntitlementService
    requests: CanonicalRequestIdentityResolver
    members: CanonicalOrganizationMemberService
    api_keys: CanonicalApiKeyService
    api_key_exchange: CanonicalApiKeyExchangeService
    profile: CanonicalSelfProfileService
    model_store: SqliteOwnerModelCredentialStore
    self_model_credentials: OwnerModelCredentialService
    feishu_routes: CanonicalFeishuRouteService | None
    feishu_bindings: CanonicalFeishuBindingService | None
    feishu_delivery: CanonicalFeishuDeliveryService | None

    def app_kwargs(self) -> CanonicalAppServices:
        """Map once to build_app(server, **bundle.app_kwargs()) at the launch root."""
        return {
            "canonical_identity": self.snapshots,
            "canonical_sessions": self.sessions,
            "canonical_entitlements": self.entitlements,
            "canonical_feishu_bindings": self.feishu_bindings,
            "canonical_feishu_delivery": self.feishu_delivery,
            "canonical_requests": self.requests,
            "canonical_members": self.members,
            "canonical_password": self.password,
            "canonical_api_keys": self.api_keys,
            "canonical_api_key_exchange": self.api_key_exchange,
            "canonical_profile": self.profile,
        }


def _assert_separate_sqlite(canonical_path: Path, product_path: Path) -> None:
    """Reject path/file aliases; filesystem observation failure is not separation."""
    try:
        canonical, product = canonical_path.resolve(), product_path.resolve()
        if canonical == product:
            raise ValueError("Product projection and canonical databases must be separate")
        both_exist = True
        for path in (canonical, product):
            try:
                path.stat()
            except FileNotFoundError:
                both_exist = False
        if both_exist and canonical.samefile(product):
            raise ValueError("Product projection and canonical databases must be separate")
    except (OSError, RuntimeError):
        raise ValueError("Cannot verify separate canonical and product database files") from None


def build_canonical_business(
    database_path: Path,
    *,
    authority_id: str,
    secret: str,
    issuer: str,
    access_ttl_seconds: int,
    phone_config: PhoneCodeConfig | None,
    phone_enabled: Callable[[], bool],
    default_login_method: LoginMethod,
    sms_sender: SmsSender | None,
    writer: CanonicalWriterAdmission,
    product_projections: CanonicalIdentityProjectionRepo,
    login_policy_readiness: CanonicalLoginMethodReadiness | None = None,
    platform_login_policy_readiness: CanonicalPlatformLoginMethodReadiness | None = None,
    sms_configuration_vault: CanonicalSmsConfigurationVault | None = None,
    feishu_channel: FeishuChannel | None = None,
    feishu_plugin_id: str | None = None,
    feishu_connection_owner_id: str | None = None,
    feishu_sessions: FeishuSessionAssertion | None = None,
    clock: Callable[[], float] = time.time,
) -> CanonicalBusinessServices:
    """Assemble existing domains using explicitly supplied trusted dependencies.

    secret is the persistent original JWT secret, also used by the original
    phone-code HMAC. There is no second secret, random/default key, no-op port
    or environment fallback. The already constructed SDK channel supplies its
    actual app/instance IDs; current tenant/revision still come from canonical
    rows. When Feishu is absent, its three adapters are not installed. When
    configured, all its dependencies are required together. Constructing a
    channel is not evidence that it is connected.

    Constructors do not open the canonical DB or change schemas. Product pool
    initialization/migration belongs to the launch owner; it must be separate.
    The SQLite alias guard reads filesystem metadata and fails closed on IO
    error. It cannot undo earlier pool initialization: the factory must keep
    stores separate before opening that pool, and preserve that separation.
    Writer admission is only None-or-raise, not a lease or takeover mechanism.
    Organization login-policy writes are installed only with an explicit
    controlled SMS/CAS readiness provider. phone_enabled and sms_sender do not
    establish that readiness. Platform writes need their separately scoped
    readiness port; organization readiness never enables the platform writer.
    The existing HTTP keyword contract is unchanged.
    """
    if not isinstance(database_path, Path):
        raise TypeError("A trusted canonical SQLite Path is required")
    if not callable(phone_enabled):
        raise TypeError("The actual effective phone setting reader is required")
    if phone_config is not None and type(phone_config) is not PhoneCodeConfig:
        raise TypeError("The controlled original phone-code configuration is required")
    if (phone_config is None) != (sms_sender is None):
        raise TypeError("Actual phone configuration and SMS sender must be supplied together")
    if type(product_projections) is not CanonicalIdentityProjectionRepo:
        raise TypeError("The existing product projection repository is required")
    if login_policy_readiness is not None and not callable(
        getattr(login_policy_readiness, "assert_available", None)
    ):
        raise TypeError("The actual organization login-method readiness port is required")
    if platform_login_policy_readiness is not None and not callable(
        getattr(platform_login_policy_readiness, "assert_platform_available", None)
    ):
        raise TypeError("The actual platform login-method readiness port is required")
    if sms_configuration_vault is not None and not all(
        callable(getattr(sms_configuration_vault, name, None))
        for name in ("get_secret", "put_secret")
    ):
        raise TypeError("The existing trusted platform SMS vault port is required")
    product_db = product_projections._db
    if isinstance(product_db, SqlitePool):
        _assert_separate_sqlite(database_path, product_db.path)
    if (
        (sms_sender is not None and not callable(sms_sender))
        or not callable(getattr(writer, "assert_current_writer", None))
        or not callable(clock)
    ):
        raise TypeError("Actual SMS, writer and clock ports are required")
    routes = None
    if feishu_channel is None:
        if any(
            value is not None
            for value in (feishu_plugin_id, feishu_connection_owner_id, feishu_sessions)
        ):
            raise TypeError("Feishu dependencies must be supplied together or all absent")
    else:
        if type(feishu_channel) is not FeishuChannel or not callable(
            getattr(feishu_sessions, "assert_session", None)
        ):
            raise TypeError(
                "The actual Feishu SDK channel and native session assertion are required"
            )
        if feishu_plugin_id is None or feishu_connection_owner_id is None:
            raise TypeError("The original Feishu plugin and connection owner are required")
        routes = CanonicalFeishuRouteService(
            database_path,
            authority_id=authority_id,
            channel_id=feishu_channel.channel_id,
            plugin_id=feishu_plugin_id,
            connection_owner_id=feishu_connection_owner_id,
            actual_app_id=feishu_channel._config.app_id,
        )
    policy = SqliteLoginPolicyReader(database_path, default_method=default_login_method)
    phone = (
        CanonicalPhoneAuthService(
            SqlitePhoneCodeStore(database_path),
            phone_config,
            secret=secret,
            sms_sender=sms_sender,
            writer=writer,
            clock=clock,
        )
        if phone_config is not None and sms_sender is not None
        else None
    )
    sessions = CanonicalHumanSessionService(
        database_path,
        authority_id=authority_id,
        secret=secret,
        issuer=issuer,
        phone=phone,
        phone_enabled=phone_enabled,
        policy=policy,
        writer=writer,
        access_ttl_seconds=access_ttl_seconds,
        clock=clock,
    )
    snapshots = sessions.snapshots
    keys = CanonicalApiKeyService(database_path, snapshots, writer=writer, clock=clock)
    models = SqliteOwnerModelCredentialStore(database_path, authority_id=authority_id)
    return CanonicalBusinessServices(
        snapshots=snapshots,
        phone=phone,
        policy=policy,
        organization_login_policy=(
            CanonicalOrganizationLoginPolicyService(
                database_path,
                snapshots,
                writer=writer,
                readiness=login_policy_readiness,
                clock=clock,
            )
            if login_policy_readiness is not None
            else None
        ),
        platform_login_policy=(
            CanonicalPlatformLoginPolicyService(
                database_path,
                snapshots,
                writer=writer,
                readiness=platform_login_policy_readiness,
                clock=clock,
            )
            if platform_login_policy_readiness is not None
            else None
        ),
        managed_sms_configuration=(
            CanonicalManagedSmsConfigurationService(
                database_path, snapshots, writer=writer, vault=sms_configuration_vault, clock=clock
            )
            if sms_configuration_vault is not None
            else None
        ),
        sessions=sessions,
        password=CanonicalPasswordAuthService(
            database_path, policy, writer=writer, issuer=sessions
        ),
        entitlements=CanonicalMossEntitlementService(
            database_path, snapshots, writer=writer, clock=clock
        ),
        requests=CanonicalRequestIdentityResolver(snapshots, product_projections),
        members=CanonicalOrganizationMemberService(
            database_path, snapshots, writer=writer, clock=clock
        ),
        api_keys=keys,
        api_key_exchange=CanonicalApiKeyExchangeService(keys, issuer=sessions),
        profile=CanonicalSelfProfileService(database_path, snapshots, writer=writer, clock=clock),
        model_store=models,
        self_model_credentials=OwnerModelCredentialService(snapshots, models),
        feishu_routes=routes,
        feishu_bindings=(
            CanonicalFeishuBindingService(
                database_path, snapshots, routes, writer=writer, clock=clock
            )
            if routes is not None
            else None
        ),
        feishu_delivery=(
            CanonicalFeishuDeliveryService(
                database_path,
                snapshots,
                routes,
                writer=writer,
                session_assertion=feishu_sessions,
                clock=clock,
            )
            if routes is not None and feishu_sessions is not None
            else None
        ),
    )
