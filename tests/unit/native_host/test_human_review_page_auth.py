"""Real canonical revocation/status outside the domain client's sanitizer."""

from __future__ import annotations

import asyncio
import json

import pytest
from tests.unit.connectors.test_fixed_json import server
from tests.unit.native_host.test_human_review import _IDS, change
from tests.unit.native_host.test_human_review import human as human
from tests.unit.native_host.test_human_review_documents import pages as pages
from tests.unit.native_host.test_human_review_documents import persist
from tests.unit.native_host.test_human_review_pages import read, reply, reviewed_pages
from tests.unit.native_host.test_human_review_provider import provider

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.native_host.human_review import HumanReviewReader
from octop.infra.sudograph.protocol import SudographError


def revoke(pages, status):
    snapshot = pages.human.principal.identity.snapshot
    if status == 401:
        change(
            pages.human.path,
            "INSERT INTO revoked_tokens VALUES (?,?)",
            (snapshot.credential_id, snapshot.credential_expires_at),
        )
    else:
        change(
            pages.human.path,
            "INSERT INTO moss_member_access VALUES (?,?,0,?,3)",
            (_IDS["homeOrg"], _IDS["homeMember"], _IDS["homeMember"]),
        )


@pytest.mark.parametrize("status", [401, 403])
async def test_canonical_revocation_after_one_page_reply_keeps_original_status(pages, status):
    persist(pages)
    revoked = []

    def respond(request):
        result = reply(request)
        revoke(pages, status)
        revoked.append(status)
        return result

    async with server(respond) as (port, calls):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        with pytest.raises(MossworkError) as caught:
            await read(pages)
        assert caught.value.status == status
        assert caught.value.code == (
            ErrorCode.AUTH_FAILED if status == 401 else ErrorCode.FORBIDDEN
        )
        assert len(calls) == 1 and revoked == [status]
    assert pages.human.audits == ["success", "error"]


@pytest.mark.parametrize("status", [401, 403])
async def test_canonical_revocation_during_success_audit_keeps_original_status(pages, status):
    persist(pages)
    revoked = []

    async def audit(*args):
        await pages.human.audit(*args)
        if pages.human.audits == ["success", "success"]:
            revoke(pages, status)
            revoked.append(status)

    async with server(reply) as (port, calls):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        with pytest.raises(MossworkError) as caught:
            await read(pages, factory=provider(pages.human, audit_review=audit))
        assert caught.value.status == status and revoked == [status]
        assert len(calls) == 1
    # These are original read/audit outcomes, not frame delivery receipts.
    assert pages.human.audits == ["success", "success"]


@pytest.mark.parametrize("status", [401, 403])
async def test_final_check_after_real_domain_return_keeps_canonical_status(
    pages, monkeypatch, status
):
    persist(pages)
    original = HumanReviewReader.review
    revoked = []

    async def returned(self, request):
        result = await original(self, request)
        revoke(pages, status)
        revoked.append(status)
        return result

    monkeypatch.setattr(HumanReviewReader, "review", returned)
    async with server(reply) as (port, calls):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        with pytest.raises(MossworkError) as caught:
            await read(pages)
        assert caught.value.status == status and revoked == [status]
        assert len(calls) == 1


@pytest.mark.parametrize("operation", ["document", "page"])
async def test_final_check_rechecks_independent_current_grants(pages, monkeypatch, operation):
    persist(pages)
    original = HumanReviewReader.review
    state = {"returned": False}

    async def returned(self, request):
        result = await original(self, request)
        state["returned"] = True
        return result

    async def grant(*args, **kwargs):
        await pages.human.grant(*args, **kwargs)
        if state["returned"] and kwargs["request"]["operation"] == operation:
            raise PermissionError("fixture current independent grant withdrawn")

    monkeypatch.setattr(HumanReviewReader, "review", returned)
    async with server(reply) as (port, calls):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        with pytest.raises(PermissionError):
            await read(pages, factory=provider(pages.human, require_review_access=grant))
        assert state["returned"] and len(calls) == 1


async def test_real_cancel_in_final_grant_wait_stays_cancelled_despite_canonical_revoke(
    pages, monkeypatch
):
    persist(pages)
    original = HumanReviewReader.review
    entered = asyncio.Event()
    state = {"returned": False}

    async def returned(self, request):
        result = await original(self, request)
        state["returned"] = True
        return result

    async def grant(*args, **kwargs):
        await pages.human.grant(*args, **kwargs)
        if state["returned"] and kwargs["request"]["operation"] == "page":
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(HumanReviewReader, "review", returned)
    async with server(reply) as (port, calls):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        task = asyncio.create_task(
            read(pages, factory=provider(pages.human, require_review_access=grant))
        )
        await asyncio.wait_for(entered.wait(), 5)
        revoke(pages, 401)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled() and state["returned"] and len(calls) == 1


@pytest.mark.parametrize("status", [None, 401])
async def test_transport_error_is_preserved_unless_actual_canonical_auth_withdraws(pages, status):
    persist(pages)

    def respond(request):
        if status is not None:
            revoke(pages, status)
        return (
            503,
            {"Content-Type": "application/json"},
            json.dumps({"error": "VIEW_UNAVAILABLE"}).encode(),
        )

    async with server(respond) as (port, calls):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        with pytest.raises(MossworkError if status else SudographError) as caught:
            await read(pages)
        if status:
            assert caught.value.status == status
        else:
            assert caught.value.code == "VIEW_UNAVAILABLE"
        assert len(calls) == 1


@pytest.mark.parametrize("operation", ["document", "page"])
async def test_current_full_scope_typed_denial_is_preserved_with_valid_canonical_identity(
    pages, operation
):
    persist(pages)
    state = {"page_started": False}

    async def grant(*args, **kwargs):
        await pages.human.grant(*args, **kwargs)
        actual = kwargs["request"]["operation"]
        if actual == "page":
            state["page_started"] = True
        if state["page_started"] and actual == operation:
            raise MossworkError.localized(ErrorCode.FORBIDDEN, status=403)

    async with server(reply) as (port, calls):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        with pytest.raises(MossworkError) as caught:
            await read(pages, factory=provider(pages.human, require_review_access=grant))
        assert caught.value.status == 403 and caught.value.code == ErrorCode.FORBIDDEN
        assert state["page_started"] and calls == []
        assert await pages.human.principal.revalidate() == pages.human.principal.identity
