"""Captured SQL documents and real TCP; graph grants/writer remain fixtures."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from tests.unit.connectors.test_fixed_json import server
from tests.unit.native_host.test_human_review import (
    _ENDPOINT,
    _IDS,
    _POINTER,
    change,
    response,
    review_row,
)
from tests.unit.native_host.test_human_review import human as human
from tests.unit.native_host.test_human_review_audit import Writer, rows
from tests.unit.native_host.test_human_review_documents import (
    document,
    persist,
)
from tests.unit.native_host.test_human_review_documents import pages as pages
from tests.unit.native_host.test_human_review_provider import (
    provider,
    require_worker_secret,
    resolve,
)
from tests.unit.native_host.test_human_review_provider_audit import readers

from octop.infra.connectors.tool_protocol import content_digest
from octop.infra.db.repos.retained_review import ReviewDocumentUnavailable
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.sudograph.protocol import SudographError


def page_request(*, offset=0, limit=50):
    return {
        "view": "project",
        "operation": "page",
        "params": {
            **_POINTER,
            "id": "sg-source/source/object/widgets",
            "offset": offset,
            "limit": limit,
        },
    }


def reviewed_pages(human, endpoint):
    review_row(human, endpoint)
    with sqlite3.connect(human.path) as db:
        review = json.loads(db.execute("SELECT document FROM agent_tool_reviews").fetchone()[0])
        review["tool"]["inputSchema"] = {
            "type": "object",
            "properties": {
                "revision": {"type": "string"},
                "context_digest": {"type": "string"},
                "id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "records": {"type": "boolean", "enum": [True]},
            },
            "additionalProperties": False,
            "required": ["revision", "context_digest", "id", "offset", "limit", "records"],
        }
        review["digest"] = content_digest({"protocol": review["protocol"], "tool": review["tool"]})
        db.execute("UPDATE agent_tool_reviews SET document=?", (json.dumps(review),))


def reply(request, *, pointer=None):
    selected = json.loads(request["body"])
    assert selected["operation"] == "page" and selected["resource"] == "project"
    return (
        200,
        {"Content-Type": "application/json"},
        json.dumps(
            response({**(pointer or _POINTER), "rows": [[1, "row"], [2, None], [3, "third"]]})
        ).encode(),
    )


async def read(pages, *, factory=None, request=None, endpoint=_ENDPOINT, **overrides):
    return await (factory or provider(pages.human)).read_page(
        pages.human.principal,
        project_id="project",
        endpoint=endpoint,
        **{
            "request": request if request is not None else page_request(),
            "documents": pages.store,
            "record_sha256": document(pages).record()["record_sha256"],
            **overrides,
        },
    )


async def test_page_restores_original_actor_document_and_reads_three_rows_once(pages):
    persist(pages)
    async with server(reply) as (port, requests):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        result = await read(pages)
    assert result["result"]["rows"] == [[1, "row"], [2, None], [3, "third"]]
    assert (
        len(requests) == 1 and json.loads(requests[0]["body"])["params"] == page_request()["params"]
    )
    assert {value["operation"] for value in pages.human.grant_requests} == {"document", "page"}
    assert pages.human.audits == ["success", "success"]
    assert (
        pages.store.load(
            pages.human.project,
            _ENDPOINT,
            account_id=pages.human.principal.identity.snapshot.account_id,
            actor_id=pages.human.principal.identity.snapshot.member.owner_id,
        ).record
        == document(pages).record()
    )
    with sqlite3.connect(pages.human.path) as db:
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('sessions','agents','ontology_session_bindings')"
        ).fetchall()


async def test_document_grant_does_not_admit_page_or_secret_io(pages):
    persist(pages)
    require_worker_secret(pages.human)
    denial = PermissionError("fixture document only")

    async def grant(*args, **kwargs):
        await pages.human.grant(*args, **kwargs)
        if kwargs["request"]["operation"] == "page":
            raise denial

    async def secrets(*args):
        pytest.fail("No secret capability before an actual page grant")

    factory = provider(pages.human, require_review_access=grant, list_secrets=secrets)
    with pytest.raises(PermissionError) as caught:
        await read(pages, factory=factory)
    assert caught.value is denial
    assert pages.human.audits == ["success", "error"]


async def test_original_document_reader_still_refuses_direct_page_scope(pages):
    persist(pages)
    reader = await resolve(pages.human)
    with pytest.raises(SudographError) as caught:
        await reader.review({"operation": "page", "params": page_request()["params"]})
    assert caught.value.code == "ACCESS_DENIED"


async def test_missing_committed_document_does_not_fetch_a_worker_or_create_it(pages):
    async with server(reply) as (port, requests):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        with pytest.raises(ReviewDocumentUnavailable):
            await read(pages)
        assert requests == []
    assert pages.human.audits == ["error"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("view", "other"),
        ("view", "model"),
        ("operation", "head"),
        ("revision", "d" * 64),
        ("context_digest", "e" * 64),
        ("offset", True),
        ("offset", -1),
        ("limit", 101),
    ],
)
async def test_page_never_substitutes_model_pointer_or_paging(pages, field, value):
    persist(pages)
    request = page_request()
    (request if field in ("view", "operation") else request["params"])[field] = value
    with pytest.raises(ValueError):
        await read(pages, request=request)
    assert all(value["operation"] == "document" for value in pages.human.grant_requests)


async def test_selector_cannot_restore_another_document(pages):
    persist(pages)
    with pytest.raises(ReviewDocumentUnavailable):
        await read(pages, endpoint="/review/" + "d" * 32 + "/v1")


async def test_parent_record_digest_mismatch_sends_no_page_or_secret_request(pages):
    persist(pages)
    require_worker_secret(pages.human)

    async def secrets(*args):
        pytest.fail("A stale captured document cannot select credentials")

    async with server(reply) as (port, requests):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        with pytest.raises(SudographError) as caught:
            await read(
                pages,
                factory=provider(pages.human, list_secrets=secrets),
                record_sha256="f" * 64,
            )
        assert caught.value.code == "CONTEXT_STALE" and requests == []
    assert all(value["operation"] == "document" for value in pages.human.grant_requests)


@pytest.mark.parametrize("digest", [None, True, "a" * 63, "A" * 64])
async def test_record_digest_requires_the_parsed_original_lowercase_hex(pages, digest):
    persist(pages)
    with pytest.raises(ValueError):
        await read(pages, record_sha256=digest)
    assert pages.human.grant_requests == [] and pages.human.audits == []


async def test_request_is_snapshotted_before_first_grant_wait(pages):
    persist(pages)
    entered, release = asyncio.Event(), asyncio.Event()
    request = page_request()

    async def grant(*args, **kwargs):
        await pages.human.grant(*args, **kwargs)
        if not entered.is_set():
            entered.set()
            await release.wait()

    async with server(reply) as (port, requests):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        task = asyncio.create_task(
            read(pages, factory=provider(pages.human, require_review_access=grant), request=request)
        )
        await asyncio.wait_for(entered.wait(), 5)
        request["params"]["offset"] = 50
        request["view"] = "other"
        release.set()
        await task
        assert len(requests) == 1 and json.loads(requests[0]["body"])["params"]["offset"] == 0


@pytest.mark.parametrize("verdict", [True, False, {"allowed": True}])
async def test_malformed_page_grant_is_not_an_authorization(pages, verdict):
    persist(pages)

    async def grant(*args, **kwargs):
        await pages.human.grant(*args, **kwargs)
        if kwargs["request"]["operation"] == "page":
            return verdict

    with pytest.raises(SudographError):
        await read(pages, factory=provider(pages.human, require_review_access=grant))
    assert pages.human.audits == ["success", "error"]


@pytest.mark.parametrize("withdraw", ["grant", "member", "document"])
async def test_withdrawal_after_one_page_reply_withholds_rows_without_replay(pages, withdraw):
    persist(pages)
    withdrawn = []

    def then_withdraw(request):
        value = reply(request)
        if withdraw == "grant":
            pages.human.allowed = False
        elif withdraw == "member":
            change(pages.human.path, "UPDATE users SET status='disabled'")
        else:
            with sqlite3.connect(pages.human.path) as db:
                stored = json.loads(
                    db.execute("SELECT value_json FROM ontology_review_values").fetchone()[0]
                )
                replacement = document(pages, html="<html>changed after original read</html>")
                stored["html"], stored["record"] = replacement.html, replacement.record()
                db.execute("UPDATE ontology_review_values SET value_json=?", (json.dumps(stored),))
        withdrawn.append(withdraw)
        return value

    async with server(then_withdraw) as (port, requests):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        expected_error = (
            MossworkError
            if withdraw == "member"
            else PermissionError
            if withdraw == "grant"
            else SudographError
        )
        with pytest.raises(expected_error) as caught:
            await read(pages)
        if withdraw == "member":
            assert caught.value.status == 401 and caught.value.code == ErrorCode.AUTH_FAILED
        elif withdraw == "grant":
            assert type(caught.value) is PermissionError
        assert len(requests) == 1 and withdrawn == [withdraw]
    assert pages.human.audits == ["success", "error"]


async def test_audit_commit_then_writer_loss_withholds_page_without_replay(pages):
    persist(pages)
    writer = Writer(fail_at=4)
    async with server(reply) as (port, requests):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        with pytest.raises(SudographError) as caught:
            await read(pages, factory=readers(pages.human, writer))
        assert caught.value.code == "AUDIT_UNAVAILABLE" and len(requests) == 1
    assert len(rows(pages.human)) == 2 and writer.calls == 4


@pytest.mark.parametrize("rotate", [False, True])
async def test_page_uses_original_secret_subject_and_rechecks_rotation(pages, rotate):
    persist(pages)
    secret = {"value": "fixture-original"}
    calls = []
    observed = []

    async def secrets(namespace, subject):
        assert namespace == f"org:{_IDS['homeOrg']}:system:worker-secret"
        assert subject == f"system:moss:{_IDS['homeOrg']}"
        calls.append((namespace, subject))
        return [{"key": "token", "value": secret["value"]}]

    def respond(request):
        # The existing server fixture lowercases the captured header lines.
        assert request["headers"]["authorization"] == "bearer fixture-original"
        value = reply(request)
        if rotate:
            secret["value"] = "fixture-rotated"
        observed.append(rotate)
        return value

    async with server(respond) as (port, requests):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        require_worker_secret(pages.human)
        factory = provider(pages.human, list_secrets=secrets)
        if rotate:
            with pytest.raises(SudographError):
                await read(pages, factory=factory)
        else:
            assert len((await read(pages, factory=factory))["result"]["rows"]) == 3
        assert len(requests) == 1 and calls and observed == [rotate]
    assert pages.human.audits == ["success", "error" if rotate else "success"]


async def test_document_permission_withdrawal_after_page_reply_withholds_rows(pages):
    persist(pages)
    allowed = {"document": True}

    async def grant(*args, **kwargs):
        await pages.human.grant(*args, **kwargs)
        if kwargs["request"]["operation"] == "document" and not allowed["document"]:
            raise PermissionError("fixture original document withdrawn")

    def respond(request):
        value = reply(request)
        allowed["document"] = False
        return value

    async with server(respond) as (port, requests):
        reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        with pytest.raises(PermissionError) as caught:
            await read(pages, factory=provider(pages.human, require_review_access=grant))
        assert type(caught.value) is PermissionError
        assert len(requests) == 1 and not allowed["document"]


async def test_cancel_during_actual_page_grant_remains_cancelled(pages):
    persist(pages)
    entered = asyncio.Event()

    async def grant(*args, **kwargs):
        await pages.human.grant(*args, **kwargs)
        if kwargs["request"]["operation"] == "page":
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        read(pages, factory=provider(pages.human, require_review_access=grant))
    )
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled() and pages.human.audits == ["success", "error"]
