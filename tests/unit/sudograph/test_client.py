"""Domain boundary checks; these fixture grants are not production identity acceptance."""

from __future__ import annotations

import asyncio
import copy
import json
import traceback
from typing import Any

import pytest

from octop.infra.sudograph.client import Operation, SudographClient
from octop.infra.sudograph.protocol import (
    ModelBinding,
    ModelPointer,
    ReviewDocument,
    SudographError,
    ToolResult,
)

BINDING = ModelBinding("authority", "model", "project", ("source-b", "source-a", "source-a"))
CONTEXT = {
    "format": "sudograph-agent-context/v1",
    "revision": "a" * 64,
    "context_digest": "b" * 64,
    "text": "Exact canonical model context",
}


class Ports:
    def __init__(self) -> None:
        self.authorizations: list[tuple[ModelBinding, Operation]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.audits: list[tuple[str, str]] = []
        self.deny_at = 0
        self.error: BaseException | None = None
        self.audit_error = False
        self.authorization_result: Any = None
        self.audit_result: Any = None
        self.on_authorize: Any = None
        self.result = ToolResult(json.dumps({"revision": "a" * 64}))
        self.response = {
            "format": "sudograph-worker-review/v2",
            "authority_ref": "authority",
            "model_ref": "model",
            "resource_ref": "project",
            "source_refs": ["source-a", "source-b"],
            "scope": "full-registered-model",
            "result": {"html": "<html>checked</html>"},
        }

    async def authorize(self, binding: ModelBinding, operation: Operation) -> None:
        self.authorizations.append((binding, operation))
        if self.on_authorize:
            self.on_authorize()
        if self.deny_at == len(self.authorizations):
            raise PermissionError("private authority diagnostic")
        return self.authorization_result

    async def call_tool(
        self, binding: ModelBinding, name: Operation, arguments: dict[str, Any]
    ) -> ToolResult:
        assert binding == BINDING
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return self.result

    async def read_review(self, binding: ModelBinding, request: dict[str, Any]) -> dict[str, Any]:
        assert binding == BINDING
        self.calls.append(("review", request))
        return self.response

    async def audit(self, binding: ModelBinding, operation: Operation, status: str) -> None:
        assert binding == BINDING
        self.audits.append((operation, status))
        if self.audit_error:
            raise RuntimeError("private audit storage diagnostic")
        return self.audit_result


def test_binding_is_frozen_and_sources_have_canonical_order() -> None:
    assert BINDING.source_refs == ("source-a", "source-b")
    with pytest.raises(AttributeError):
        BINDING.model_ref = "another"  # type: ignore[misc]


@pytest.mark.parametrize("reference", ["../model", "https://worker", "model\n", ""])
def test_binding_refuses_paths_urls_and_malformed_ids(reference: str) -> None:
    with pytest.raises(ValueError):
        ModelBinding("authority", reference, "project", ())


@pytest.mark.parametrize("deny_at", [1, 2])
def test_live_authorization_prevents_request_or_result_delivery(deny_at: int) -> None:
    ports = Ports()
    ports.deny_at = deny_at
    with pytest.raises(SudographError) as failure:
        asyncio.run(SudographClient(BINDING, ports).contract())
    assert failure.value.code == "ACCESS_DENIED"
    assert "private" not in str(failure.value)
    assert len(ports.calls) == deny_at - 1
    assert ports.audits == [("sudograph_model_contract", "error")]


@pytest.mark.parametrize("value", [False, True, {}, "allowed", 0])
@pytest.mark.parametrize("phase", ["pre", "post", "audit"])
def test_host_assertion_verdict_is_not_silently_accepted(value: Any, phase: str) -> None:
    ports = Ports()
    if phase == "audit":
        ports.audit_result = value
    elif phase == "pre":
        ports.authorization_result = value
    else:

        def malformed_post_assertion() -> None:
            if len(ports.authorizations) == 2:
                ports.authorization_result = value

        ports.on_authorize = malformed_post_assertion
    with pytest.raises(SudographError) as refused:
        asyncio.run(SudographClient(BINDING, ports).contract())
    assert refused.value.code == ("AUDIT_UNAVAILABLE" if phase == "audit" else "HOST_CONTRACT")
    assert len(ports.calls) == (0 if phase == "pre" else 1)


@pytest.mark.parametrize(
    ("key", "wrong"),
    [
        ("format", "v0"),
        ("authority_ref", "other"),
        ("model_ref", "other"),
        ("resource_ref", "other"),
        ("source_refs", ["source-b", "source-a"]),
        ("scope", "context-only"),
        ("extra", "untrusted"),
    ],
)
def test_review_refuses_cross_binding_scope_or_profile(key: str, wrong: Any) -> None:
    ports = Ports()
    ports.response[key] = wrong
    with pytest.raises(SudographError) as failure:
        asyncio.run(SudographClient(BINDING, ports).review({"operation": "head"}))
    assert failure.value.code == "WORKER_CONTRACT"
    assert len(ports.authorizations) == 1
    assert ports.audits == [("review", "error")]


def test_review_resource_comes_only_from_host_and_response_is_owned_snapshot() -> None:
    ports = Ports()

    def mutate_after_read() -> None:
        if len(ports.authorizations) == 2:
            ports.response["result"]["html"] = "changed behind authorization await"

    ports.on_authorize = mutate_after_read
    result = asyncio.run(SudographClient(BINDING, ports).review({"operation": "head"}))
    assert ports.calls == [("review", {"operation": "head", "resource": "project"})]
    assert result["result"]["html"] == "<html>checked</html>"
    assert ports.audits == [("review", "success")]


@pytest.mark.parametrize("missing", ["revision", "context_digest", "html"])
def test_review_document_requires_the_pointer_of_the_same_html_snapshot(missing: str) -> None:
    ports = Ports()
    ports.response["result"] = {
        "revision": "a" * 64,
        "context_digest": "b" * 64,
        "html": "<html>original revision</html>",
    }
    del ports.response["result"][missing]
    with pytest.raises(SudographError) as failure:
        asyncio.run(
            SudographClient(BINDING, ports).review(
                {"operation": "document", "endpoint": "/review/" + "c" * 32 + "/v1"}
            )
        )
    assert failure.value.code == "WORKER_CONTRACT"
    assert ports.audits == [("review", "error")]


def test_review_document_captures_same_response_without_reading_latest_context() -> None:
    ports = Ports()
    original = {"revision": "a" * 64, "context_digest": "b" * 64, "html": "<html>原始</html>"}
    ports.response["result"] = copy.deepcopy(original)

    def mutate_during_post_authorization() -> None:
        if len(ports.authorizations) == 2:
            ports.response["result"].update(revision="d" * 64, context_digest="e" * 64, html="new")

    ports.on_authorize = mutate_during_post_authorization
    endpoint = "/review/" + "c" * 32 + "/v1"
    document = asyncio.run(SudographClient(BINDING, ports).review_document(endpoint))
    assert document.pointer == ModelPointer(original["revision"], original["context_digest"])
    assert document.html == original["html"]
    assert ports.calls == [
        ("review", {"operation": "document", "endpoint": endpoint, "resource": "project"})
    ]
    assert ports.audits == [("review", "success")]
    with pytest.raises(AttributeError):
        document.pointer = ModelPointer("f" * 64, "f" * 64)  # type: ignore[misc]


def test_review_record_survives_sqlite_reopen_without_a_head_or_model_reader(tmp_path: Any) -> None:
    import sqlite3

    document = ReviewDocument(
        BINDING,
        ModelPointer("a" * 64, "b" * 64),
        "/review/" + "c" * 32 + "/v1",
        "<html>原始</html>",
    )
    path = tmp_path / "fixture-host.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE fixture_review(record TEXT, html TEXT)")
        connection.execute(
            "INSERT INTO fixture_review VALUES (?, ?)",
            (json.dumps(document.record()), document.html),
        )
    with sqlite3.connect(path) as connection:
        record, html = connection.execute("SELECT record, html FROM fixture_review").fetchone()
    assert ReviewDocument.restore(BINDING, json.loads(record), html) == document
    # This fixture stores domain metadata only. It does not establish original
    # session provenance, native authority or a production persistence adapter.


