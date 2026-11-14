"""Explicit canonical monolith inputs for the existing product startup path.

These inputs are supplied by trusted controller composition. They do not load
secrets, select a provider from request metadata, or establish writer exclusion.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from moss_gateway.channels.feishu import FeishuChannel

from octop.config import MossworkConfig
from octop.infra.agents.execution_backend import ManagedExecutionBackend
from octop.infra.db.factory import resolve_sqlite_db_path
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.canonical_identity import CanonicalIdentityProjectionRepo
from octop.infra.db.repos.retained_cohost import RetainedCohostBindingRepo
from octop.infra.db.repos.retained_review import RetainedHumanReviewDocumentRepo
from octop.infra.db.repos.retained_session_projection import RetainedSessionProjectionRepo
from octop.infra.db.services import SharedServices
from octop.infra.execution.admission import CanonicalRunAdmissions
from octop.infra.execution.selection import ProductCohostSelections
from octop.infra.execution.service import ExecutionService
from octop.infra.gateway.feishu_delivery import FeishuSessionAssertion
from octop.infra.identity.composition import CanonicalBusinessServices, build_canonical_business
from octop.infra.identity.login_policy import LoginMethod
from octop.infra.identity.organization_login_policy import CanonicalLoginMethodReadiness
from octop.infra.identity.phone_auth import PhoneCodeConfig, SmsSender
from octop.infra.identity.platform_login_policy import CanonicalPlatformLoginMethodReadiness
from octop.infra.identity.sms_configuration_updates import CanonicalSmsConfigurationVault
from octop.infra.identity.write_authority import CanonicalWriterAdmission
from octop.infra.utils.paths import PathLayout

if TYPE_CHECKING:
    from octop.infra.identity.projection import CanonicalRequestIdentityResolver
    from octop.infra.identity.request_principal import CanonicalAuthenticatedRequest
    from octop.infra.native_host.human_review import HumanReviewReader


class CanonicalHumanReviewResolver(Protocol):
    """Resolve from this held human and original project with actual current grants."""

    async def __call__(
        self,
        principal: CanonicalAuthenticatedRequest,
        *,
        project_id: str,
        endpoint: str,
    ) -> HumanReviewReader: ...


CanonicalHumanReviewPorts = tuple[
    CanonicalHumanReviewResolver,
    "CanonicalRequestIdentityResolver",
    RetainedHumanReviewDocumentRepo,
    CanonicalWriterAdmission,
]


@dataclass(frozen=True, repr=False)
class CanonicalRuntimeInputs:
    """Exact shared instances for the configured Owner6 provider constructor.

    selections is the current Product-to-original binding reader, not a kind,
    action/model grant, history importer or native readiness provider.
    """

    business: CanonicalBusinessServices
    services: SharedServices
    originals: RetainedCohostBindingRepo
    selections: ProductCohostSelections
    execution: ExecutionService


class CanonicalManagedBackendFactory(Protocol):
    def __call__(self, inputs: CanonicalRuntimeInputs, /) -> ManagedExecutionBackend:
        """Construct once from these instances; async startup belongs to boot()."""
        ...


@dataclass(frozen=True, repr=False, kw_only=True)
class CanonicalProductConfiguration:
    """Required configured dependencies, never a native readiness/grant receipt.

    node/incarnation identify only the existing Product execution lease. The
    backend must independently enforce current actor, registered resource kind,
    exact grants and original native custody. Its presence is not qualification.
    Optional login_policy_readiness is the actual org-aware SMS/CAS port; phone
    settings never substitute for it. Absence leaves that writer service absent
    and its presence does not mount an HTTP route.
    """

    database_path: Path
    authority_id: str
    secret: str
    issuer: str
    access_ttl_seconds: int
    phone_config: PhoneCodeConfig | None
    phone_enabled: Callable[[], bool]
    default_login_method: LoginMethod
    sms_sender: SmsSender | None
    writer: CanonicalWriterAdmission
    backend_factory: CanonicalManagedBackendFactory
    node: str
    incarnation: str
    capacity: int
    login_policy_readiness: CanonicalLoginMethodReadiness | None = None
    platform_login_policy_readiness: CanonicalPlatformLoginMethodReadiness | None = None
    sms_configuration_vault: CanonicalSmsConfigurationVault | None = None
    human_review_reader: CanonicalHumanReviewResolver | None = None
    feishu_channel: FeishuChannel | None = None
    feishu_plugin_id: str | None = None
    feishu_connection_owner_id: str | None = None
    feishu_sessions: FeishuSessionAssertion | None = None
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if not isinstance(self.database_path, Path) or not self.database_path.is_absolute():
            raise ValueError("An explicit absolute canonical SQLite path is required")
        if not self.node or not self.incarnation or type(self.capacity) is not int:
            raise ValueError("Explicit Product executor identity and capacity are required")
        if not 1 <= self.capacity <= 256:
            raise ValueError("Invalid Product executor capacity")
        if not callable(self.backend_factory):
            raise TypeError("A configured managed backend factory is required")
        if self.human_review_reader is not None and not callable(self.human_review_reader):
            raise TypeError("The actual current human review reader resolver is required")
        if self.login_policy_readiness is not None and not callable(
            getattr(self.login_policy_readiness, "assert_available", None)
        ):
            raise TypeError("The actual organization login-method readiness port is required")
        if self.platform_login_policy_readiness is not None and not callable(
            getattr(self.platform_login_policy_readiness, "assert_platform_available", None)
        ):
            raise TypeError("The actual platform login-method readiness port is required")
        if self.sms_configuration_vault is not None and not all(
            callable(getattr(self.sms_configuration_vault, name, None))
            for name in ("get_secret", "put_secret")
        ):
            raise TypeError("The existing trusted platform SMS vault port is required")

    def validate_storage(self, config: MossworkConfig, paths: PathLayout) -> Path:
        """Refuse canonical aliases BEFORE opening/migrating the Product pool.

        Deployment must keep paths/mounts stable throughout boot and runtime.
        This metadata check cannot exclude a concurrent filesystem replacement.
        """
        if not config.database.is_sqlite:
            raise ValueError("Canonical monolith currently requires an explicit SQLite Product DB")
        try:
            canonical = self.database_path.resolve(strict=True)
            if not canonical.is_file():
                raise ValueError("The original canonical SQLite file must already exist")
            product = resolve_sqlite_db_path(config, paths).resolve()
            protected = (
                canonical,
                *(Path(str(canonical) + s) for s in ("-wal", "-shm", "-journal")),
            )
            writable = (
                product,
                *(Path(str(product) + s) for s in ("-wal", "-shm", "-journal")),
            )
            for target in writable:
                for original in protected:
                    try:
                        target.stat()
                        original.stat()
                    except FileNotFoundError:
                        aliased = False
                    else:
                        aliased = target.samefile(original)
                    if target.resolve() == original.resolve() or aliased:
                        raise ValueError(
                            "Canonical and Product storage must be physically separate"
                        )
        except (OSError, RuntimeError):
            raise ValueError(
                "Cannot verify original canonical and separate Product storage"
            ) from None
        return product

    def build(self, services: SharedServices) -> CanonicalProductServices:
        """Bind the actual business services and ONE queue to this Product pool."""
        expected = self.validate_storage(services.config, services.paths)
        if not isinstance(services.db, SqlitePool) or services.db.path.resolve() != expected:
            raise ValueError("Canonical composition requires the checked Product pool")
        business = build_canonical_business(
            self.database_path,
            authority_id=self.authority_id,
            secret=self.secret,
            issuer=self.issuer,
            access_ttl_seconds=self.access_ttl_seconds,
            phone_config=self.phone_config,
            phone_enabled=self.phone_enabled,
            default_login_method=self.default_login_method,
            sms_sender=self.sms_sender,
            writer=self.writer,
            product_projections=CanonicalIdentityProjectionRepo(services.db),
            login_policy_readiness=self.login_policy_readiness,
            platform_login_policy_readiness=self.platform_login_policy_readiness,
            sms_configuration_vault=self.sms_configuration_vault,
            feishu_channel=self.feishu_channel,
            feishu_plugin_id=self.feishu_plugin_id,
            feishu_connection_owner_id=self.feishu_connection_owner_id,
            feishu_sessions=self.feishu_sessions,
            clock=self.clock,
        )
        repo = services.execution_repo
        admissions = CanonicalRunAdmissions(repo)
        execution = ExecutionService(
            repo,
            node=self.node,
            incarnation=self.incarnation,
            role="monolith",
            capacity=self.capacity,
            human_admissions=admissions,
        )
        originals = RetainedCohostBindingRepo(self.database_path)
        selections = ProductCohostSelections(RetainedSessionProjectionRepo(services.db), originals)
        inputs = CanonicalRuntimeInputs(business, services, originals, selections, execution)
        backend = self.backend_factory(inputs)
        for method in (
            "boot",
            "aclose",
            "assert_chat_current",
            "start",
            "stop",
            "reload",
            "stream",
            "call",
            "resume_hitl",
            "request_cancel",
            "get_thread_model",
            "set_thread_model",
            "clear_thread_model",
        ):
            if not callable(getattr(backend, method, None)):
                raise TypeError("The configured factory must return a managed execution backend")
        human_review_ports = (
            (
                self.human_review_reader,
                business.requests,
                RetainedHumanReviewDocumentRepo(self.database_path),
                self.writer,
            )
            if self.human_review_reader is not None
            else None
        )
        return CanonicalProductServices(
            business, admissions, execution, inputs, backend, human_review_ports
        )


@dataclass(frozen=True, repr=False)
class CanonicalProductServices:
    business: CanonicalBusinessServices
    admissions: CanonicalRunAdmissions
    execution: ExecutionService
    runtime_inputs: CanonicalRuntimeInputs
    backend: ManagedExecutionBackend
    human_review_ports: CanonicalHumanReviewPorts | None = None
