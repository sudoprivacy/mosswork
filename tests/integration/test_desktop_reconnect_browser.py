"""Built dashboard → desktop bridge port → real local harness and company API.

The bridge port is supplied by Playwright instead of the Go/WebView shell.
The model is deterministic; tools, checkpoints, HTTP handlers and UI are real.
Run explicitly after building dashboard assets; ordinary Python runs skip it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import socket
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import uvicorn
from langchain_core.messages import AIMessageChunk, ToolMessage
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright, expect

from octop.infra.company_runtime import runtime as runtime_module
from octop.infra.company_runtime.client import CompanyClient
from tests.support.app import mosswork_client, write_mosswork_config
from tests.support.auth import auth_header, bootstrap_admin
from tests.unit.company_runtime.test_memory_approvals import native_runtime

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("MOSSWORK_RUN_BROWSER_ACCEPTANCE") != "1",
        reason="Explicit built-dashboard browser acceptance",
    ),
]


@pytest.fixture
async def env(tmp_mosswork_home):
    """Use the existing integration fixtures with company mode enabled at boot."""
    write_mosswork_config(
        tmp_mosswork_home,
        company_runtime_enabled=True,
        device_channel_enabled=True,
    )
    async with mosswork_client(tmp_mosswork_home) as (client, server):
        await bootstrap_admin(client, tmp_mosswork_home)
        yield client, server, await auth_header(client)


@pytest.fixture
async def company_origin(env_with_agent, monkeypatch):
    """Use real HTTP so SSE deltas arrive before the model response completes."""
    client, _, _, _ = env_with_agent
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        host, port = listener.getsockname()
        server = uvicorn.Server(
            uvicorn.Config(
                client._mosswork_app,
                lifespan="off",
                log_level="warning",
                access_log=False,
                timeout_graceful_shutdown=5,
            )
        )
        monkeypatch.setattr(server, "capture_signals", nullcontext)
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        task.result()
                    await asyncio.sleep(0.01)
            yield f"http://{host}:{port}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)


async def test_built_desktop_chat_reconnect_and_cancel(
    env_with_agent, company_origin, tmp_path, monkeypatch
):
    client, server, auth, agent_id = env_with_agent
    assets = Path(__file__).resolve().parents[2] / "src/octop/dashboard"
    assert (assets / "index.html").is_file(), "Build the dashboard before this acceptance"
    started = await client.post(f"/api/agents/{agent_id}/start", headers=auth)
    assert started.status_code == 204, started.text
    created = await client.post(f"/api/agents/{agent_id}/threads", headers=auth, json={})
    assert created.is_success, created.text
    thread_id = created.json()["thread_id"]
    user_id = server.user_manager.get("admin").id
    credential = "mosswork-device.browser-acceptance"
    now = int(time.time())
    server.services.device_repo.issue_code(
        "browser-code", user_id, "browser-login", now + 60, now + 3600
    )
    server.services.device_repo.pair(
        "browser-code",
        "browser-device",
        hashlib.sha256(credential.encode()).hexdigest(),
        "Acceptance",
        "test",
        2,
        now,
    )
    company = CompanyClient(company_origin, credential)
    events = []
    runtime = native_runtime(company, tmp_path, events)
    phase = {"mode": "complete"}
    generating, release_generation, syncing, release_sync = (asyncio.Event() for _ in range(4))

    class Model:
        def bind_tools(self, _tools):
            return self

        async def astream(self, messages):
            mode = phase["mode"]
            if mode == "approval":
                if not any(
                    isinstance(message, ToolMessage)
                    and message.tool_call_id == "browser-approved-remove"
                    for message in messages
                ):
                    yield AIMessageChunk(
                        content="",
                        id="remove",
                        tool_call_chunks=[
                            {
                                "name": "execute",
                                "id": "browser-approved-remove",
                                "index": 0,
                                "args": json.dumps({"command": "rm disposable.txt"}),
                            }
                        ],
                    )
                else:
                    yield AIMessageChunk(content="Approval complete.", id="approved")
                return
            if not any(isinstance(message, ToolMessage) for message in messages):
                yield AIMessageChunk(
                    content="",
                    id="write",
                    tool_call_chunks=[
                        {
                            "name": "write_file",
                            "id": "write-once",
                            "index": 0,
                            "args": json.dumps(
                                {"file_path": "/report.txt", "content": "browser report 42"}
                            ),
                        }
                    ],
                )
                return
            answer_id = f"answer-{mode}"
            yield AIMessageChunk(content=f"Browser {mode} stream started. ", id=answer_id)
            if mode != "complete":
                generating.set()
                await release_generation.wait()
            yield AIMessageChunk(content=f"Browser {mode} stream finished.", id=answer_id)

    agent = server.app_runtime.agent_registry.get_agent(agent_id)
    agent.model_factory = SimpleNamespace(
        list_refs=lambda: ["openai/gpt-4o"],
        get_chat_model=lambda _ref: Model(),
    )
    agent.memory = None
    agent.config.pick_default_model_ref = lambda: "openai/gpt-4o"
    flush = runtime_module.flush_checkpoints

    async def hold_final_sync(*args):
        if phase["mode"] == "reload":
            syncing.set()
            await release_sync.wait()
        await flush(*args)

    monkeypatch.setattr(runtime_module, "flush_checkpoints", hold_final_sync)
    evidence = Path(os.environ.get("MOSSWORK_BROWSER_EVIDENCE", tmp_path / "browser-evidence"))
    evidence.mkdir(parents=True, exist_ok=True)
    page_errors = []
    calls = []
    queue = asyncio.Queue()
    emit = runtime.emit

    def publish(event):
        emit(event)
        queue.put_nowait(events[-1])

    runtime.emit = publish
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1000}, locale="en-US"
        )
        await context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = await context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        async def route(request_route):
            request = request_route.request
            url = urlsplit(request.url)
            assert url.netloc == "testserver", (
                "Acceptance must not access production or external APIs"
            )
            if url.path.startswith("/api/"):
                response = await client.request(
                    request.method,
                    url.path + ("?" + url.query if url.query else ""),
                    headers={
                        key: value
                        for key, value in request.headers.items()
                        if key not in {"host", "content-length"}
                    },
                    content=request.post_data_buffer,
                )
                await request_route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    headers={
                        key: value
                        for key, value in response.headers.items()
                        if key not in {"content-length", "content-encoding"}
                    },
                )
            else:
                file = assets / url.path.lstrip("/")
                if not file.is_file():
                    file = assets / "index.html"
                await request_route.fulfill(
                    body=file.read_bytes(),
                    content_type=mimetypes.guess_type(file.name)[0] or "application/octet-stream",
                )

        await page.route("**/*", route)

        async def bridge_call(method, params=None):
            calls.append(method)
            if method == "status":
                return runtime.status()
            if method == "start":
                return await runtime.start(params)
            if method == "cancel":
                return await runtime.cancel(params)
            if method == "hitl_resume":
                return runtime.steering.approvals.resolve(params)
            raise AssertionError(method)

        await page.expose_function("acceptanceRuntimeCall", bridge_call)
        await page.add_init_script("""(() => {
          const listeners = new Set();
          window.acceptanceEmit = event => { for (const fn of listeners) fn(event); };
          window.__mossworkRuntime = {
            version: 1,
            status: () => window.acceptanceRuntimeCall('status'),
            start: params => window.acceptanceRuntimeCall('start', params),
            cancel: params => window.acceptanceRuntimeCall('cancel', params),
            resumeHitl: params => window.acceptanceRuntimeCall('hitl_resume', params),
            subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn); },
            reveal: async () => {},
          };
        })();""")
        token = auth["Authorization"].removeprefix("Bearer ")
        await page.add_init_script(
            "localStorage.setItem('auth_token', "
            + json.dumps(token)
            + "); localStorage.setItem('mosswork:locale', 'en');"
        )

        async def deliver():
            while True:
                event = await queue.get()
                try:
                    await page.evaluate("event => window.acceptanceEmit?.(event)", event)
                except PlaywrightError as error:
                    # Native events during a reload are recovered from task status/history.
                    if not page.is_closed() and "Execution context was destroyed" not in str(error):
                        raise

        pump = asyncio.create_task(deliver())
        try:
            await page.goto(f"http://testserver/chat/{agent_id}/{thread_id}")
            editor = page.locator("textarea").first
            await expect(editor).to_be_visible(timeout=30000)
            await editor.fill("Create the browser acceptance report")
            await editor.press("Enter")
            await expect(
                page.get_by_text("Browser complete stream finished.", exact=False).first
            ).to_be_visible(timeout=45000)
            async with asyncio.timeout(20):
                while runtime.work:
                    await asyncio.sleep(0.02)
            target = runtime.workspace / thread_id / "report.txt"
            assert target.read_text() == "browser report 42"
            first_task = runtime.tasks[thread_id]["task_id"]
            phase["mode"] = "reload"
            await editor.fill("Continue the browser acceptance report")
            await editor.press("Enter")
            await asyncio.wait_for(generating.wait(), 30)
            await page.reload()
            await expect(page.locator("textarea").first).to_be_visible(timeout=30000)
            stop = page.get_by_role("button", name=re.compile(r"^(Stop|停止)$"))
            await expect(stop).to_be_visible(timeout=30000)
            await page.evaluate(
                "event => window.acceptanceEmit(event)",
                {
                    "event": "done",
                    "thread_id": thread_id,
                    "task_id": first_task,
                    "status": "completed",
                },
            )
            await expect(stop).to_be_visible()
            release_generation.set()
            await asyncio.wait_for(syncing.wait(), 30)
            await expect(stop).to_be_visible()
            await page.screenshot(path=str(evidence / "awaiting-runtime-completion.png"))
            release_sync.set()
            await expect(stop).to_have_count(0, timeout=20000)
            for message in ("Browser complete stream finished.", "Browser reload stream finished."):
                await expect(page.get_by_text(message, exact=False)).to_have_count(1)
            await page.reload()
            await expect(page.locator("textarea").first).to_be_visible(timeout=30000)
            for message in ("Browser complete stream finished.", "Browser reload stream finished."):
                await expect(page.get_by_text(message, exact=False)).to_have_count(1)
            assert calls.count("start") == 2, "Completed history reload must not replay a task"
            phase["mode"] = "cancel"
            generating.clear()
            release_generation.clear()
            editor = page.locator("textarea").first
            await editor.fill("Run the cancellable browser acceptance task")
            await editor.press("Enter")
            await asyncio.wait_for(generating.wait(), 30)
            await expect(
                page.get_by_text("Browser cancel stream started.", exact=False)
            ).to_be_visible()
            await page.get_by_role("button", name=re.compile(r"^(Stop|停止)$")).click()
            async with asyncio.timeout(20):
                while runtime.work:
                    await asyncio.sleep(0.02)
            assert runtime.tasks[thread_id]["status"] == "cancelled"
            assert calls.count("start") == 3, "Reload must never start or replay a task"
            assert target.read_text() == "browser report 42"
            if os.name == "posix":
                await expect(stop).to_have_count(0, timeout=20000)
                phase["mode"] = "approval"
                disposable = target.parent / "disposable.txt"
                disposable.write_text("owned approval fixture", encoding="utf-8")
                await editor.fill("Remove the disposable approval fixture")
                await editor.press("Enter")
                approve = page.get_by_role("button", name=re.compile(r"^(Approve|批准)$"))
                await expect(approve).to_be_visible(timeout=30000)
                pending_id = runtime.steering.approvals.snapshot(thread_id)["pending_id"]
                assert disposable.exists()
                await page.reload()
                await expect(approve).to_be_visible(timeout=30000)
                assert runtime.steering.approvals.snapshot(thread_id)["pending_id"] == pending_id
                assert calls.count("start") == 4, "Approval reload must not restart execution"
                await page.screenshot(path=str(evidence / "restored-approval.png"))
                await approve.click()
                await expect(
                    page.get_by_text("Approval complete.", exact=False).first
                ).to_be_visible(timeout=30000)
                async with asyncio.timeout(20):
                    while runtime.work:
                        await asyncio.sleep(0.02)
                assert not disposable.exists(), "Approved real tool must execute"
            assert not page_errors, page_errors
            await page.screenshot(path=str(evidence / "cancelled.png"))
            (evidence / "receipt.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "scope": "built React dashboard + Playwright bridge port + real Python harness/company APIs; deterministic model",
                        "native_go_webview_shell": "not exercised; its source is unchanged",
                        "starts": calls.count("start"),
                        "cancelled": True,
                        "unique_turn_history_reload": True,
                        "incremental_model_transport": "live loopback HTTP/SSE",
                        "approval_reload": "passed"
                        if os.name == "posix"
                        else "POSIX command fixture not applicable",
                        "real_file_content": target.read_text(),
                        "page_errors": page_errors,
                    },
                    indent=2,
                )
                + "\n"
            )
        finally:
            await page.screenshot(path=str(evidence / "final-state.png"))
            release_generation.set()
            release_sync.set()
            await runtime.close()
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            await context.tracing.stop(path=str(evidence / "trace.zip"))
            await browser.close()