@pytest.mark.parametrize(
    "key,wrong",
    [
        ("format", "legacy"),
        ("authority_ref", "another"),
        ("model_ref", "another"),
        ("resource_ref", "another"),
        ("source_refs", ["source-c"]),
        ("revision", "d" * 64),
        ("context_digest", "e" * 64),
        ("endpoint", "/review/" + "f" * 32 + "/v1"),
        ("html_sha256", "f" * 64),
        ("record_sha256", "f" * 64),
        ("unexpected", "value"),
    ],
)
def test_review_restore_refuses_inconsistent_stored_metadata(key: str, wrong: Any) -> None:
    document = ReviewDocument(
        BINDING, ModelPointer("a" * 64, "b" * 64), "/review/" + "c" * 32 + "/v1", "old"
    )
    record = document.record()
    record[key] = wrong
    with pytest.raises(SudographError) as failure:
        ReviewDocument.restore(BINDING, record, document.html)
    assert failure.value.code == "WORKER_CONTRACT"


def test_review_restore_refuses_missing_digest_changed_html_or_another_binding() -> None:
    document = ReviewDocument(
        BINDING, ModelPointer("a" * 64, "b" * 64), "/review/" + "c" * 32 + "/v1", "old"
    )
    legacy = document.record()
    del legacy["context_digest"]
    for binding, record, html in (
        (BINDING, legacy, document.html),
        (BINDING, document.record(), "new"),
        (
            ModelBinding("authority", "another", "project", BINDING.source_refs),
            document.record(),
            document.html,
        ),
    ):
        with pytest.raises(SudographError):
            ReviewDocument.restore(binding, record, html)


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "head", "resource": "other"},
        {"operation": "head", "actor": "admin"},
        {"operation": "export"},
        {"operation": "document", "endpoint": "https://evil/review"},
        {"operation": "document", "endpoint": "/review/" + "a" * 32 + "/v1\n"},
        {"operation": "page", "params": {"limit": float("nan")}},
        {"operation": "page", "params": None},
    ],
)
def test_bad_review_request_has_no_host_or_network_effect(payload: dict[str, Any]) -> None:
    ports = Ports()
    with pytest.raises(ValueError):
        asyncio.run(SudographClient(BINDING, ports).review(payload))
    assert ports.authorizations == ports.calls == ports.audits == []


