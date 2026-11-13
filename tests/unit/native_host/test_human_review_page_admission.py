"""Same captured reader; canonical failures survive all transport/audit waits."""

import json
import sqlite3

import pytest
from tests.unit.native_host import test_human_review_pages as fixtures
from tests.unit.native_host.test_human_review import change
from tests.unit.native_host.test_human_review import human as human
from tests.unit.native_host.test_human_review_documents import pages as pages

from octop.infra.db.repos.retained_mcp import RetainedReviewedMcpRepo
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.native_host import human_review_provider as producer
from octop.infra.sudograph.protocol import SudographError


@pytest.mark.parametrize(
    "withdrawal",
    ["none", "member-after-reply", "canonical-graph-after-reply", "member-after-audit"],
)
async def test_selected_final_canonical_status_survives_existing_domain_sanitizer(
    pages, withdrawal
):
    fixtures.persist(pages)
    observed = []

    async def grant(*args, **kwargs):
        await pages.human.grant(*args, **kwargs)
        if observed and withdrawal == "canonical-graph-after-reply":
            raise MossworkError.localized(ErrorCode.FORBIDDEN, status=403)

    async def audit(*args):
        await pages.human.audit(*args)
        if observed and args[-1] == "success" and withdrawal == "member-after-audit":
            change(pages.human.path, "UPDATE users SET status='disabled'")

    def respond(request):
        reply = fixtures.reply(request)
        observed.append(True)
        if withdrawal == "member-after-reply":
            change(pages.human.path, "UPDATE users SET status='disabled'")
        return reply

    readers = producer.CanonicalHumanReviewReaders(
        projects=pages.human.projects,
        reviews=RetainedReviewedMcpRepo(pages.human.path),
        require_review_access=grant,
        audit_review=audit,
        allowed_addresses=("127.0.0.1",),
    )
    async with fixtures.server(respond) as (port, requests):
        fixtures.reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        if withdrawal == "none":
            result = await fixtures.read(pages, factory=readers)
            assert len(result["result"]["rows"]) == 3
        else:
            try:
                await fixtures.read(pages, factory=readers)
            except Exception as caught:
                observed_status = getattr(caught, "status", None)
                observed_code = getattr(caught, "code", None)
            else:
                pytest.fail("Withdrawn captured data must never be released")
            expected = 403 if withdrawal == "canonical-graph-after-reply" else 401
            expected_code = ErrorCode.FORBIDDEN if expected == 403 else ErrorCode.AUTH_FAILED
            assert (observed_status, observed_code) == (expected, expected_code)
        assert len(requests) == 1 and len(observed) == 1


@pytest.mark.parametrize("failure", ["malformed-grant", "changed-document"])
async def test_internal_refusal_keeps_original_audit_failure(pages, failure):
    fixtures.persist(pages)

    async def grant(*args, **kwargs):
        await pages.human.grant(*args, **kwargs)
        if failure == "malformed-grant" and kwargs["request"]["operation"] == "page":
            return True

    async def audit(*args):
        await pages.human.audit(*args)
        if args[-1] == "error":
            raise RuntimeError("fixture original audit unavailable")

    def respond(request):
        result = fixtures.reply(request)
        with sqlite3.connect(pages.human.path) as db:
            stored = json.loads(
                db.execute("SELECT value_json FROM ontology_review_values").fetchone()[0]
            )
            stored["html"] = "<html>changed after original page read</html>"
            db.execute("UPDATE ontology_review_values SET value_json=?", (json.dumps(stored),))
        return result

    readers = producer.CanonicalHumanReviewReaders(
        projects=pages.human.projects,
        reviews=RetainedReviewedMcpRepo(pages.human.path),
        require_review_access=grant,
        audit_review=audit,
        allowed_addresses=("127.0.0.1",),
    )
    async with fixtures.server(respond) as (port, requests):
        fixtures.reviewed_pages(pages.human, f"http://127.0.0.1:{port}/mcp")
        with pytest.raises(SudographError) as caught:
            await fixtures.read(pages, factory=readers)
        assert caught.value.code == "AUDIT_UNAVAILABLE"
        assert len(requests) == (0 if failure == "malformed-grant" else 1)
    assert pages.human.audits == ["success", "error"]
