"""Constructor-only composition plus actual retained SQLite and SDK domain flows."""

from __future__ import annotations

import inspect
import json
import sqlite3
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from moss_gateway.channels.feishu import FeishuChannel, FeishuConfig
from tests.unit.gateway.test_canonical_feishu import _IM_SCHEMA
from tests.unit.gateway.test_feishu_delivery_business_store import (
    _DELIVERY_SCHEMA,
    _Sessions,
)
from tests.unit.gateway.test_personal_moss_im_identity import _processor, _sdk_message
from tests.unit.identity.test_canonical_human_request_projection import product as product
from tests.unit.identity.test_human_sessions import (
    _IDS,
    _LOGOUT_SCHEMA,
    _NOW,
    _NUMBER,
    _PHONE,
    _POLICY,
    _SECRET,
    _TOKENS,
    _Sender,
    _Writer,
)
from tests.unit.identity.test_human_tokens import legacy_db as legacy_db
from tests.unit.identity.test_model_credentials import _case, _load, _token

from octop.api.app import build_app
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.canonical_identity import CanonicalIdentityProjectionRepo
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.gateway.personal_moss_identity import FeishuMemberConnection
from octop.infra.identity import composition
from octop.infra.identity.composition import CanonicalBusinessServices, build_canonical_business
from octop.infra.identity.phone_auth import PhoneAuthError, PhoneCodeConfig
from octop.infra.identity.request_principal import authenticate

_FIXTURES = Path(__file__).parent / "fixtures"
_MEMBERS = json.loads((_FIXTURES / "organization_members_port_legacy.json").read_text("utf-8"))
_KEYS = json.loads((_FIXTURES / "canonical_api_keys_port_legacy.json").read_text("utf-8"))
_PASSWORD = json.loads((_FIXTURES / "password_auth_canonical_legacy.json").read_text("utf-8"))


@dataclass
class _Environment:
    path: Path
    product: SqlitePool
    sender: _Sender
    writer: _Writer
    native: _Sessions
    kwargs: dict[str, Any]
    bundle: CanonicalBusinessServices


@pytest.fixture
def environment(legacy_db: Path, product: SqlitePool) -> _Environment:
    # Exact original schemas from the existing independent TS fixtures. All
    # initialization here is fixture setup, never part of the composition.
    with sqlite3.connect(legacy_db) as db:
        db.executescript(
            "BEGIN;"
            + _PHONE["schema"]
            + _POLICY["schema"]["policies"]
            + ";"
            + _POLICY["schema"]["profiles"]
            + _LOGOUT_SCHEMA
            + _IM_SCHEMA
            + _DELIVERY_SCHEMA
            + _MEMBERS["supplementalSchema"]
            + _KEYS["schema"]
        )
        connection = FeishuMemberConnection(
            _IDS["homeOrg"],
            _IDS["homeMember"],
            "plugin-a",
            "tenant-a",
            "cli_app_a",
            "r1",
            "channel-a",
        )
        db.execute(
            "INSERT INTO channel_plugins (id,type,name,enabled,status,user_id,org_id,created_at,"
            "updated_at) VALUES ('plugin-a','lark','Moss',1,'connected',?,?,1,1)",
            (_IDS["homeMember"], _IDS["homeOrg"]),
        )
        db.execute(
            "INSERT INTO moss_im_connections VALUES ('plugin-a',?,?,'cli_app_a','tenant-a','r1',?,1)",
            (_IDS["homeMember"], _IDS["homeOrg"], _IDS["homeMember"]),
        )
        db.execute(
            "INSERT INTO user_auth_identities VALUES ('binding-a',?,?,'feishu-moss',?,"
            "'ou_member_a','{}',1,1)",
            (_IDS["homeOrg"], _IDS["homeMember"], connection.scope),
        )
    _load(legacy_db, _case("original_organization_member_writer_and_reader"))
    sender, writer, native = _Sender(), _Writer(), _Sessions()
    channel = FeishuChannel(
        _processor,
        FeishuConfig(app_id="cli_app_a", app_secret="public-synthetic-sdk-only"),
        channel_id="channel-a",
    )
    kwargs: dict[str, Any] = {
        "authority_id": "retained-moss",
        "secret": _SECRET,
        "issuer": _TOKENS["issuer"],
        "access_ttl_seconds": 300,
        "phone_config": PhoneCodeConfig(300, 60, 5, 3),
        "phone_enabled": lambda: True,
        "default_login_method": "sms",
        "sms_sender": sender,
        "writer": writer,
        "product_projections": CanonicalIdentityProjectionRepo(product),
        "feishu_channel": channel,
        "feishu_plugin_id": "plugin-a",
        "feishu_connection_owner_id": _IDS["homeMember"],
        "feishu_sessions": native,
        "clock": lambda: float(_NOW),
    }
    return _Environment(
        legacy_db,
        product,
        sender,
        writer,
        native,
        kwargs,
        build_canonical_business(legacy_db, **kwargs),
    )