@pytest.mark.parametrize("field", ["actor", "source", "sql", "path", "resource"])
def test_tool_cannot_supply_execution_authority(field: str) -> None:
    ports = Ports()
    request = {"revision": "r", "context_digest": "d", "id": "catalog", field: "x"}
    with pytest.raises(ValueError):
        asyncio.run(SudographClient(BINDING, ports).read(request))
    assert not ports.calls


@pytest.mark.parametrize("limit", [0, 101, True, "2"])
def test_record_page_keeps_existing_strict_bound(limit: Any) -> None:
    ports = Ports()
    with pytest.raises(ValueError):
        asyncio.run(
            SudographClient(BINDING, ports).read(
                {"revision": "r", "context_digest": "d", "id": "catalog", "limit": limit}
            )
        )
    assert not ports.calls


def test_pending_request_cannot_be_retargeted_by_caller() -> None:
    ports = Ports()
    request = {"revision": "r", "context_digest": "d", "id": "catalog"}
    ports.on_authorize = lambda: request.update(id="another")
    asyncio.run(SudographClient(BINDING, ports).read(request))
    assert ports.calls[0][1]["id"] == "catalog"


def test_context_checks_semantic_pointer_without_hashing_rendered_text() -> None:
    ports = Ports()
    packet = {key: value for key, value in ports.response.items() if key != "resource_ref"}
    packet.pop("result")
    packet.update(format="sudograph-worker-context/v1", context=copy.deepcopy(CONTEXT))
    ports.result = ToolResult(json.dumps(packet))
    assert asyncio.run(SudographClient(BINDING, ports).read_context()) == CONTEXT
    packet["context"]["context_digest"] = "not-a-digest"
    ports.result = ToolResult(json.dumps(packet))
    with pytest.raises(SudographError) as failure:
        asyncio.run(SudographClient(BINDING, ports).read_context())
    assert failure.value.code == "WORKER_CONTRACT"


