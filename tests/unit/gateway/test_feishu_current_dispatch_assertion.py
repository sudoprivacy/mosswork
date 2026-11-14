"""Current SDK-claim dispatch proof without native lookup or new authority."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tests.unit.gateway.test_feishu_delivery_business_store import (
    _Environment,
    _execute,
    _message,
    _ready,
    _rows,
    _service,
)
from tests.unit.gateway.test_feishu_delivery_business_store import business as business
from tests.unit.gateway.test_feishu_delivery_business_store import legacy_db as legacy_db

from octop.infra.errors import MossworkError
from octop.infra.gateway.feishu_delivery import CanonicalFeishuClaim

SESSION = "native-original-session"


async def _dispatch(env: _Environment) -> CanonicalFeishuClaim:
    claim = await _ready(env, SESSION)
    assert (
        await env.service.delivery(claim, "dispatching", session_id=SESSION)
    ).status == "dispatching"
    return claim


async def test_real_sdk_claim_pointer_dispatching_current_proof_is_none_readonly_and_not_native_ready(
    business: _Environment,
) -> None:
    claim = await _dispatch(business)
    before, native_calls = business.path.read_bytes(), len(business.sessions.calls)
    # Native availability is deliberately independent. This proof accessor
    # must not preflight native credentials, sessions or runtime connections.
    business.sessions.is_available = False
    assert await business.service.assert_dispatch_current(claim, session_id=SESSION) is None
    assert await business.service.assert_dispatch_current(claim, session_id=SESSION) is None
    assert business.path.read_bytes() == before
    assert len(business.sessions.calls) == native_calls
    assert _rows(business, "moss_im_deliveries")[0][7] == "dispatching"


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE moss_im_connections SET tenant_key='other-tenant'",
        "UPDATE moss_im_connections SET revision='rotated'",
        "UPDATE moss_im_connections SET app_id='cli_other'",
        "UPDATE channel_plugins SET enabled=0",
        "DELETE FROM user_auth_identities",
        "UPDATE user_auth_identities SET id='replacement-binding'",
        "UPDATE users SET membership_revision=membership_revision+1",
        "UPDATE users SET status='disabled'",
        "UPDATE human_accounts SET status='disabled'",
        "UPDATE users SET role='user'",
        "INSERT INTO moss_member_access SELECT org_id,id,0,id,9 FROM users WHERE phone IS NOT NULL",
    ],
)
async def test_current_connection_binding_member_and_moss_changes_refuse(
    business: _Environment, mutation: str
) -> None:
    claim = await _dispatch(business)
    _execute(business, mutation)
    before, native_calls = business.path.read_bytes(), len(business.sessions.calls)
    with pytest.raises(MossworkError) as denied:
        await business.service.assert_dispatch_current(claim, session_id=SESSION)
    assert denied.value.status == 403
    assert business.path.read_bytes() == before
    assert len(business.sessions.calls) == native_calls


@pytest.mark.parametrize(
    "field", ["platform_user_id", "chat_id", "user_id", "agent_ref", "session_id", "created_at"]
)
async def test_delivery_must_remain_the_exact_original_event_owner_session_association(
    business: _Environment, field: str
) -> None:
    claim = await _dispatch(business)
    _execute(business, f"UPDATE moss_im_deliveries SET {field}='changed'")
    before = business.path.read_bytes()
    with pytest.raises(MossworkError) as denied:
        await business.service.assert_dispatch_current(claim, session_id=SESSION)
    assert denied.value.status == 409
    assert business.path.read_bytes() == before


@pytest.mark.parametrize(
    "assignments",
    [
        "user_id=NULL,agent_ref=NULL,session_id=NULL",
        "session_id=NULL",
    ],
)
async def test_dispatching_row_without_real_association_cannot_pass_bookkeeping_null_tolerance(
    business: _Environment, assignments: str
) -> None:
    claim = await _dispatch(business)
    _execute(business, "UPDATE moss_im_deliveries SET " + assignments)
    with pytest.raises(MossworkError) as denied:
        await business.service.assert_dispatch_current(claim, session_id=SESSION)
    assert denied.value.status == 409


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE moss_im_conversations SET session_id='replacement-session'",
        "DELETE FROM moss_im_conversations",
    ],
)
async def test_changed_or_removed_pointer_is_not_recovered_or_rebound(
    business: _Environment, mutation: str
) -> None:
    claim = await _dispatch(business)
    _execute(business, mutation)
    before = business.path.read_bytes()
    with pytest.raises(MossworkError) as denied:
        await business.service.assert_dispatch_current(claim, session_id=SESSION)
    assert denied.value.status == 409
    assert business.path.read_bytes() == before


@pytest.mark.parametrize("status", ["claimed", "completed", "failed", "unknown", "not-a-state"])
async def test_only_dispatching_is_admitted_and_failure_uncertainty_remain_occupied(
    business: _Environment, status: str
) -> None:
    claim = await _dispatch(business)
    _execute(business, "UPDATE moss_im_deliveries SET status=?", (status,))
    before = _rows(business, "moss_im_deliveries")
    with pytest.raises(MossworkError) as denied:
        await business.service.assert_dispatch_current(claim, session_id=SESSION)
    assert denied.value.status == 409
    assert await business.service.claim(_message()) is None
    assert _rows(business, "moss_im_deliveries") == before


async def test_unknown_original_row_is_not_created(business: _Environment) -> None:
    claim = await _dispatch(business)
    _execute(business, "DELETE FROM moss_im_deliveries")
    before = business.path.read_bytes()
    with pytest.raises(MossworkError) as denied:
        await business.service.assert_dispatch_current(claim, session_id=SESSION)
    assert denied.value.status == 409
    assert business.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["other-service", "metadata", "unsealed", "wrong-seal"])
async def test_private_receipt_not_metadata_or_another_service_proof(
    business: _Environment, kind: str
) -> None:
    claim: Any = await _dispatch(business)
    service = business.service
    if kind == "other-service":
        service = _service(business)
    elif kind == "metadata":
        claim = {"route": claim.route, "connection": claim.connection, "event": claim.event}
    elif kind in {"unsealed", "wrong-seal"}:
        claim = object.__new__(CanonicalFeishuClaim)
        if kind == "wrong-seal":
            object.__setattr__(claim, "_CanonicalFeishuClaim__seal", object())
    native_calls = len(business.sessions.calls)
    with pytest.raises(MossworkError) as denied:
        await service.assert_dispatch_current(claim, session_id=SESSION)
    assert denied.value.status == 403
    assert len(business.sessions.calls) == native_calls


async def test_real_unbound_sdk_claim_cannot_dispatch(business: _Environment) -> None:
    _execute(business, "DELETE FROM user_auth_identities")
    claim = await business.service.claim(_message())
    assert claim is not None and claim.route is None
    with pytest.raises(MossworkError) as denied:
        await business.service.assert_dispatch_current(claim, session_id=SESSION)
    assert denied.value.status == 403
    assert not business.sessions.calls


@pytest.mark.parametrize("session", ["replacement-session", "", "bad\x00session", None, 123])
async def test_exact_session_argument_required(business: _Environment, session: Any) -> None:
    claim = await _dispatch(business)
    with pytest.raises(MossworkError):
        await business.service.assert_dispatch_current(claim, session_id=session)


@pytest.mark.parametrize("change", ["moss-revoke", "terminal", "pointer"])
async def test_last_writer_await_cannot_hide_revoke_terminal_or_pointer_change(
    business: _Environment, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    claim = await _dispatch(business)
    original = business.writer.assert_current_writer
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def gate() -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await release.wait()
        return await original()

    monkeypatch.setattr(business.writer, "assert_current_writer", gate)
    pending = asyncio.create_task(
        business.service.assert_dispatch_current(claim, session_id=SESSION)
    )
    await asyncio.wait_for(entered.wait(), 5)
    if change == "moss-revoke":
        _execute(
            business,
            "INSERT INTO moss_member_access SELECT org_id,id,0,id,9 FROM users WHERE phone IS NOT NULL",
        )
    elif change == "terminal":
        assert (await business.service.delivery(claim, "unknown")).status == "unknown"
    else:
        _execute(business, "UPDATE moss_im_conversations SET session_id='changed-session'")
    release.set()
    with pytest.raises(MossworkError) as denied:
        await pending
    assert denied.value.status == (403 if change == "moss-revoke" else 409)


async def test_terminal_bookkeeping_after_revoke_stays_available_without_effect_assertion(
    business: _Environment,
) -> None:
    claim = await _dispatch(business)
    _execute(business, "DELETE FROM user_auth_identities")
    business.sessions.is_available = False
    calls = len(business.sessions.calls)
    with pytest.raises(MossworkError):
        await business.service.assert_dispatch_current(claim, session_id=SESSION)
    assert (await business.service.delivery(claim, "failed")).status == "failed"
    assert await business.service.claim(_message()) is None
    with pytest.raises(MossworkError):
        await business.service.assert_dispatch_current(claim, session_id=SESSION)
    assert len(business.sessions.calls) == calls


@pytest.mark.parametrize("result", [False, True, {}, "ok"])
async def test_read_assertion_still_requires_strict_none_writer_admission(
    business: _Environment, result: Any
) -> None:
    claim = await _dispatch(business)
    business.writer.result = result
    before = business.path.read_bytes()
    with pytest.raises(MossworkError) as denied:
        await business.service.assert_dispatch_current(claim, session_id=SESSION)
    assert denied.value.status == 503
    assert business.path.read_bytes() == before


async def test_missing_delivery_schema_is_unavailable_not_initialized(
    business: _Environment,
) -> None:
    claim = await _dispatch(business)
    _execute(business, "DROP TABLE moss_im_deliveries")
    before = business.path.read_bytes()
    with pytest.raises(MossworkError) as denied:
        await business.service.assert_dispatch_current(claim, session_id=SESSION)
    assert denied.value.status == 503
    assert business.path.read_bytes() == before
