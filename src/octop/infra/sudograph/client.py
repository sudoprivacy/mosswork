"""Bound domain operations through trusted host ports, without a second authority."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable
from typing import Any, Literal, Protocol

from .protocol import (
    ModelBinding,
    ModelPointer,
    ReviewDocument,
    SudographError,
    ToolResult,
    context,
    envelope,
    review_request,
    tool_request,
)

Operation = Literal[
    "sudograph_context",
    "sudograph_context_read",
    "sudograph_model_contract",
    "sudograph_model_apply",
    "review",
]


async def _require_completion(operation: Awaitable[object]) -> None:
    if await operation is not None:
        raise SudographError(
            "HOST_CONTRACT",
            "Required host assertion did not complete correctly",
            "Host assertion ports must complete with None or raise.",
        )


class HostPorts(Protocol):
    """Trusted composition implements these using its current authenticated caller.

    The host binds organization/user/agent/session/run and grant generations. It
    resolves the worker, credentials and transport restrictions; none arrive in
    browser/tool arguments. Missing authorization must refuse, never fall back.
    """

    async def authorize(self, binding: ModelBinding, operation: Operation) -> None:
        """Check current binding and exact operation/scope; deny with PermissionError."""
        ...

    async def call_tool(
        self, binding: ModelBinding, name: Operation, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def read_review(
        self, binding: ModelBinding, request: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def audit(
        self, binding: ModelBinding, operation: Operation, status: Literal["success", "error"]
    ) -> None:
        """Durable host audit. No request, response, credential or record body."""
        ...


class SudographClient:
    def __init__(self, binding: ModelBinding, ports: HostPorts) -> None:
        self._binding, self._ports = binding, ports

    async def read_context(self) -> dict[str, Any]:
        return await self._run("sudograph_context", {})

    async def host_context(self) -> str:
        """Existing hostContext v1 resolver text, freshly authorized on each call.

        The host checks the original logical/native session and input fences
        around this callback. No session ID or credential comes from this text.
        """
        return str((await self.read_context())["text"])

    async def resolve_review_target(self, pointer: ModelPointer, target_id: str) -> dict[str, Any]:
        """Resolve an admitted comment's exact readable node, without dispatch.

        The compiler owns identities and historical revision/digest matching.
        Never substitute the latest revision, a label search or a live row read.
        Host-side comment provenance, grants and durable ingress are separate.
        """
        if not isinstance(target_id, str) or not target_id.startswith(
            ("sg-source/", "sg-business/")
        ):
            raise ValueError("Require a canonical compiler node target")
        result = await self.read(
            {
                "revision": pointer.revision,
                "context_digest": pointer.context_digest,
                "id": target_id,
            }
        )
        # Source objects also have a "definition" key, containing their SQL.
        definition = result.get("definition")
        node = definition if isinstance(definition, dict) else result
        if (
            not isinstance(node, dict)
            or node.get("id") != target_id
            or node.get("kind") not in ("schema_object", "schema_field", "business_definition")
        ):
            raise SudographError(
                "COMMENT_TARGET_MISMATCH",
                "The compiler did not resolve the exact review target",
                "Use the original model revision and canonical node identity.",
            )
        return {
            "revision": pointer.revision,
            "context_digest": pointer.context_digest,
            "target": result,
        }

    async def read(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._run("sudograph_context_read", request)

    async def contract(self) -> dict[str, Any]:
        return await self._run("sudograph_model_contract", {})

    async def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        # Never retry or mint an operation ID here: a lost reply can follow a commit.
        return await self._run("sudograph_model_apply", request)

    async def review(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._run("review", request)

    async def review_document(self, endpoint: str) -> ReviewDocument:
        """Capture HTML and its pointer atomically from the same worker reply.

        Host composition must persist this document with the original session
        before serving it; this method creates no binding, queue or permission.
        """
        response = await self.review({"operation": "document", "endpoint": endpoint})
        return ReviewDocument.from_worker(self._binding, endpoint, response["result"])

    async def _run(self, operation: Operation, request: dict[str, Any]) -> dict[str, Any]:
        binding, ports = self._binding, self._ports
        arguments = (
            review_request(request) if operation == "review" else tool_request(operation, request)
        )
        status: Literal["success", "error"] = "error"
        cancelled: asyncio.CancelledError | None = None
        try:
            await _require_completion(ports.authorize(binding, operation))
            if operation == "review":
                response = await ports.read_review(
                    binding, {**arguments, "resource": binding.resource_ref}
                )
                # Own a snapshot before post-authorization yields to other tasks.
                result = copy.deepcopy(envelope(response, binding, review=True))
                if arguments["operation"] == "document":
                    ReviewDocument.from_worker(binding, arguments["endpoint"], result["result"])
            else:
                result = (await ports.call_tool(binding, operation, arguments)).decode()
                if operation == "sudograph_context":
                    result = context(envelope(result, binding, review=False)["context"])
            await _require_completion(ports.authorize(binding, operation))
            status = "success"
            return result
        except asyncio.CancelledError as error:
            cancelled = error
            raise
        except SudographError:
            raise
        except PermissionError:
            raise SudographError(
                "ACCESS_DENIED", "Registered model access denied", "Use the authorized host entry."
            ) from None
        except Exception:
            # Worker/network/provider exceptions may contain private paths or credentials.
            raise SudographError(
                "MODEL_OPERATION_UNAVAILABLE",
                "Registered model operation unavailable",
                "Check the bound worker; retry interrupted writes with their original operation ID.",
            ) from None
        finally:
            try:
                await _require_completion(ports.audit(binding, operation, status))
            except Exception:
                if cancelled is not None:
                    # Keep task cancellation observable without losing the
                    # audit failure or exposing its private storage diagnostic.
                    cancelled.add_note("AUDIT_UNAVAILABLE: canonical model audit did not complete")
                    raise cancelled from None
                raise SudographError(
                    "AUDIT_UNAVAILABLE",
                    "Canonical model audit unavailable",
                    "Ask the host operator.",
                ) from None