def test_host_context_uses_fresh_authorized_text_and_has_no_cached_fallback() -> None:
    ports = Ports()
    packet = {key: value for key, value in ports.response.items() if key != "resource_ref"}
    packet.pop("result")
    packet.update(format="sudograph-worker-context/v1", context=copy.deepcopy(CONTEXT))
    client = SudographClient(BINDING, ports)
    ports.result = ToolResult(json.dumps(packet))
    assert asyncio.run(client.host_context()) == CONTEXT["text"]
    packet["context"].update(revision="c" * 64, text="Updated reviewed business context")
    ports.result = ToolResult(json.dumps(packet))
    assert asyncio.run(client.host_context()) == packet["context"]["text"]
    ports.deny_at = 5
    with pytest.raises(SudographError) as denied:
        asyncio.run(client.host_context())
    assert denied.value.code == "ACCESS_DENIED"
    assert len(ports.calls) == 2


@pytest.mark.parametrize("value", ["", "not-a-digest", "A" * 64, "a" * 64 + "\n", 1, None])
def test_comment_pointer_requires_exact_revision_and_semantic_digest(value: Any) -> None:
    with pytest.raises(ValueError):
        ModelPointer(value, "b" * 64)
    with pytest.raises(ValueError):
        ModelPointer("a" * 64, value)


@pytest.mark.parametrize("target_id", ["catalog", "a translated label", "document/doc", ""])
def test_comment_node_helper_refuses_non_node_reads_before_host_io(target_id: str) -> None:
    ports = Ports()
    with pytest.raises(ValueError):
        asyncio.run(
            SudographClient(BINDING, ports).resolve_review_target(
                ModelPointer("a" * 64, "b" * 64), target_id
            )
        )
    assert ports.authorizations == ports.calls == ports.audits == []


@pytest.mark.parametrize("kind", ["schema_object", "schema_field", "business_definition"])
def test_comment_node_uses_original_pointer_and_exact_identity(kind: str) -> None:
    target_id = (
        "sg-business/concept/id" if kind == "business_definition" else "sg-source/db/object/id"
    )
    node = {"id": target_id, "kind": kind, "label": "Name can change"}
    if kind == "schema_object":
        node["definition"] = "CREATE VIEW example AS SELECT 1 AS value"
    response = {"definition": node, "related": {}} if kind == "business_definition" else node
    ports = Ports()
    ports.result = ToolResult(json.dumps(response))
    pointer = ModelPointer("a" * 64, "b" * 64)
    result = asyncio.run(SudographClient(BINDING, ports).resolve_review_target(pointer, target_id))
    assert result == {
        "revision": pointer.revision,
        "context_digest": pointer.context_digest,
        "target": response,
    }
    assert ports.calls == [
        (
            "sudograph_context_read",
            {
                "revision": pointer.revision,
                "context_digest": pointer.context_digest,
                "id": target_id,
            },
        )
    ]
    assert len(ports.authorizations) == 2


@pytest.mark.parametrize(
    "response",
    [
        {"id": "another", "kind": "schema_object"},
        {"rows": [[1, "private"]]},
        {"definition": []},
        {"id": "sg-source/db/object/id", "kind": []},
    ],
)
def test_comment_node_rejects_retargeted_or_malformed_compiler_results(response: Any) -> None:
    ports = Ports()
    ports.result = ToolResult(json.dumps(response))
    with pytest.raises(SudographError) as refused:
        asyncio.run(
            SudographClient(BINDING, ports).resolve_review_target(
                ModelPointer("a" * 64, "b" * 64), "sg-source/db/object/id"
            )
        )
    assert refused.value.code == "COMMENT_TARGET_MISMATCH"
    assert len(ports.calls) == 1


def test_comment_revision_failure_does_not_retry_with_current_pointer() -> None:
    ports = Ports()
    ports.result = ToolResult(
        json.dumps(
            {
                "code": "CONTEXT_STALE",
                "reason": "Digest mismatch",
                "recovery": "Use the original revision",
            }
        ),
        is_error=True,
    )
    with pytest.raises(SudographError) as refused:
        asyncio.run(
            SudographClient(BINDING, ports).resolve_review_target(
                ModelPointer("a" * 64, "b" * 64), "sg-source/db/object/id"
            )
        )
    assert refused.value.code == "CONTEXT_STALE"
    assert len(ports.calls) == 1