async def _login(env: _Environment, bundle: CanonicalBusinessServices | None = None):
    services = bundle or env.bundle
    await services.sessions.send_code(_NUMBER)
    return await services.sessions.login(_NUMBER, env.sender.sent[-1][1])


def test_constructs_one_actual_issuer_and_no_database_or_effect(
    environment: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = []
    original = composition.CanonicalHumanSessionService

    def counted(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        observed.append(result)
        return result

    def forbidden_connect(*args: Any, **kwargs: Any):
        raise AssertionError("Constructor must not open or initialize a database")

    monkeypatch.setattr(composition, "CanonicalHumanSessionService", counted)
    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    bundle = build_canonical_business(environment.path, **environment.kwargs)
    assert observed == [bundle.sessions]
    assert bundle.snapshots is bundle.sessions.snapshots
    assert bundle.snapshots._verifier is bundle.sessions._verifier
    assert bundle.sessions._phone is bundle.phone
    assert bundle.sessions._policy is bundle.policy
    assert bundle.password._issuer is bundle.sessions
    assert bundle.api_key_exchange._issuer is bundle.sessions
    assert bundle.api_key_exchange._keys is bundle.api_keys
    assert bundle.phone._secret == bundle.sessions._secret.decode() == _SECRET
    assert environment.writer.calls == 0
    assert environment.sender.sent == [] and environment.native.calls == []
    for service in (
        bundle.entitlements,
        bundle.requests,
        bundle.members,
        bundle.profile,
        bundle.api_keys,
        bundle.self_model_credentials,
        bundle.feishu_bindings,
        bundle.feishu_delivery,
    ):
        assert service is not None and service._snapshots is bundle.snapshots
    for service in (
        bundle.sessions,
        bundle.password,
        bundle.phone,
        bundle.entitlements,
        bundle.members,
        bundle.profile,
        bundle.api_keys,
        bundle.feishu_bindings,
        bundle.feishu_delivery,
    ):
        assert service is not None and service._writer is environment.writer
    assert bundle.model_store._uri == bundle.snapshots._store._uri
    assert bundle.model_store.authority_id == bundle.snapshots._store.authority_id
    assert bundle.self_model_credentials._store is bundle.model_store
    assert bundle.requests._projections is environment.kwargs["product_projections"]
    assert bundle.feishu_routes is not None
    assert bundle.feishu_bindings is not None and bundle.feishu_delivery is not None
    assert bundle.feishu_bindings._channel is bundle.feishu_routes
    assert bundle.feishu_delivery._channel is bundle.feishu_routes
    assert bundle.feishu_delivery._sessions is environment.native


async def test_actual_phone_human_request_switch_and_owner_facts(environment: _Environment) -> None:
    bundle = environment.bundle
    pair = await _login(environment)
    principal = await authenticate(bundle.requests, pair.access_token)
    assert (await principal.revalidate()).snapshot.account_id == _IDS["account"]
    assert principal.identity.snapshot.member.owner_id == _IDS["homeMember"]
    memberships = await bundle.snapshots.organizations(pair.access_token)
    assert {member.organization_id for member in memberships} == {
        _IDS["homeOrg"],
        _IDS["companyOrg"],
    }
    assert {m.member_id for m in await bundle.members.list_members(pair.access_token)} == {
        _IDS["homeMember"]
    }
    company = await bundle.sessions.switch_organization(pair.access_token, _IDS["companyOrg"])
    current = await authenticate(bundle.requests, company.access_token)
    assert current.identity.snapshot.member.owner_id == _IDS["companyMember"]
    with sqlite3.connect(environment.path) as db:
        db.execute("UPDATE moss_member_access SET enabled=0,updated_at=updated_at+1")
    facts = bundle.model_store.get_for_registered_member(
        organization_id=_IDS["companyOrg"], member_id=_IDS["companyMember"]
    )
    assert facts.binding is not None and facts.binding.billing_mode == "organization_shared"
    bundle.model_store.assert_registered_member_current(facts)
    with pytest.raises(MossworkError) as error:
        bundle.model_store.get_for_registered_owner(
            organization_id=_IDS["companyOrg"], member_id=_IDS["companyMember"]
        )
    assert error.value.code == ErrorCode.MOSS_ACCESS_DENIED
    with pytest.raises(MossworkError):
        await current.revalidate()
    refreshed = await bundle.sessions.refresh(pair.refresh_token)
    await bundle.sessions.logout(refreshed.access_token, refreshed.refresh_token)
    with pytest.raises(MossworkError):
        await authenticate(bundle.requests, refreshed.access_token)
    assert environment.native.calls == []


async def test_actual_key_lifecycle_and_machine_exchange_do_not_make_human(
    environment: _Environment,
) -> None:
    bundle = environment.bundle
    pair = await _login(environment)
    created = await bundle.api_keys.create_key(
        pair.access_token, _IDS["homeMember"], "composition fixture", ("sessions:list",)
    )
    assert (await bundle.api_keys.list_keys(pair.access_token))[0].id == created.api_key.id
    machine = await bundle.api_key_exchange.exchange(created.plain_text_key)
    credential = await bundle.api_key_exchange.verify_access(machine.access_token)
    assert credential is not None and credential.scopes == ("sessions:list",)
    assert credential.member_id == _IDS["homeMember"]
    with pytest.raises(MossworkError):
        await authenticate(bundle.requests, machine.access_token)
    await bundle.api_keys.revoke_key(pair.access_token, created.api_key.id)
    with pytest.raises(MossworkError):
        await bundle.api_key_exchange.exchange(created.plain_text_key)
    assert environment.native.calls == []


async def test_actual_sdk_claim_pointer_dispatch_uses_injected_native_port(
    environment: _Environment,
) -> None:
    bundle = environment.bundle
    delivery = bundle.feishu_delivery
    assert delivery is not None
    channel = environment.kwargs["feishu_channel"]
    queued = []
    channel._running = True
    channel._enqueue_callback = queued.append
    channel._add_reaction_async = MagicMock()
    channel._on_message_event(_sdk_message())
    assert len(queued) == 1
    message = channel.parse_inbound(queued[0])
    claim = await delivery.claim(message)
    assert claim is not None and claim.route is not None
    environment.native.register(claim.route, "actual-synthetic-session")
    claim = await delivery.set_pointer(claim, "actual-synthetic-session")
    await delivery.delivery(claim, "dispatching", session_id="actual-synthetic-session")
    native_calls = len(environment.native.calls)
    await delivery.assert_dispatch_current(claim, session_id="actual-synthetic-session")
    assert len(environment.native.calls) == native_calls
    human = await _login(environment)
    await bundle.entitlements.set_access(human.access_token, _IDS["homeMember"], False)
    with pytest.raises(MossworkError):
        await delivery.assert_dispatch_current(claim, session_id="actual-synthetic-session")
    await delivery.delivery(claim, "unknown", session_id="actual-synthetic-session")
    assert await delivery.claim(message) is None


async def test_absent_feishu_preserves_actual_phone_and_password_composition(
    environment: _Environment,
) -> None:
    kwargs = {
        key: value for key, value in environment.kwargs.items() if not key.startswith("feishu_")
    }
    bundle = build_canonical_business(environment.path, **kwargs)
    assert bundle.feishu_routes is None
    assert bundle.feishu_bindings is None and bundle.feishu_delivery is None
    pair = await _login(environment, bundle)
    assert (await bundle.snapshots.snapshot(pair.access_token)).member.owner_id == _IDS[
        "homeMember"
    ]
    with sqlite3.connect(environment.path) as db:
        db.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (_PASSWORD["passwordHash"], _IDS["homeMember"]),
        )
        db.execute(
            "INSERT INTO client_delivery_policies VALUES ('platform','default',?,'fixture',1,1)",
            ('{"loginMethod":"password"}',),
        )
    password = await bundle.password.login_sudowork(username="home", password=_PASSWORD["password"])
    snapshot = await bundle.snapshots.snapshot(password.access_token)
    assert snapshot.key_id == "password-login"
    refreshed = await bundle.sessions.refresh(password.refresh_token)
    assert (await bundle.snapshots.snapshot(refreshed.access_token)).key_id == "password-login"
    with pytest.raises(MossworkError):
        await bundle.sessions.switch_organization(password.access_token, _IDS["companyOrg"])


@pytest.mark.parametrize(
    "missing",
    ["feishu_channel", "feishu_plugin_id", "feishu_connection_owner_id", "feishu_sessions"],
)
def test_partial_feishu_registration_is_refused(environment: _Environment, missing: str) -> None:
    with pytest.raises(TypeError):
        build_canonical_business(environment.path, **{**environment.kwargs, missing: None})


@pytest.mark.parametrize(
    "patch",
    [
        {"sms_sender": None},
        {"writer": None},
        {"writer": True},
        {"phone_config": None},
        {"product_projections": {}},
        {"feishu_channel": {"channel_id": "channel-a", "app_id": "cli_app_a"}},
    ],
)
def test_missing_real_port_never_becomes_a_default(
    environment: _Environment, patch: dict[str, Any]
) -> None:
    before = environment.path.read_bytes()
    with pytest.raises(TypeError):
        build_canonical_business(environment.path, **{**environment.kwargs, **patch})
    assert environment.path.read_bytes() == before
    assert environment.sender.sent == [] and environment.writer.calls == 0


@pytest.mark.parametrize("patch", [{"authority_id": ""}, {"secret": ""}, {"issuer": ""}])
def test_missing_original_authority_configuration_has_no_fallback(
    environment: _Environment, patch: dict[str, Any]
) -> None:
    with pytest.raises((ValueError, MossworkError)):
        build_canonical_business(environment.path, **{**environment.kwargs, **patch})
    assert environment.sender.sent == [] and environment.writer.calls == 0


async def test_foreign_issuer_and_missing_database_are_refused(environment: _Environment) -> None:
    foreign = build_canonical_business(
        environment.path, **{**environment.kwargs, "issuer": "another-controlled-issuer"}
    )
    with pytest.raises(MossworkError) as error:
        await foreign.snapshots.snapshot(_token("sms_home"))
    assert error.value.status == 401
    missing = environment.path.parent / "not-created.sqlite"
    bundle = build_canonical_business(missing, **environment.kwargs)
    with pytest.raises(MossworkError) as error:
        await bundle.snapshots.snapshot(_token("sms_home"))
    assert error.value.status == 503
    with pytest.raises(PhoneAuthError) as send_error:
        await bundle.sessions.send_code(_NUMBER)
    assert send_error.value.status == 503
    assert not missing.exists() and environment.sender.sent == []


async def test_actual_writer_denial_prevents_sms_and_canonical_write(
    environment: _Environment,
) -> None:
    environment.writer.is_allowed = False
    before = environment.path.read_bytes()
    with pytest.raises(MossworkError) as error:
        await environment.bundle.sessions.send_code(_NUMBER)
    assert error.value.status == 503
    assert environment.path.read_bytes() == before and environment.sender.sent == []


def test_product_canonical_database_mix_is_refused(environment: _Environment) -> None:
    canonical_pool = SqlitePool(environment.path)
    try:
        kwargs = {
            **environment.kwargs,
            "product_projections": CanonicalIdentityProjectionRepo(canonical_pool),
        }
        with pytest.raises(ValueError, match="must be separate"):
            build_canonical_business(environment.path, **kwargs)
    finally:
        canonical_pool.close()


def test_existing_hardlink_alias_cannot_be_a_product_database(environment: _Environment) -> None:
    alias = environment.path.parent / "same-canonical-hardlink.sqlite"
    alias.hardlink_to(environment.path)
    assert alias != environment.path and alias.samefile(environment.path)
    alias_pool = SqlitePool(alias)
    try:
        kwargs = {
            **environment.kwargs,
            "product_projections": CanonicalIdentityProjectionRepo(alias_pool),
        }
        with pytest.raises(ValueError, match="must be separate"):
            build_canonical_business(environment.path, **kwargs)
    finally:
        alias_pool.close()


def test_bundle_maps_exact_existing_app_contract_and_is_frozen(environment: _Environment) -> None:
    bundle = environment.bundle
    kwargs = bundle.app_kwargs()
    parameters = inspect.signature(build_app).parameters
    # Product queue admission is supplied separately by the one product factory.
    assert "canonical_admissions" in parameters
    assert set(kwargs) == {
        name
        for name in parameters
        if name.startswith("canonical_") and name != "canonical_admissions"
    }
    assert len(kwargs) == 11 and kwargs["canonical_identity"] is bundle.snapshots
    assert kwargs["canonical_requests"] is bundle.requests
    assert _SECRET not in repr(bundle)
    with pytest.raises(FrozenInstanceError):
        bundle.sessions = None  # type: ignore[misc]


async def test_same_business_bundle_profile_and_fresh_original_actor_reauthentication(
    environment: _Environment,
) -> None:
    from octop.infra.identity.request_principal import authenticate, reauthenticate

    profile_fixture = json.loads(
        (_FIXTURES / "self_profile_canonical_legacy.json").read_text(encoding="utf-8")
    )
    with sqlite3.connect(environment.path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        assert {"display_name", "password_hash"} <= columns
        avatar_ddl = profile_fixture["extension"].split("CREATE TABLE user_profile_avatars", 1)[1]
        db.executescript("CREATE TABLE user_profile_avatars" + avatar_ddl)
    bundle = environment.bundle
    pair = await _login(environment)
    principal = await authenticate(bundle.requests, pair.access_token)
    refreshed = await bundle.sessions.refresh(pair.refresh_token)
    renewed = await reauthenticate(
        bundle.requests, refreshed.access_token, principal.identity.projection
    )
    assert renewed.identity.projection == principal.identity.projection
    assert renewed.identity.snapshot.credential_id != principal.identity.snapshot.credential_id
    assert bundle.app_kwargs()["canonical_profile"] is bundle.profile
    assert (await bundle.profile.profile(refreshed.access_token)).member_id == _IDS["homeMember"]
    await bundle.profile.update_nickname(refreshed.access_token, "Unified member profile")
    assert (
        await bundle.profile.profile(refreshed.access_token)
    ).nickname == "Unified member profile"


class _LoginReadiness:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def assert_available(self, method: str, organization_id: str) -> None:
        self.calls.append((method, organization_id))


def test_phone_setting_and_sender_do_not_install_login_policy_writer(
    environment: _Environment,
) -> None:
    assert environment.bundle.phone is not None
    assert environment.kwargs["phone_enabled"]() is True
    assert environment.bundle.organization_login_policy is None
    assert environment.bundle.platform_login_policy is None


def test_policy_service_reuses_original_business_instances_without_constructor_io(
    environment: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    readiness = _LoginReadiness()

    def forbid_database(*args: Any, **kwargs: Any):
        raise AssertionError("Policy construction must not open either database")

    monkeypatch.setattr(sqlite3, "connect", forbid_database)
    bundle = build_canonical_business(
        environment.path, **environment.kwargs, login_policy_readiness=readiness
    )
    service = bundle.organization_login_policy
    assert service is not None
    assert service._snapshots is bundle.snapshots is bundle.sessions.snapshots
    assert service._writer is environment.writer
    assert service._clock is environment.kwargs["clock"]
    assert service._readiness is readiness
    assert readiness.calls == [] and environment.writer.calls == 0
    assert len(bundle.app_kwargs()) == 11
    assert "canonical_organization_login_policy" not in bundle.app_kwargs()


async def test_policy_operations_use_the_same_canonical_human_and_selected_org(
    environment: _Environment,
) -> None:
    readiness = _LoginReadiness()
    bundle = build_canonical_business(
        environment.path, **environment.kwargs, login_policy_readiness=readiness
    )
    token = (await _login(environment, bundle)).access_token
    assert bundle.organization_login_policy is not None
    await bundle.organization_login_policy.set_method(token, "cas")
    assert readiness.calls == [("cas", _IDS["homeOrg"])]
    with sqlite3.connect(environment.path) as db:
        row = db.execute(
            "SELECT policy_json,updated_by FROM client_delivery_policies "
            "WHERE scope_type='organization' AND scope_id=?",
            (_IDS["homeOrg"],),
        ).fetchone()
    assert json.loads(row[0])["loginMethod"] == 2 and row[1] == _IDS["homeMember"]
    await bundle.organization_login_policy.inherit(token)
    assert readiness.calls == [("cas", _IDS["homeOrg"])]
    assert await bundle.policy.effective_method(_IDS["homeOrg"]) == "sms"


@pytest.mark.parametrize("readiness", [False, True, object(), lambda: True])
def test_malformed_readiness_does_not_construct_a_policy_service(
    environment: _Environment, readiness: Any
) -> None:
    before = environment.path.read_bytes()
    with pytest.raises(TypeError, match="readiness port"):
        build_canonical_business(
            environment.path, **environment.kwargs, login_policy_readiness=readiness
        )
    assert environment.path.read_bytes() == before
    assert environment.writer.calls == 0


class _PlatformLoginReadiness:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def assert_platform_available(self, method: str) -> None:
        self.calls.append(method)


def test_platform_policy_construction_keeps_scope_ports_separate_and_does_no_io(
    environment: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    readiness = _PlatformLoginReadiness()

    def forbid_database(*args: Any, **kwargs: Any):
        raise AssertionError("Platform policy construction must not open either database")

    monkeypatch.setattr(sqlite3, "connect", forbid_database)
    bundle = build_canonical_business(
        environment.path, **environment.kwargs, platform_login_policy_readiness=readiness
    )
    service = bundle.platform_login_policy
    assert service is not None
    assert service._snapshots is bundle.snapshots is bundle.sessions.snapshots
    assert service._writer is environment.writer
    assert service._clock is environment.kwargs["clock"] and service._readiness is readiness
    assert bundle.organization_login_policy is None
    assert readiness.calls == [] and environment.writer.calls == 0
    assert len(bundle.app_kwargs()) == 11
    assert "canonical_platform_login_policy" not in bundle.app_kwargs()


async def test_same_business_platform_policy_keeps_org_override_and_requires_super_admin(
    environment: _Environment,
) -> None:
    organization, platform = _LoginReadiness(), _PlatformLoginReadiness()
    bundle = build_canonical_business(
        environment.path,
        **environment.kwargs,
        login_policy_readiness=organization,
        platform_login_policy_readiness=platform,
    )
    token = (await _login(environment, bundle)).access_token
    assert bundle.platform_login_policy is not None and bundle.organization_login_policy is not None
    with pytest.raises(MossworkError) as forbidden:
        await bundle.platform_login_policy.set_method(token, "cas")
    assert forbidden.value.status == 403 and platform.calls == []
    with sqlite3.connect(environment.path) as db:
        db.execute("UPDATE users SET role='super_admin' WHERE id=?", (_IDS["homeMember"],))
    token = (await _login(environment, bundle)).access_token
    await bundle.organization_login_policy.set_method(token, "sms")
    await bundle.platform_login_policy.set_method(token, "cas")
    assert organization.calls == [("sms", _IDS["homeOrg"])] and platform.calls == ["cas"]
    assert await bundle.policy.effective_method(_IDS["homeOrg"]) == "sms"
    assert await bundle.policy.effective_method(ignore_organization_policy=True) == "cas"


@pytest.mark.parametrize("readiness", [False, True, object(), lambda: True, _LoginReadiness()])
def test_org_or_malformed_port_never_installs_platform_policy(
    environment: _Environment, readiness: Any
) -> None:
    before = environment.path.read_bytes()
    with pytest.raises(TypeError, match="platform login-method readiness"):
        build_canonical_business(
            environment.path, **environment.kwargs, platform_login_policy_readiness=readiness
        )
    assert environment.path.read_bytes() == before and environment.writer.calls == 0


def test_org_readiness_alone_does_not_install_platform_policy(environment: _Environment) -> None:
    bundle = build_canonical_business(
        environment.path, **environment.kwargs, login_policy_readiness=_LoginReadiness()
    )
    assert bundle.organization_login_policy is not None and bundle.platform_login_policy is None


def test_actual_sms_sender_does_not_install_configuration_vault(environment: _Environment) -> None:
    assert environment.bundle.phone is not None
    assert environment.bundle.managed_sms_configuration is None


def test_managed_sms_service_uses_same_business_without_constructor_io(
    environment: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Vault:
        async def get_secret(self, namespace: str, key: str) -> str | None:
            raise AssertionError("Builder must not read secrets")

        async def put_secret(self, namespace: str, key: str, value: str) -> None:
            raise AssertionError("Builder must not stage secrets")

    def refuse_database(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Builder must not open storage")

    vault = Vault()
    monkeypatch.setattr(sqlite3, "connect", refuse_database)
    bundle = build_canonical_business(
        environment.path, **environment.kwargs, sms_configuration_vault=vault
    )
    service = bundle.managed_sms_configuration
    assert (
        service is not None and service._snapshots is bundle.snapshots is bundle.sessions.snapshots
    )
    assert service._writer is environment.writer and service._vault is vault
    assert service._clock is environment.kwargs["clock"] and environment.writer.calls == 0
    assert len(bundle.app_kwargs()) == 11 and "managed_sms_configuration" not in bundle.app_kwargs()


@pytest.mark.parametrize("vault", [False, True, object(), _PlatformLoginReadiness()])
def test_readiness_or_malformed_port_never_becomes_a_sms_vault(
    environment: _Environment, vault: Any
) -> None:
    before = environment.path.read_bytes()
    with pytest.raises(TypeError, match="SMS vault"):
        build_canonical_business(
            environment.path, **environment.kwargs, sms_configuration_vault=vault
        )
    assert environment.path.read_bytes() == before and environment.writer.calls == 0
