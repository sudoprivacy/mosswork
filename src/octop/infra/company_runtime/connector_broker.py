"""Server-side broker for connectors used by the desktop company runtime."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import httpx

from octop.infra.company_runtime.connector_side_effects import (
    CREDENTIAL_CHANGE,
    ToolSideEffect,
    adapter_manages_confirmation,
    remote_tool_side_effect,
    side_effect_for,
)
from octop.infra.connectors.builder import build_http_mcp_spec
from octop.infra.connectors.custom_mcp import CUSTOM_MCP_KIND, harness_spec_for_server
from octop.infra.connectors.gateway.registry import call_gateway_tool, mcp_tools_for_kind
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.utils.asyncio import offload


class ConnectorBroker:
    """Resolve, describe and invoke only the connectors selected for a task."""

    def __init__(
        self,
        *,
        service: Any,
        audit_repo: Any,
        owner: int | None = None,
        actor: str | None = None,
        cache: dict[str, Any] | None = None,
    ) -> None:
        self.service = service
        self.audit_repo = audit_repo
        self.owner = owner if owner is not None else int(getattr(service, "owner", 0))
        self.actor = actor or str(self.owner)
        self._cache = cache if cache is not None else {}
        self._locks: dict[str, asyncio.Lock] = self._cache.setdefault("locks", {})

    @staticmethod
    def _selected(task: dict[str, Any]) -> list[str]:
        try:
            request = json.loads(task.get("request_json") or "{}")
        except (TypeError, ValueError):
            return []
        raw = request.get("mcp_servers")
        return [str(item) for item in raw if str(item).strip()] if isinstance(raw, list) else []

    @staticmethod
    def _schema(tool: Any) -> dict[str, Any]:
        schema = getattr(tool, "args_schema", None)
        if schema is None:
            return {"type": "object", "properties": {}}
        if hasattr(schema, "model_json_schema"):
            return cast(dict[str, Any], schema.model_json_schema())
        if hasattr(schema, "schema"):
            return cast(dict[str, Any], schema.schema())
        return {"type": "object", "properties": {}}

    @staticmethod
    def _safe_args(arguments: dict[str, Any]) -> dict[str, Any]:
        secret_words = (
            "token",
            "secret",
            "password",
            "credential",
            "authorization",
            "cookie",
            "key",
        )

        def redact(value: Any, key: str = "") -> Any:
            if any(word in key.lower() for word in secret_words):
                return "[redacted]"
            if isinstance(value, dict):
                return {str(k): redact(v, str(k)) for k, v in value.items()}
            if isinstance(value, list):
                return [redact(v, key) for v in value]
            return value

        return cast(dict[str, Any], redact(arguments))

    async def _audit(
        self, *, actor: str, action: str, target: str, payload: dict[str, Any]
    ) -> None:
        await offload(
            self.audit_repo.write,
            actor=actor,
            action=action,
            target=target,
            payload=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )

    def _descriptor(
        self,
        *,
        server_name: str,
        tool_name: str,
        description: str,
        schema: dict[str, Any],
        effect: ToolSideEffect,
        kind: str,
        available: bool = True,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "server_name": server_name,
            "tool_name": tool_name,
            "description": description,
            "input_schema": schema,
            "inputSchema": schema,
            "side_effect": effect.level,
            "kind": kind,
            "available": available,
            "reason": reason,
        }

    async def _load_remote(
        self, task_id: str, server_name: str, spec: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str | None]:
        key = (task_id, server_name)
        tools_cache = self._cache.setdefault("tools", {})
        if key in tools_cache:
            return cast(tuple[list[dict[str, Any]], str | None], tools_cache[key])
        from moss_harness.mcp import aload_mcp_tools

        errors: dict[str, str] = {}
        try:
            tools = await aload_mcp_tools({server_name: spec}, errors=errors)
        except Exception:
            tools = []
            errors[server_name] = "mcp_tool_load_failed"
        descriptors: list[dict[str, Any]] = []
        tool_map = self._cache.setdefault("tool_objects", {})
        for tool in tools:
            full_name = str(getattr(tool, "name", ""))
            name = full_name.removeprefix(f"{server_name}_")
            raw = {"description": str(getattr(tool, "description", "") or "")}
            effect = remote_tool_side_effect(name, raw)
            descriptors.append(
                self._descriptor(
                    server_name=server_name,
                    tool_name=name,
                    description=raw["description"],
                    schema=self._schema(tool),
                    effect=effect,
                    kind="remote",
                )
            )
            tool_map[(task_id, server_name, name)] = tool
        result = (descriptors, errors.get(server_name))
        tools_cache[key] = result
        return result

    async def list_task_tools(
        self, task: dict[str, Any], server_names: list[str] | None = None
    ) -> list[dict[str, Any]]:
        selected = self._selected(task)
        names = (
            selected
            if server_names is None
            else [str(item).strip() for item in server_names if str(item).strip()]
        )
        if set(names) - set(selected):
            raise MossworkError(
                ErrorCode.CONNECTOR_BROKER_NOT_SELECTED, "connector was not selected for this task"
            )
        descriptor_cache = self._cache.setdefault("descriptors", {})
        descriptor_key = (str(task["task_id"]), tuple(names))
        if descriptor_key in descriptor_cache:
            return cast(list[dict[str, Any]], descriptor_cache[descriptor_key])
        resolved = self.service.resolve_server_names(self.owner, names)
        result: list[dict[str, Any]] = []
        for name in names:
            item = resolved.get(name)
            if item is None:
                result.append(
                    self._descriptor(
                        server_name=name,
                        tool_name="",
                        description="",
                        schema={"type": "object", "properties": {}},
                        effect=ToolSideEffect("write", "connector unavailable"),
                        kind="unknown",
                        available=False,
                        reason="connector_unavailable",
                    )
                )
                continue
            kind = str(item["kind"])
            if kind == CUSTOM_MCP_KIND:
                spec = item.get("spec") or {}
                if str(spec.get("transport") or "").lower() == "stdio":
                    result.append(
                        self._descriptor(
                            server_name=name,
                            tool_name="",
                            description="",
                            schema={"type": "object", "properties": {}},
                            effect=ToolSideEffect("write", "stdio connectors are unavailable"),
                            kind=kind,
                            available=False,
                            reason="stdio_not_supported",
                        )
                    )
                    continue
                try:
                    descriptors, error = await self._load_remote(
                        task["task_id"], name, harness_spec_for_server(spec)
                    )
                except Exception:
                    descriptors, error = [], "mcp_tool_load_failed"
            elif item["mcp_mode"] == "gateway":
                descriptors = []
                for raw in mcp_tools_for_kind(kind):
                    tool_name = str(raw.get("name") or "")
                    if not tool_name:
                        continue
                    descriptors.append(
                        self._descriptor(
                            server_name=name,
                            tool_name=tool_name,
                            description=str(raw.get("description") or tool_name),
                            schema=cast(dict[str, Any], raw.get("inputSchema"))
                            if isinstance(raw.get("inputSchema"), dict)
                            else {"type": "object", "properties": {}},
                            effect=side_effect_for(kind, tool_name, raw),
                            kind=kind,
                        )
                    )
                error = None
            elif kind == "qcc":
                response = await self.service.handle_qcc_request(
                    item["instance_id"],
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                )
                tools = (
                    ((response.get("result") or {}).get("tools") or [])
                    if isinstance(response, dict)
                    else []
                )
                descriptors = [
                    self._descriptor(
                        server_name=name,
                        tool_name=str(raw.get("name") or ""),
                        description=str(raw.get("description") or ""),
                        schema=cast(dict[str, Any], raw.get("inputSchema"))
                        if isinstance(raw.get("inputSchema"), dict)
                        else {"type": "object", "properties": {}},
                        effect=remote_tool_side_effect(str(raw.get("name") or ""), raw),
                        kind=kind,
                    )
                    for raw in tools
                    if isinstance(raw, dict) and raw.get("name")
                ]
                error = None if descriptors else "connector_unavailable"
            else:
                try:
                    creds = await self.service.ensure_fresh_credentials(item["instance_id"], kind)
                    spec = build_http_mcp_spec(
                        entry=item["entry"],
                        instance_id=item["instance_id"],
                        creds=creds,
                        config=self.service.config,
                    )
                    descriptors, error = await self._load_remote(task["task_id"], name, spec)
                except Exception:
                    descriptors, error = [], "connector_unavailable"
            if descriptors:
                result.extend(descriptors)
            elif error:
                result.append(
                    self._descriptor(
                        server_name=name,
                        tool_name="",
                        description="",
                        schema={"type": "object", "properties": {}},
                        effect=ToolSideEffect("write", error),
                        kind=kind,
                        available=False,
                        reason=error,
                    )
                )
        descriptor_cache[descriptor_key] = result
        return result

    async def call_tool(
        self,
        task: dict[str, Any],
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        tool_call_id: str,
        idempotency_key: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        task_id = str(task["task_id"])
        if server_name not in self._selected(task):
            raise MossworkError(
                ErrorCode.CONNECTOR_BROKER_NOT_SELECTED, "connector was not selected for this task"
            )
        expected_key = f"{task_id}:{tool_call_id}"
        if idempotency_key != expected_key:
            raise MossworkError(
                ErrorCode.CONNECTOR_BROKER_IDEMPOTENCY, "invalid connector idempotency key"
            )
        key = (task_id, idempotency_key)
        results = self._cache.setdefault("results", {})
        if key in results:
            return cast(dict[str, Any], results[key])
        async with self._locks.setdefault(task_id, asyncio.Lock()):
            if key in results:
                return cast(dict[str, Any], results[key])
            descriptors = await self.list_task_tools(task, [server_name])
            descriptor = next(
                (item for item in descriptors if item["tool_name"] == tool_name), None
            )
            if descriptor is None or not descriptor["available"]:
                raise MossworkError(
                    ErrorCode.CONNECTOR_BROKER_TOOL_UNAVAILABLE,
                    descriptor.get("reason", "tool unavailable")
                    if descriptor
                    else "tool unavailable",
                )
            kind = str(descriptor.get("kind") or "")
            effect_info = side_effect_for(kind, tool_name, descriptor, arguments)
            effect = effect_info.level
            if effect == CREDENTIAL_CHANGE.level:
                raise MossworkError(
                    ErrorCode.CONNECTOR_BROKER_CREDENTIAL_CHANGE,
                    "credential changes are unavailable to model tools",
                )
            if (
                effect in {"write", "message_send"}
                and not confirmed
                and not adapter_manages_confirmation(kind, tool_name)
            ):
                raise MossworkError(
                    ErrorCode.CONNECTOR_BROKER_CONFIRMATION_REQUIRED,
                    "user confirmation is required",
                )
            await self._audit(
                actor=self.actor,
                action="company_connector.call",
                target=f"{server_name}:{tool_name}",
                payload={
                    "task_id": task_id,
                    "tool_call_id": tool_call_id,
                    "idempotency_key": idempotency_key,
                    "side_effect": effect,
                    "confirmed": confirmed,
                    "arguments": self._safe_args(arguments),
                },
            )
            item = self.service.resolve_server_names(self.owner, [server_name]).get(server_name)
            if item is None:
                raise MossworkError(
                    ErrorCode.CONNECTOR_BROKER_TOOL_UNAVAILABLE, "connector unavailable"
                )
            try:
                if item["kind"] == CUSTOM_MCP_KIND or item["mcp_mode"] == "remote":
                    if item["kind"] == CUSTOM_MCP_KIND:
                        spec = harness_spec_for_server(item["spec"])
                        await self._load_remote(task_id, server_name, spec)
                    tool = self._cache.get("tool_objects", {}).get(
                        (task_id, server_name, tool_name)
                    )
                    if tool is None:
                        raise MossworkError(
                            ErrorCode.CONNECTOR_BROKER_TOOL_UNAVAILABLE, "tool unavailable"
                        )
                    value = await tool.ainvoke(arguments)
                elif item["kind"] == "qcc":
                    value = await self.service.handle_qcc_request(
                        item["instance_id"],
                        {
                            "jsonrpc": "2.0",
                            "id": tool_call_id,
                            "method": "tools/call",
                            "params": {"name": tool_name, "arguments": arguments},
                        },
                    )
                else:
                    creds = await self.service.ensure_fresh_credentials(
                        item["instance_id"], item["kind"]
                    )
                    value = await offload(
                        call_gateway_tool, item["kind"], creds, tool_name, arguments
                    )
                result = {
                    "status": "completed",
                    "content": value
                    if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False, default=str),
                }
            except (TimeoutError, httpx.TimeoutException):
                result = {
                    "status": "unknown",
                    "error": "connector result is unknown; do not retry automatically",
                }
            except MossworkError:
                raise
            except Exception as exc:
                result = {"status": "failed", "error": str(exc)}
            results[key] = result
            return result

    def clear_task(self, task_id: str) -> None:
        """Release per-task tool/session and idempotency state after termination."""
        for name in ("tools", "tool_objects", "results", "descriptors"):
            values = self._cache.get(name)
            if not isinstance(values, dict):
                continue
            for key in list(values):
                if isinstance(key, tuple) and key and key[0] == task_id:
                    values.pop(key, None)
        self._locks.pop(task_id, None)


__all__ = ["ConnectorBroker"]