def test_compiler_diagnostic_reaches_repair_loop_without_retry() -> None:
    ports = Ports()
    diagnostic = {
        "code": "COMPILER_REJECTED",
        "reason": "Unknown source ID",
        "recovery": "Read catalog",
    }
    ports.result = ToolResult(json.dumps(diagnostic), is_error=True)
    request = {"base_revision": "r", "operation_id": "same-id", "model": "invalid"}
    with pytest.raises(SudographError) as failure:
        asyncio.run(SudographClient(BINDING, ports).apply(request))
    assert failure.value.code == diagnostic["code"]
    assert failure.value.recovery == diagnostic["recovery"]
    assert ports.calls == [("sudograph_model_apply", request)]


def test_transport_failure_hides_secrets_and_does_not_retry_uncertain_write() -> None:
    ports = Ports()
    ports.error = RuntimeError("https://host?token=secret /private/compiler")
    with pytest.raises(SudographError) as failure:
        asyncio.run(
            SudographClient(BINDING, ports).apply(
                {"base_revision": "r", "operation_id": "keep-this-id", "model": "yaml"}
            )
        )
    assert failure.value.code == "MODEL_OPERATION_UNAVAILABLE"
    assert "secret" not in str(failure.value)
    assert len(ports.calls) == 1
    assert ports.calls[0][1]["operation_id"] == "keep-this-id"


def test_cancellation_propagates_and_has_no_automatic_retry() -> None:
    ports = Ports()
    ports.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(SudographClient(BINDING, ports).contract())
    assert len(ports.calls) == 1
    assert ports.audits == [("sudograph_model_contract", "error")]


@pytest.mark.parametrize("phase", ["pre", "tool", "post"])
@pytest.mark.parametrize("audit_failure", ["raise", "verdict"])
def test_real_task_cancellation_survives_failed_audit_without_replaying_write(
    phase: str, audit_failure: str
) -> None:
    async def exercise() -> None:
        entered = asyncio.Event()

        async def pause() -> None:
            entered.set()
            await asyncio.Event().wait()

        class PausingPorts(Ports):
            async def authorize(self, binding: ModelBinding, operation: Operation) -> None:
                await super().authorize(binding, operation)
                if (phase == "pre" and len(self.authorizations) == 1) or (
                    phase == "post" and len(self.authorizations) == 2
                ):
                    await pause()

            async def call_tool(
                self, binding: ModelBinding, name: Operation, arguments: dict[str, Any]
            ) -> ToolResult:
                result = await super().call_tool(binding, name, arguments)
                if phase == "tool":
                    await pause()
                return result

        ports = PausingPorts()
        ports.audit_error = audit_failure == "raise"
        if audit_failure == "verdict":
            ports.audit_result = False
        client = SudographClient(BINDING, ports)
        request = {"base_revision": "r", "operation_id": "original-write", "model": "yaml"}
        task = asyncio.create_task(client.apply(request))
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert task.cancel()
        with pytest.raises(asyncio.CancelledError) as failure:
            await task
        assert task.cancelled()
        assert failure.value.__notes__ == [
            "AUDIT_UNAVAILABLE: canonical model audit did not complete"
        ]
        assert "private audit storage diagnostic" not in "".join(
            traceback.format_exception(failure.value)
        )
        assert len(ports.calls) == (0 if phase == "pre" else 1)
        assert all(arguments == request for _, arguments in ports.calls)
        assert ports.audits == [("sudograph_model_apply", "error")]

    asyncio.run(exercise())


def test_audit_failure_is_a_failure_not_a_successful_read() -> None:
    ports = Ports()
    ports.audit_error = True
    with pytest.raises(SudographError) as failure:
        asyncio.run(SudographClient(BINDING, ports).contract())
    assert failure.value.code == "AUDIT_UNAVAILABLE"
    assert "private" not in str(failure.value)


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", [], [7], [{"type": "image"}], [{"type": "text", "text": "{}"}] * 2],
)
def test_malformed_tool_payload_is_refused(content: Any) -> None:
    with pytest.raises(SudographError):
        ToolResult(content).decode()
