"""High-level ACP service for delegated external agent sessions."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, TypedDict
from uuid import uuid4

from moss_harness.acp.client import ACPHostedClient
from moss_harness.acp.models import ACPConfig, ACPConfigurationError, ACPRunnerConfig, ACPSessionError
from moss_harness.acp.runtime_status import (
    RUNTIME_STATUS_BUDGET_SECONDS,
    RuntimeObservation,
    RuntimeSnapshot,
    supports_runtime_status,
)
from moss_harness.backends.utils import ASYNC_DEFENSIVE_OP_ERRORS
from moss_harness.runtime_env import load_dotenv_path, overlay_env

MessageHandler = Callable[[dict[str, Any], bool], Awaitable[None]]


class _RuntimeIdentity(TypedDict):
    instance_id: str
    session_id: str


def _finish_runtime_query(task: asyncio.Task[Any]) -> None:
    """Observe failures even when every bounded reader has already left."""
    if not task.cancelled():
        task.exception()


def _kill_process_tree(pid: int) -> None:
    try:
        import psutil
    except ImportError:
        return
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    for child in parent.children(recursive=True):
        with contextlib.suppress(psutil.NoSuchProcess):
            child.kill()
    with contextlib.suppress(psutil.NoSuchProcess):
        parent.kill()


@dataclass
class _Conversation:
    thread_id: str
    runner: str
    acp_session_id: str
    cwd: str
    conn: Any
    process: Any
    client: ACPHostedClient
    exit_stack: AsyncExitStack
    turn_lock: asyncio.Lock
    instance_id: str = field(default_factory=lambda: uuid4().hex)
    runtime_status_supported: bool = False
    runtime_status_task: asyncio.Task[Any] | None = None
    runtime_status_deadline: float = 0.0
    prompt_task: asyncio.Task[Any] | None = None


class ACPService:
    def __init__(self, *, config: ACPConfig) -> None:
        self.config = config
        self._lock = asyncio.Lock()
        self._sessions: dict[tuple[str, str], _Conversation] = {}

    async def run_turn(
        self,
        *,
        thread_id: str,
        runner: str,
        prompt_blocks: list[dict[str, Any]],
        cwd: str,
        on_message: MessageHandler,
        restart: bool = False,
        require_existing: bool = False,
    ) -> dict[str, Any]:
        if restart:
            await self.close_thread_session(thread_id=thread_id, runner=runner)

        conversation = await self._get_or_create_session(
            thread_id=thread_id,
            runner=runner,
            cwd=cwd,
            require_existing=require_existing,
        )
        async with conversation.turn_lock:
            if conversation.client.pending_permission is not None:
                raise ACPSessionError(
                    f"Session {conversation.acp_session_id} is waiting for permission",
                )
            if conversation.prompt_task is not None and not conversation.prompt_task.done():
                raise ACPSessionError(
                    f"Session {conversation.acp_session_id} is already processing a turn",
                )

            conversation.cwd = cwd or conversation.cwd
            conversation.client.update_cwd(conversation.cwd)
            conversation.client.start_prompt(on_message)
            conversation.prompt_task = asyncio.create_task(
                conversation.conn.prompt(
                    session_id=conversation.acp_session_id,
                    prompt=self._prompt_blocks_to_models(prompt_blocks),
                ),
            )
            return await self._wait_for_prompt_outcome(conversation=conversation)

    async def resume_permission(
        self,
        *,
        acp_session_id: str,
        option_id: str,
        on_message: MessageHandler,
    ) -> dict[str, Any]:
        conversation = await self._find_session_by_acp_id(acp_session_id)
        if conversation is None:
            raise ACPSessionError(f"Session not found: {acp_session_id}")
        if conversation.client.pending_permission is None:
            raise ACPSessionError(f"Session {acp_session_id} has no pending permission request")
        if conversation.prompt_task is None or conversation.prompt_task.done():
            raise ACPSessionError(f"Session {acp_session_id} is not awaiting permission resume")

        async with conversation.turn_lock:
            conversation.client.resume_prompt(on_message)
            conversation.client.resolve_permission(option_id)
            return await self._wait_for_prompt_outcome(conversation=conversation)

    async def close_thread_session(self, *, thread_id: str, runner: str) -> None:
        async with self._lock:
            conversation = self._sessions.pop((thread_id, runner), None)
        if conversation is not None:
            await self._close_conversation(conversation)

    async def close_all_sessions(self) -> None:
        async with self._lock:
            conversations = list(self._sessions.values())
            self._sessions.clear()
        for conversation in conversations:
            await self._close_conversation(conversation)

    async def get_session(self, thread_id: str, runner: str) -> _Conversation | None:
        async with self._lock:
            return self._sessions.get((thread_id, runner))

    async def get_pending_permission(self, *, thread_id: str, runner: str) -> Any | None:
        conversation = await self.get_session(thread_id, runner)
        if conversation is None:
            return None
        return conversation.client.pending_permission

    async def get_runtime_observation(self, *, thread_id: str, runner: str) -> RuntimeObservation:
        """Read this connection's engine without acquiring its in-flight turn lock."""
        conversation = await self.get_session(thread_id, runner)
        deadline = asyncio.get_running_loop().time() + RUNTIME_STATUS_BUDGET_SECONDS
        return await self._observe_runtime(conversation, deadline)

    async def get_runtime_observations(self, *, thread_id: str, runners: list[str]) -> dict[str, RuntimeObservation]:
        """Query existing connections with one deadline and four concurrent reads."""
        async with self._lock:
            conversations = [self._sessions.get((thread_id, runner)) for runner in runners]
        deadline = asyncio.get_running_loop().time() + RUNTIME_STATUS_BUDGET_SECONDS
        slots = asyncio.Semaphore(4)

        async def observe(conversation: _Conversation | None) -> RuntimeObservation:
            async with slots:
                return await self._observe_runtime(conversation, deadline)

        values = await asyncio.gather(*(observe(conversation) for conversation in conversations))
        return dict(zip(runners, values, strict=True))

    async def _observe_runtime(self, conversation: _Conversation | None, deadline: float) -> RuntimeObservation:
        if conversation is None:
            return RuntimeObservation(availability="disconnected")
        identity: _RuntimeIdentity = {
            "instance_id": conversation.instance_id,
            "session_id": conversation.acp_session_id,
        }
        if conversation.process.returncode is not None:
            return RuntimeObservation(**identity, availability="disconnected")
        if not conversation.runtime_status_supported:
            return RuntimeObservation(**identity, availability="unsupported")
        if asyncio.get_running_loop().time() >= deadline:
            return RuntimeObservation(**identity, availability="unavailable")

        from acp import RequestError

        task = conversation.runtime_status_task
        if task is None or task.done():
            # Keep one wire request per connection, including when the SDK
            # sender stalls. Readers leave at their deadline; the request
            # drains before another can be sent. Completed facts are not cached.
            task = asyncio.create_task(
                conversation.conn.ext_method("sudocode/session/status", {"sessionId": conversation.acp_session_id})
            )
            task.add_done_callback(_finish_runtime_query)
            conversation.runtime_status_task = task
            conversation.runtime_status_deadline = asyncio.get_running_loop().time() + RUNTIME_STATUS_BUDGET_SECONDS
        read_deadline = min(deadline, conversation.runtime_status_deadline)
        if asyncio.get_running_loop().time() >= read_deadline:
            return RuntimeObservation(**identity, availability="unavailable")
        try:
            async with asyncio.timeout_at(read_deadline):
                # The SDK adds the extension's leading underscore itself.
                payload = await asyncio.shield(task)
            if asyncio.get_running_loop().time() >= read_deadline:
                return RuntimeObservation(**identity, availability="unavailable")
            snapshot = RuntimeSnapshot.model_validate(payload)
        except (OSError, RuntimeError, ValueError, RequestError):
            return RuntimeObservation(**identity, availability="unavailable")
        current = await self.get_session(conversation.thread_id, conversation.runner)
        if (
            current is not conversation
            or conversation.acp_session_id != identity["session_id"]
            or conversation.process.returncode is not None
        ):
            return RuntimeObservation(**identity, availability="disconnected")
        return RuntimeObservation(**identity, availability="fresh", snapshot=snapshot)

    async def cancel_turn(self, *, thread_id: str, runner: str) -> bool:
        conversation = await self.get_session(thread_id, runner)
        if conversation is None:
            return False
        prompt_task = conversation.prompt_task
        if prompt_task is None or prompt_task.done():
            return False
        try:
            await conversation.conn.cancel(session_id=conversation.acp_session_id)
        except (OSError, RuntimeError, asyncio.CancelledError):
            return False
        try:
            await asyncio.wait_for(asyncio.shield(prompt_task), timeout=0.5)
        except TimeoutError:
            return prompt_task.done()
        except (OSError, RuntimeError, asyncio.CancelledError):
            return prompt_task.done()
        return prompt_task.done()

    def _get_runner_config(self, runner: str) -> ACPRunnerConfig:
        runner_config = self.config.runners.get(runner)
        if runner_config is None:
            raise ACPConfigurationError(f"Unknown ACP runner: {runner}", runner=runner)
        if not runner_config.enabled:
            raise ACPConfigurationError(f"ACP runner '{runner}' is disabled", runner=runner)
        return runner_config

    async def _get_or_create_session(
        self,
        *,
        thread_id: str,
        runner: str,
        cwd: str,
        require_existing: bool,
    ) -> _Conversation:
        runner_config = self._get_runner_config(runner)
        async with self._lock:
            existing = self._sessions.get((thread_id, runner))

        if existing is not None:
            if existing.process.returncode is None:
                return existing
            await self.close_thread_session(thread_id=thread_id, runner=runner)
            if require_existing:
                raise ACPSessionError(
                    f"ACP session for runner '{runner}' is no longer active; call start first",
                )
        elif require_existing:
            raise ACPSessionError(
                f"no bound ACP session found for runner '{runner}' in current thread",
            )

        conversation = await self._open_conversation(
            thread_id=thread_id,
            runner=runner,
            cwd=cwd or ".",
            runner_config=runner_config,
        )
        async with self._lock:
            self._sessions[(thread_id, runner)] = conversation
        return conversation

    async def _find_session_by_acp_id(self, acp_session_id: str) -> _Conversation | None:
        async with self._lock:
            for session in self._sessions.values():
                if session.acp_session_id == acp_session_id:
                    return session
        return None

    async def _open_conversation(
        self,
        *,
        thread_id: str,
        runner: str,
        cwd: str,
        runner_config: ACPRunnerConfig,
    ) -> _Conversation:
        from acp import PROTOCOL_VERSION, spawn_agent_process
        from acp.schema import ClientCapabilities, Implementation

        client = ACPHostedClient(runner_name=runner, runner_config=runner_config, cwd=cwd)
        exit_stack = AsyncExitStack()
        spawn_env = overlay_env(os.environ, load_dotenv_path(os.path.join(cwd, ".env")), protect=True)
        spawn_env = overlay_env(spawn_env, runner_config.env, protect=False)
        try:
            conn, process = await exit_stack.enter_async_context(
                spawn_agent_process(
                    client,  # type: ignore[arg-type]
                    runner_config.command,
                    *runner_config.args,
                    cwd=cwd,
                    env=spawn_env,
                    transport_kwargs={"limit": runner_config.stdio_buffer_limit_bytes},
                ),
            )
            initialized = await conn.initialize(
                protocol_version=PROTOCOL_VERSION,
                capabilities=ClientCapabilities(),
                client_info=Implementation(name="moss-harness-acp", version="0.1.0"),
            )
            if initialized.protocol_version != PROTOCOL_VERSION:
                raise ACPSessionError(f"Protocol mismatch: {initialized.protocol_version}")
            new_session = await conn.new_session(cwd=cwd)
            return _Conversation(
                thread_id=thread_id,
                runner=runner,
                acp_session_id=new_session.session_id,
                cwd=cwd,
                conn=conn,
                process=process,
                client=client,
                exit_stack=exit_stack,
                turn_lock=asyncio.Lock(),
                runtime_status_supported=supports_runtime_status(initialized.field_meta),
            )
        except ASYNC_DEFENSIVE_OP_ERRORS:
            await exit_stack.aclose()
            raise

    async def _wait_for_prompt_outcome(self, *, conversation: _Conversation) -> dict[str, Any]:
        prompt_task = conversation.prompt_task
        if prompt_task is None:
            raise ACPSessionError("ACP prompt task is missing")

        permission_task = asyncio.create_task(conversation.client.wait_for_permission_request())
        try:
            done, _ = await asyncio.wait(
                {prompt_task, permission_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if permission_task in done and conversation.client.pending_permission is not None:
                finished_event = await conversation.client.finish_prompt()
                return {
                    "status": "permission_required",
                    "suspended_permission": conversation.client.pending_permission,
                    "event": finished_event,
                }

            permission_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await permission_task

            try:
                await prompt_task
            except ASYNC_DEFENSIVE_OP_ERRORS as exc:
                conversation.prompt_task = None
                await conversation.client.finish_prompt()
                raise ACPSessionError(str(exc)) from exc

            conversation.prompt_task = None
            finished_event = await conversation.client.finish_prompt()
            pending = conversation.client.pending_permission
            if pending is not None:
                return {
                    "status": "permission_required",
                    "suspended_permission": pending,
                    "event": finished_event,
                }
            return {"status": "completed", "event": finished_event}
        finally:
            if not permission_task.done():
                permission_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await permission_task

    async def _close_conversation(self, conversation: _Conversation) -> None:
        try:
            if conversation.prompt_task is not None and not conversation.prompt_task.done():
                conversation.prompt_task.cancel()
                with contextlib.suppress(Exception):
                    await conversation.prompt_task
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    conversation.conn.close_session(session_id=conversation.acp_session_id),
                    timeout=5.0,
                )
        finally:
            _kill_process_tree(conversation.process.pid)
            try:
                await conversation.exit_stack.aclose()
            finally:
                task = conversation.runtime_status_task
                if task is not None:
                    if not task.done():
                        task.cancel()
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await task

    @staticmethod
    def _prompt_blocks_to_models(blocks: list[dict[str, Any]]) -> list[Any]:
        from acp import text_block

        out: list[Any] = []
        for block in blocks:
            if block.get("type") != "text":
                raise ACPConfigurationError("Only text prompt blocks are currently supported")
            out.append(text_block(str(block.get("text", ""))))
        return out


_services: dict[str, ACPService] = {}


def get_acp_service(service_key: str) -> ACPService | None:
    return _services.get(service_key)


def init_acp_service(service_key: str, config: ACPConfig) -> ACPService:
    previous = _services.get(service_key)
    _services[service_key] = ACPService(config=config)
    if previous is not None:
        _schedule_close(previous)
    return _services[service_key]


def close_acp_service(service_key: str) -> None:
    previous = _services.pop(service_key, None)
    if previous is not None:
        _schedule_close(previous)


def _schedule_close(service: ACPService) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and not loop.is_closed():
        if loop.is_running():
            loop.create_task(service.close_all_sessions())
        else:
            loop.run_until_complete(service.close_all_sessions())
        return
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(service.close_all_sessions())
        loop.close()
    except (OSError, RuntimeError):
        pass


def _shutdown_services() -> None:
    services = list(_services.values())
    _services.clear()
    if not services:
        return
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            for service in services:
                loop.create_task(service.close_all_sessions())
            return
    except RuntimeError:
        pass
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(asyncio.gather(*(s.close_all_sessions() for s in services)))
        loop.close()
    except (OSError, RuntimeError):
        pass


atexit.register(_shutdown_services)
