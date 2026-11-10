"""Application consumer checks; these fixtures do not qualify a native runtime."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from octop.infra.native_host.context import (
    BackendHostContextRequest,
    CohostSessionBinding,
    HostBinding,
    HostSelection,
    HostUnavailable,
    ResolvedHost,
    TrustedHostContext,
    resolve_trusted_host,
)


def binding() -> HostBinding:
    return HostBinding(
        account_id="original-human-account",
        organization_id="selected-company",
        selection=HostSelection("product-agent", "product-thread"),
        native=CohostSessionBinding(
            owner_id="canonical-member-uuid",
            agent_id="original-native-agent-home",
            durable_session_id="original-native-durable-session",
            repository_path="original-owned-repository",
        ),
    )


class Runtime:
    def __init__(self, original: HostBinding) -> None:
        self.original = original
        self.is_current = True
        self.admission_error: Exception | None = None
        self.check_entered: asyncio.Event | None = None
        self.check_release: asyncio.Event | None = None
        self.admission_entered: asyncio.Event | None = None
        self.admission_release: asyncio.Event | None = None

    async def check_current(self, current: HostBinding) -> None:
        if self.check_entered is not None and self.check_release is not None:
            self.check_entered.set()
            await self.check_release.wait()
        if not self.is_current or current != self.original:
            raise HostUnavailable("Original host/endpoint is no longer current")

    async def check_admission(self, current: HostBinding) -> None:
        if current != self.original:
            raise PermissionError("Foreign allocation")
        if self.admission_entered is not None and self.admission_release is not None:
            self.admission_entered.set()
            await self.admission_release.wait()
        if self.admission_error is not None:
            raise self.admission_error


class Authority:
    def __init__(self) -> None:
        self.principal = object()
        self.current = binding()
        self.runtime = Runtime(self.current)
        self.result = ResolvedHost(self.current, self.runtime)
        self.is_member = True
        self.grants = {
            ("sudograph_context", "registered-model"),
            ("model.invoke", "owner-model-mount"),
        }
        self.error: Exception | None = None

    async def resolve(self, principal: object, selection: HostSelection) -> ResolvedHost:
        if principal is not self.principal or selection != self.current.selection:
            raise PermissionError("Foreign product selection")
        return self.result

    async def check_current(self, principal: object, current: HostBinding) -> None:
        if self.error is not None:
            raise self.error
        if not self.is_member or principal is not self.principal or current != self.current:
            raise PermissionError("Original membership/binding revoked")

    async def authorize(
        self, principal: object, current: HostBinding, operation: str, resource_ref: str
    ) -> None:
        await self.check_current(principal, current)
        if (operation, resource_ref) not in self.grants:
            raise PermissionError("Current action grant revoked")


async def context(authority: Authority) -> TrustedHostContext:
    return await resolve_trusted_host(authority.principal, authority.current.selection, authority)


def request() -> BackendHostContextRequest:
    original = binding()
    return {
        "sessionId": original.selection.thread_id,
        "nativeSessionId": original.native.durable_session_id,
    }


@pytest.mark.asyncio
async def test_native_and_product_session_ids_remain_distinct() -> None:
    authority = Authority()
    host = await context(authority)
    read_count = 0

    async def read_context() -> str:
        nonlocal read_count
        read_count += 1
        return f"Current model revision {read_count}"

    resolve = host.host_context_resolver("registered-model", read_context)
    assert await resolve(request()) == "Current model revision 1"  # type: ignore[arg-type]
    assert await resolve(request()) == "Current model revision 2"  # type: ignore[arg-type]
    assert host.binding.native.owner_id != host.binding.account_id
    assert host.binding.native.agent_id != host.binding.selection.agent_id


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["sessionId", "nativeSessionId"])
async def test_wrong_session_never_reads_model_context(field: str) -> None:
    host = await context(Authority())

    async def read_context() -> str:
        pytest.fail("A foreign session must not read model context")

    resolve = host.host_context_resolver("registered-model", read_context)
    wrong = {**request(), field: "another-session"}
    with pytest.raises(PermissionError):
        await resolve(wrong)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selection",
    [HostSelection("foreign", "product-thread"), HostSelection("product-agent", "foreign")],
)
async def test_foreign_product_selection_is_refused(selection: HostSelection) -> None:
    authority = Authority()
    with pytest.raises(PermissionError):
        await resolve_trusted_host(authority.principal, selection, authority)


@pytest.mark.asyncio
async def test_numeric_projection_is_not_an_authenticated_principal() -> None:
    authority = Authority()
    with pytest.raises(PermissionError):
        await resolve_trusted_host(1, authority.current.selection, authority)


@pytest.mark.asyncio
async def test_resolver_cannot_substitute_another_thread() -> None:
    authority = Authority()
    forged = replace(authority.current, selection=HostSelection("product-agent", "foreign"))
    authority.result = ResolvedHost(forged, authority.runtime)
    with pytest.raises(PermissionError):
        await context(authority)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["account", "organization", "owner", "agent", "durable", "repository"]
)
async def test_changed_binding_after_read_discards_the_text(change: str) -> None:
    authority = Authority()
    host = await context(authority)

    async def read_context() -> str:
        original = authority.current
        native_fields = {
            "owner": "owner_id",
            "agent": "agent_id",
            "durable": "durable_session_id",
            "repository": "repository_path",
        }
        if change in native_fields:
            authority.current = replace(
                original,
                native=replace(original.native, **{native_fields[change]: "replacement"}),
            )
        else:
            field_name = "account_id" if change == "account" else "organization_id"
            authority.current = replace(original, **{field_name: "replacement"})
        return "This result must never reach the engine"

    with pytest.raises(PermissionError):
        await host.host_context_resolver("registered-model", read_context)(request())  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["membership", "grant", "host", "closed"])
async def test_revocation_during_domain_read_is_refused(change: str) -> None:
    authority = Authority()
    host = await context(authority)
    entered, release = asyncio.Event(), asyncio.Event()

    async def read_context() -> str:
        entered.set()
        await release.wait()
        return "Protected current model context"

    task = asyncio.create_task(
        host.host_context_resolver("registered-model", read_context)(request())  # type: ignore[arg-type]
    )
    await entered.wait()
    if change == "membership":
        authority.is_member = False
    elif change == "grant":
        authority.grants.clear()
    elif change == "host":
        authority.runtime.is_current = False
    else:
        host.close()
    release.set()
    with pytest.raises((PermissionError, HostUnavailable)):
        await task


@pytest.mark.asyncio
async def test_membership_change_during_native_check_is_refused() -> None:
    authority = Authority()
    host = await context(authority)
    authority.runtime.check_entered, authority.runtime.check_release = (
        asyncio.Event(),
        asyncio.Event(),
    )
    task = asyncio.create_task(host.check_current())
    await authority.runtime.check_entered.wait()
    authority.is_member = False
    authority.runtime.check_release.set()
    with pytest.raises(PermissionError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["membership", "grant", "host"])
async def test_admission_cannot_publish_ready_after_revocation(change: str) -> None:
    authority = Authority()
    host = await context(authority)
    authority.runtime.admission_entered, authority.runtime.admission_release = (
        asyncio.Event(),
        asyncio.Event(),
    )
    task = asyncio.create_task(host.check_ready("model.invoke", "owner-model-mount"))
    await authority.runtime.admission_entered.wait()
    if change == "membership":
        authority.is_member = False
    elif change == "grant":
        authority.grants.clear()
    else:
        authority.runtime.is_current = False
    authority.runtime.admission_release.set()
    with pytest.raises((PermissionError, HostUnavailable)):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [PermissionError("DLP not applied"), OSError("private-token")])
async def test_unavailable_or_denied_admission_never_reports_ready(error: Exception) -> None:
    authority = Authority()
    host = await context(authority)
    authority.runtime.admission_error = error
    with pytest.raises((PermissionError, HostUnavailable)) as caught:
        await host.check_ready("model.invoke", "owner-model-mount")
    assert "private-token" not in str(caught.value)


@pytest.mark.asyncio
async def test_unknown_operation_or_model_does_not_read_context() -> None:
    host = await context(Authority())
    with pytest.raises(PermissionError):
        await host.authorize("model.invoke", "another-owner-model")

    async def read_context() -> str:
        pytest.fail("Missing registered model grant must prevent the read")

    with pytest.raises(PermissionError):
        await host.host_context_resolver("foreign-model", read_context)(request())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_read_failure_has_no_cached_context_fallback() -> None:
    authority = Authority()
    host = await context(authority)
    should_fail = False

    async def read_context() -> str:
        if should_fail:
            raise OSError("private-path-and-credential")
        return "Revision A"

    resolve = host.host_context_resolver("registered-model", read_context)
    assert await resolve(request()) == "Revision A"  # type: ignore[arg-type]
    should_fail = True
    with pytest.raises(HostUnavailable) as caught:
        await resolve(request())  # type: ignore[arg-type]
    assert "private-path-and-credential" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", " \n ", None, {"text": "not a domain string"}])
async def test_empty_or_malformed_context_is_refused(value: object) -> None:
    host = await context(Authority())

    async def read_context() -> str:
        return value  # type: ignore[return-value]

    with pytest.raises(HostUnavailable):
        await host.host_context_resolver("registered-model", read_context)(request())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_cancellation_is_preserved() -> None:
    host = await context(Authority())
    entered, release = asyncio.Event(), asyncio.Event()

    async def read_context() -> str:
        entered.set()
        await release.wait()
        return "Never published"

    task = asyncio.create_task(
        host.host_context_resolver("registered-model", read_context)(request())  # type: ignore[arg-type]
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_context_withdrawal_never_reopens_or_stops_another_writer() -> None:
    authority = Authority()
    host = await context(authority)
    await host.check_ready("model.invoke", "owner-model-mount")
    host.close()
    with pytest.raises(HostUnavailable):
        await host.check_ready("model.invoke", "owner-model-mount")
    assert authority.runtime.is_current  # No native stop/cleanup claim.


@pytest.mark.asyncio
async def test_current_authority_errors_do_not_expose_private_details() -> None:
    authority = Authority()
    authority.error = OSError("private-credential")
    with pytest.raises(HostUnavailable) as caught:
        await context(authority)
    assert "private-credential" not in str(caught.value)


@pytest.mark.asyncio
async def test_malformed_authority_result_is_not_a_ready_host() -> None:
    class BrokenAuthority(Authority):
        async def resolve(self, principal: object, selection: HostSelection) -> ResolvedHost:
            return None  # type: ignore[return-value]

    authority = BrokenAuthority()
    with pytest.raises(HostUnavailable):
        await context(authority)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["application", "native", "grant", "admission"])
@pytest.mark.parametrize("result", [False, True, {"allowed": False}])
async def test_malformed_provider_completion_cannot_grant_ready(
    monkeypatch: pytest.MonkeyPatch, stage: str, result: object
) -> None:
    authority = Authority()
    host = await context(authority)

    async def malformed_completion(*_args: object) -> object:
        return result

    target, method = {
        "application": (authority, "check_current"),
        "native": (authority.runtime, "check_current"),
        "grant": (authority, "authorize"),
        "admission": (authority.runtime, "check_admission"),
    }[stage]
    monkeypatch.setattr(target, method, malformed_completion)
    with pytest.raises(HostUnavailable):
        await host.check_ready("model.invoke", "owner-model-mount")


@pytest.mark.parametrize("value", ["", "\x00", 1, None])
def test_binding_requires_explicit_string_values(value: object) -> None:
    with pytest.raises(ValueError):
        HostSelection(value, "thread")  # type: ignore[arg-type]


def test_binding_does_not_accept_untyped_native_identifiers() -> None:
    with pytest.raises(ValueError):
        replace(binding(), native={"owner_id": "guessed"})  # type: ignore[arg-type]
