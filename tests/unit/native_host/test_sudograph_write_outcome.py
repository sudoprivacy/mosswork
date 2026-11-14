"""A post-effect refusal must retain original model-write recovery custody."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tests.unit.db.test_retained_cohost import _change
from tests.unit.db.test_retained_cohost import retained as retained
from tests.unit.db.test_retained_ontology import registered as registered
from tests.unit.native_host.test_sudograph import Setup, rows
from tests.unit.native_host.test_sudograph import prepared as prepared

from octop.infra.sudograph.protocol import SudographError, ToolResult


def request() -> dict[str, str]:
    return {"base_revision": "a" * 64, "operation_id": "original-operation", "model": "model"}


@pytest.mark.parametrize("refusal", ["revoked", "malformed", "unavailable", "registry"])
async def test_post_reply_model_write_refusal_is_unconfirmed(prepared: Setup, refusal: str) -> None:
    async def fail_after_reply() -> None:
        if refusal == "revoked":
            prepared.grants.allowed.remove("sudograph_model_apply")
        elif refusal == "malformed":
            prepared.grants.completion = {"allowed": True}
        elif refusal == "unavailable":

            async def unavailable(*_: Any) -> None:
                raise RuntimeError("private grant service credential diagnostic")

            prepared.ports._require_model_access = unavailable
        else:
            _change(prepared.database, "UPDATE ontology_projects SET generation=8")

    prepared.transport.before_return = fail_after_reply
    with pytest.raises(SudographError) as failure:
        await prepared.client.apply(request())
    assert failure.value.code == "MODEL_WRITE_UNCONFIRMED"
    assert "original operation_id" in failure.value.recovery
    assert "private grant" not in str(failure.value)
    assert len(prepared.transport.calls) == 1
    assert prepared.transport.calls[0][2]["operation_id"] == "original-operation"
    assert [row["status"] for row in rows(prepared)] == ["error"]


async def test_malformed_host_write_reply_retains_possible_write(prepared: Setup) -> None:
    malformed: Any = {"text": "not a ToolResult"}
    prepared.transport.result = malformed
    with pytest.raises(SudographError) as failure:
        await prepared.client.apply(request())
    assert failure.value.code == "MODEL_WRITE_UNCONFIRMED"
    assert len(prepared.transport.calls) == 1


async def test_post_write_refusal_plus_sql_audit_failure_keeps_recovery(prepared: Setup) -> None:
    async def revoke_and_break_audit() -> None:
        prepared.grants.allowed.clear()
        _change(prepared.database, "DROP TABLE mcp_audit_log")

    prepared.transport.before_return = revoke_and_break_audit
    with pytest.raises(SudographError) as failure:
        await prepared.client.apply(request())
    assert failure.value.code == "AUDIT_UNAVAILABLE"
    assert "original operation_id" in failure.value.recovery
    assert "do not submit a new operation ID" in failure.value.recovery
    assert len(prepared.transport.calls) == 1
    assert prepared.transport.calls[0][2]["operation_id"] == "original-operation"


@pytest.mark.parametrize("operation", ["read", "write"])
async def test_pre_effect_and_read_only_refusals_keep_denial(
    prepared: Setup, operation: str
) -> None:
    if operation == "write":
        prepared.grants.allowed.remove("sudograph_model_apply")
        call = prepared.client.apply(request())
    else:

        async def revoke() -> None:
            prepared.grants.allowed.clear()

        prepared.transport.before_return = revoke
        call = prepared.client.contract()
    with pytest.raises(SudographError) as failure:
        await call
    assert failure.value.code == "ACCESS_DENIED"
    assert len(prepared.transport.calls) == (0 if operation == "write" else 1)


@pytest.mark.parametrize("revoke", [False, True])
async def test_compiler_error_does_not_become_possible_success(
    prepared: Setup, revoke: bool
) -> None:
    prepared.transport.result = ToolResult(
        [
            {
                "type": "text",
                "text": '{"code":"STALE_REVISION","reason":"changed","recovery":"read"}',
            }
        ],
        is_error=True,
    )
    if revoke:

        async def denied() -> None:
            prepared.grants.allowed.clear()

        prepared.transport.before_return = denied
    with pytest.raises(SudographError) as failure:
        await prepared.client.apply(request())
    assert failure.value.code == ("ACCESS_DENIED" if revoke else "STALE_REVISION")
    assert len(prepared.transport.calls) == 1


@pytest.mark.parametrize("audit_available", [True, False])
async def test_post_reply_real_task_cancel_remains_cancelled(
    prepared: Setup, audit_available: bool
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_on_post_authorization() -> None:
        prepared.grants.entered, prepared.grants.release = entered, release

    prepared.transport.before_return = wait_on_post_authorization
    task = asyncio.create_task(prepared.client.apply(request()))
    await asyncio.wait_for(entered.wait(), timeout=8)
    if not audit_available:
        _change(prepared.database, "DROP TABLE mcp_audit_log")
    task.cancel("original post-write cancellation")
    with pytest.raises(asyncio.CancelledError, match="original post-write cancellation") as failure:
        await task
    assert task.cancelled()
    assert len(prepared.transport.calls) == 1
    assert prepared.transport.calls[0][2]["operation_id"] == "original-operation"
    if not audit_available:
        assert failure.value.__notes__ == [
            "AUDIT_UNAVAILABLE: canonical model audit did not complete"
        ]


@pytest.mark.parametrize("content", ["{", "[]"])
@pytest.mark.parametrize("audit_fails", [False, True])
async def test_non_error_write_decode_failure_retains_original_recovery(
    prepared: Setup, content: str, audit_fails: bool
) -> None:
    prepared.transport.result = ToolResult(content, is_error=False)
    if audit_fails:

        async def fail_after_reply() -> None:
            _change(prepared.database, "DROP TABLE mcp_audit_log")

        prepared.transport.before_return = fail_after_reply
    with pytest.raises(SudographError) as failure:
        await prepared.client.apply(request())
    assert failure.value.code == ("AUDIT_UNAVAILABLE" if audit_fails else "MODEL_WRITE_UNCONFIRMED")
    assert "original operation_id" in failure.value.recovery
    assert "do not submit a new operation ID" in failure.value.recovery
    assert len(prepared.transport.calls) == 1
    assert prepared.transport.calls[0][2]["operation_id"] == "original-operation"
    if not audit_fails:
        assert [row["status"] for row in rows(prepared)] == ["error"]


@pytest.mark.parametrize("content", ["{", "[]"])
async def test_host_rejects_malformed_write_before_domain_decode(
    prepared: Setup, content: str
) -> None:
    prepared.transport.result = ToolResult(content, is_error=False)
    with pytest.raises(SudographError) as failure:
        await prepared.ports.call_tool(prepared.ports.binding, "sudograph_model_apply", request())
    assert failure.value.code == "MODEL_WRITE_UNCONFIRMED"
    assert "original operation_id" in failure.value.recovery
    assert len(prepared.transport.calls) == 1
    assert prepared.transport.calls[0][2]["operation_id"] == "original-operation"
    assert rows(prepared) == []
