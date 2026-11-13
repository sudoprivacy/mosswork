"""Probe/read races using explicit IPC fixtures, not native/grant acceptance."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from tests.unit.native_host.test_sdk import allowed, config, wait_until

from octop.infra.native_host.sdk import (
    ControllerSdkCommand,
    ControllerSdkConnection,
    ControllerSdkError,
)


def command(tmp_path, scenario):
    fixture = Path(__file__).parent / "fixtures/read_controller_stub.py"
    entry = tmp_path / (scenario + ".py")
    entry.write_bytes(fixture.read_bytes())
    return ControllerSdkCommand(Path(sys.executable).resolve(), entry)


def frames(tmp_path):
    log = tmp_path / "frames.jsonl"
    return (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )


async def connect(tmp_path, scenario="echo", **kwargs):
    return await ControllerSdkConnection.connect(
        command(tmp_path, scenario),
        config(tmp_path),
        before_connect=allowed,
        before_dispatch=allowed,
        required_operations=frozenset({"file.read"}),
        **kwargs,
    )


async def test_probe_precedes_configuration_and_read_returns_exact_bytes_on_same_connection(
    tmp_path,
):
    assertions = []

    async def current(*args):
        assertions.append(args)

    sdk = await ControllerSdkConnection.connect(
        command(tmp_path, "echo"),
        config(tmp_path),
        before_connect=current,
        before_dispatch=current,
        required_operations=frozenset({"file.read", "session.open"}),
    )
    try:
        assert await sdk.read_file("/original/transcript") == b"original\x00\xff\r\n"
        assert await sdk.read_file("/original/empty") == b""
        assert await sdk.call("managed_agent.get_session_v1", "{}", "") == "{}"
        observed = frames(tmp_path)
        assert observed[0] == {"type": "capabilities"}
        assert observed[1] == config(tmp_path).bootstrap()
        assert observed[2:] == [
            {"type": "read", "id": 1, "path": "/original/transcript"},
            {"type": "read", "id": 2, "path": "/original/empty"},
            {"type": "call", "id": 3, "method": "managed_agent.get_session_v1", "payload": "{}"},
        ]
        assert assertions == [
            (),
            (),
            (),
            ("controller.file.read", "/original/transcript"),
            ("controller.file.read", "/original/transcript"),
            ("controller.file.read", "/original/empty"),
            ("controller.file.read", "/original/empty"),
            ("managed_agent.get_session_v1", "{}"),
        ]
    finally:
        await sdk.close()


@pytest.mark.parametrize(
    "scenario",
    ["missing", "bad-version", "duplicate-op", "unknown-op", "premature-ready"],
)
async def test_missing_or_malformed_probe_refuses_before_configuration(tmp_path, scenario):
    with pytest.raises(ControllerSdkError, match="CONNECT_UNAVAILABLE") as failure:
        await connect(tmp_path, scenario)
    assert not failure.value.is_outcome_uncertain
    assert frames(tmp_path) == [{"type": "capabilities"}]


async def test_repeated_probe_cannot_rebind_the_selected_component(tmp_path):
    with pytest.raises(ControllerSdkError, match="CONNECT_UNAVAILABLE"):
        await connect(tmp_path, "repeat-probe")
    assert all(frame["type"] in {"capabilities", "connect"} for frame in frames(tmp_path))


async def test_revocation_after_probe_prevents_configuration_and_credential_access(tmp_path):
    checked = 0

    async def current():
        nonlocal checked
        checked += 1
        if checked == 2:
            raise PermissionError("private revoke")

    with pytest.raises(PermissionError, match="connection access denied"):
        await ControllerSdkConnection.connect(
            command(tmp_path, "echo"),
            config(tmp_path),
            before_connect=current,
            before_dispatch=allowed,
            required_operations=frozenset({"file.read"}),
        )
    assert checked == 2 and frames(tmp_path) == [{"type": "capabilities"}]


async def test_read_requires_selected_probe_even_on_ready_legacy_carrier(tmp_path):
    from tests.unit.native_host.test_sdk import connect as legacy_connect

    sdk = await legacy_connect(tmp_path)
    try:
        with pytest.raises(ControllerSdkError, match="READ_UNAVAILABLE"):
            await sdk.read_file("/original/transcript")
    finally:
        await sdk.close()


@pytest.mark.parametrize(
    "scenario",
    ["read-failed", "too-large", "lost", "bad-base64", "noncanonical", "foreign-id", "oversize"],
)
async def test_failed_or_invalid_read_is_uncertain_never_empty_missing_or_retried(
    tmp_path, scenario
):
    sdk = await connect(tmp_path, scenario)
    try:
        with pytest.raises(ControllerSdkError) as failure:
            await sdk.read_file("/original/transcript")
        assert failure.value.is_outcome_uncertain
        assert len(frames(tmp_path)) == 3
        assert frames(tmp_path)[2] == {"type": "read", "id": 1, "path": "/original/transcript"}
    finally:
        await sdk.close()


async def test_raw_four_mib_limit_is_supported_without_truncation(tmp_path):
    sdk = await connect(tmp_path, "maximum")
    try:
        assert await sdk.read_file("/original/transcript") == b"a" * (4 * 1024 * 1024)
    finally:
        await sdk.close()


@pytest.mark.parametrize("verdict", [False, True, {}, "allow"])
async def test_read_assertion_requires_none_before_write(tmp_path, verdict):
    async def invalid(*args):
        return verdict

    sdk = await ControllerSdkConnection.connect(
        command(tmp_path, "echo"),
        config(tmp_path),
        before_connect=allowed,
        before_dispatch=invalid,
        required_operations=frozenset({"file.read"}),
    )
    try:
        with pytest.raises(ControllerSdkError, match="DISPATCH_UNAVAILABLE"):
            await sdk.read_file("/original/transcript")
        assert len(frames(tmp_path)) == 2
    finally:
        await sdk.close()


async def test_revocation_after_read_wait_refuses_returned_private_bytes(tmp_path):
    granted = True

    async def current(*args):
        if not granted:
            raise PermissionError("private revoke")

    sdk = await ControllerSdkConnection.connect(
        command(tmp_path, "delay"),
        config(tmp_path),
        before_connect=allowed,
        before_dispatch=current,
        required_operations=frozenset({"file.read"}),
    )
    task = asyncio.create_task(sdk.read_file("/original/transcript"))
    try:
        await wait_until(lambda: len(frames(tmp_path)) == 3)
        granted = False
        with pytest.raises(PermissionError, match="operation denied"):
            await task
        assert len(frames(tmp_path)) == 3
    finally:
        await sdk.close()
        await asyncio.gather(task, return_exceptions=True)


async def test_read_waits_for_capacity_then_rechecks_original_authority(tmp_path):
    granted = True

    async def current(*args):
        if not granted:
            raise PermissionError("revoked")

    sdk = await ControllerSdkConnection.connect(
        command(tmp_path, "held-batch"),
        config(tmp_path),
        before_connect=allowed,
        before_dispatch=current,
        required_operations=frozenset({"file.read"}),
    )
    tasks = [asyncio.create_task(sdk.call("original", str(i), "")) for i in range(16)]
    try:
        await wait_until(lambda: (tmp_path / "held").exists())
        reading = asyncio.create_task(sdk.read_file("/original/transcript"))
        await asyncio.sleep(0)
        assert not reading.done()
        granted = False
        (tmp_path / "release").touch()
        assert await asyncio.gather(*tasks) == [str(i) for i in range(16)]
        with pytest.raises(PermissionError):
            await reading
        assert len(frames(tmp_path)) == 18
    finally:
        (tmp_path / "release").touch()
        await sdk.close()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_read_does_not_clear_original_uncertain_mailbox_append(tmp_path):
    sdk = await connect(tmp_path)
    try:
        with pytest.raises(ControllerSdkError, match="SDK_SESSION_FAILED"):
            await sdk.session_operation("send", "original-input")
        assert await sdk.read_file("/original/transcript") == b"original\x00\xff\r\n"
        with pytest.raises(ControllerSdkError, match="OUTCOME_UNKNOWN"):
            await sdk.session_operation("send", "replacement-or-replay")
        assert [f["type"] for f in frames(tmp_path)] == [
            "capabilities",
            "connect",
            "session",
            "read",
        ]
    finally:
        await sdk.close()
