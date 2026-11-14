"""Canonical chat on the existing WebSocket, durable queue and managed backend."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, Literal, cast

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from octop.api.common.canonical_identity import canonical_authenticated_websocket
from octop.api.routers.chat.models import UserTurnWsFrame
from octop.api.routers.chat.sse import json_chunk_default
from octop.api.routers.chat.turn import turn_has_content
from octop.i18n import tr
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.execution.admission import CanonicalRunAdmissions
from octop.infra.execution.gateway import GatewayExecution
from octop.infra.execution.service import ExecutionConflict, ExecutionService, public_frames
from octop.infra.utils.locale import resolve_request_locale


async def canonical_chat_ws(websocket: WebSocket, agent_id: str) -> None:
    """Carry one real human through admission and every replay/cancel wait.

    Missing current resource authorization refuses this consumer. Credentials
    remain inside the principal; stored actor constraints cannot authenticate a
    reconnect or grant access. Disconnect cancels observation only.
    """
    server = websocket.app.state.mosswork_server
    observer: asyncio.Task[None] | None = None

    async def close(error: MossworkError | None = None) -> None:
        code = (
            1011 if error is None or error.status == 503 else 4003 if error.status == 403 else 4001
        )
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(
                code=code,
                reason="canonical chat unavailable"
                if code == 1011
                else "current authorization required",
            )

    try:
        principal = await canonical_authenticated_websocket(websocket)
        canonical = getattr(server, "canonical", None)
        service = getattr(server, "execution", None)
        runtime = getattr(server, "app_runtime", None)
        registry = getattr(runtime, "agent_registry", None)
        gateway = getattr(runtime, "gateway", None)
        adapter = getattr(getattr(gateway, "processor", None), "execution", None)
        admissions = getattr(canonical, "admissions", None)
        backend = getattr(canonical, "backend", None)
        guard = getattr(backend, "assert_chat_current", None)
        if (
            type(service) is not ExecutionService
            or type(admissions) is not CanonicalRunAdmissions
            or type(adapter) is not GatewayExecution
            or adapter.service is not service
            or service.human_admissions is not admissions
            or admissions.repo is not service.repo
            or getattr(registry, "execution", None) is not adapter
            or getattr(registry, "_managed_backend", None) is not backend
            or not callable(guard)
        ):
            await close()
            return
        dispatcher = cast(GatewayExecution, adapter)

        async def current(thread: str, operation: Literal["submit", "observe", "cancel"]) -> None:
            await principal.revalidate()
            verdict = await guard(agent_id, thread, admission=principal, operation=operation)
            if verdict is not None:
                raise MossworkError.localized(ErrorCode.FORBIDDEN, status=403)
            await principal.revalidate()

        async def send(frame: dict[str, Any], thread: str) -> None:
            await current(thread, "observe")
            await websocket.send_text(
                json.dumps(frame, ensure_ascii=False, default=json_chunk_default)
            )

        async def observe(run: dict[str, Any], after: int, index: int) -> None:
            try:
                async for event in service.replay(
                    run["run_id"], user=run["user_id"], after=max(0, after - 1)
                ):
                    latest = await admissions.original_actor(run["run_id"], principal)
                    if latest["agent_id"] != agent_id or latest["thread_id"] != run["thread_id"]:
                        raise ExecutionConflict("original_request_selection_changed")
                    for frame in public_frames(event, run=run["run_id"], thread=run["thread_id"]):
                        if event["seq"] == after and frame["event_index"] <= index:
                            continue
                        await send(frame, run["thread_id"])
            except MossworkError as error:
                await close(error)
            except ExecutionConflict:
                await close()
            except Exception:
                await close()

        async def replace_observer(run: dict[str, Any], after: int = 0, index: int = -1) -> None:
            nonlocal observer
            if observer is not None:
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
            observer = asyncio.create_task(observe(run, after, index))

        await websocket.accept()
        while True:
            raw = await websocket.receive_text()
            await principal.revalidate()
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError
                kind = payload.get("type", "user_turn")
                if kind == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue
                if kind in {"subscribe", "cancel"}:
                    run_id = payload.get("execution_id")
                    thread = payload.get("thread_id")
                    if (
                        not isinstance(run_id, str)
                        or not run_id
                        or not isinstance(thread, str)
                        or not thread
                    ):
                        raise ValueError
                    run = await admissions.original_actor(run_id, principal)
                    if run["agent_id"] != agent_id or run["thread_id"] != thread:
                        raise ExecutionConflict("original_request_selection_changed")
                    await current(thread, "cancel" if kind == "cancel" else "observe")
                    if kind == "cancel":
                        key = payload.get("client_request_id")
                        if not isinstance(key, str) or not key:
                            raise ValueError
                        await service.io.run(
                            service.repo.command,
                            run_id,
                            user=run["user_id"],
                            key=key,
                            kind="cancel",
                            payload={},
                        )
                        await current(thread, "cancel")
                        continue
                    after, index = payload.get("event_seq", 0), payload.get("event_index", -1)
                    if (
                        type(after) is not int
                        or type(index) is not int
                        or after < 0
                        or after > run["event_seq"]
                        or index < -1
                    ):
                        raise ValueError
                    await replace_observer(run, after, index)
                    continue
                if kind != "user_turn":
                    raise ValueError
                frame = UserTurnWsFrame.model_validate(payload)
                turn = frame.to_turn_body()
                if not turn.thread_id or not turn.client_request_id or not turn_has_content(turn):
                    raise ValueError
                if turn.delivery == "steer" or turn.target_agent_ids or turn.device is not None:
                    raise ValueError
                await current(turn.thread_id, "submit")
                run = await dispatcher.admit_human_dashboard(
                    principal,
                    agent=agent_id,
                    thread=turn.thread_id,
                    key=turn.client_request_id,
                    request=turn.model_dump(mode="json", exclude_none=True),
                )
                await current(turn.thread_id, "submit")
                await replace_observer(run)
            except (MossworkError, ExecutionConflict):
                raise
            except (ValueError, ValidationError):
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "canonical_chat_frame_unavailable",
                        "message": tr(
                            "stream_errors.canonical_chat_frame_unavailable",
                            resolve_request_locale(websocket),
                        ),
                    }
                )
    except WebSocketDisconnect:
        pass
    except MossworkError as error:
        await close(error)
    except ExecutionConflict:
        await close()
    except Exception:
        await close()
    finally:
        if observer is not None:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
