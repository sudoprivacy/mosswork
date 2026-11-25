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
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright, expect

from octop.infra.company_runtime import runtime as runtime_module
from octop.infra.company_runtime.client import CompanyClient
from tests.support.app import mosswork_client, write_mosswork_config
from tests.support.auth import auth_header, bootstrap_admin
from tests.support.http import serve_http
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
async def company_origin(env_with_agent):
    """Use real HTTP so SSE deltas arrive before the model response completes."""
    client, _, _, _ = env_with_agent
    async with serve_http(client._mosswork_app) as origin:
        yield origin


async def test_built_desktop_chat_reconnect_and_cancel(
    env_with_agent, company_origin, tmp_path, monkeypatch
):
    client, server, auth, agent_id = env_with_agent
    assets = Path(__file__).resolve().parents[2] / "src/octop/dashboard"
    assert (assets / "index.html").is_file(), "Build the dashboard before this acceptance"
    started = await client.post(f"/api/agents/{agent_id}/start", headers=auth)
    assert started.status_code == 204, started.text
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
    created = await client.post(
        "/api/company-runtime/threads",
        headers={"X-Mosswork-Device-Credential": credential},
        json={"agent_id": agent_id},
    )
    assert created.is_success, created.text
    existing_thread_id = created.json()["thread_id"]
    company = CompanyClient(company_origin, credential)
    events = []
    runtime = native_runtime(company, tmp_path, events)
    initial_metadata_seen, release_initial_metadata = asyncio.Event(), asyncio.Event()
    phase = {"mode": "complete"}
    generating, release_generation, syncing, release_sync = (asyncio.Event() for _ in range(4))
    reading_deltas = asyncio.Queue()

    class Model:
        def bind_tools(self, _tools):
            return self

        async def astream(self, messages):
            mode = phase["mode"]
            if mode.startswith("history-"):
                yield AIMessageChunk(content=f"Archived reading fixture {mode}.", id=mode)
                return
            if mode == "reading":
                yield AIMessageChunk(
                    content="\n\n".join(
                        f"Reading row {index:03}: Keep this paragraph readable while the report grows."
                        for index in range(120)
                    ),
                    id="reading-answer",
                )
                while (delta := await reading_deltas.get()) is not None:
                    yield AIMessageChunk(content="\n\n" + delta, id="reading-answer")
                return
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
        browser = await playwright.chromium.launch(ignore_default_args=["--hide-scrollbars"])
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
                if request.method == "GET" and url.path in {
                    f"/api/agents/{agent_id}/threads",
                    f"/api/agents/{agent_id}/threads/{existing_thread_id}/history",
                }:
                    initial_metadata_seen.set()
                    await release_initial_metadata.wait()
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
            if method == "create_thread":
                return await company.request("threads", {**params, "execution_mode": "local"})
            if method == "start":
                return await runtime.start(params)
            if method == "cancel":
                return await runtime.cancel(params)
            if method == "hitl_resume":
                return runtime.steering.approvals.resolve(params)
            raise AssertionError(method)

        await page.expose_function("acceptanceRuntimeCall", bridge_call)
        await page.add_init_script("""(() => {
          const bindings = new WeakMap();
          window.acceptancePointerEvents = [];
          for (const type of ['pointerdown', 'pointerup', 'pointercancel']) {
            window.addEventListener(type, e => {
              const el = document.querySelector('[data-virtuoso-scroller]');
              window.acceptancePointerEvents.push({type, x:e.clientX, y:e.clientY,
                target:e.target.tagName, scrollerTarget:e.target === el,
                top:el?.scrollTop, gap:el && el.scrollHeight - el.scrollTop - el.clientHeight});
              if (window.acceptancePointerEvents.length > 30) window.acceptancePointerEvents.shift();
            }, true);
          }
          const addListener = EventTarget.prototype.addEventListener;
          EventTarget.prototype.addEventListener = function(type, ...args) {
            if (type === 'pointerdown') bindings.set(this, (bindings.get(this) || 0) + 1);
            return addListener.call(this, type, ...args);
          };
          window.acceptanceScrollBindings = bindings;
          const listeners = new Set();
          window.acceptanceEmit = event => { for (const fn of listeners) fn(event); };
          window.__mossworkRuntime = {
            version: 1,
            status: () => window.acceptanceRuntimeCall('status'),
            createThread: params => window.acceptanceRuntimeCall('create_thread', params),
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
            await page.goto(f"http://testserver/chat/{agent_id}/{existing_thread_id}")
            editor = page.locator("textarea").first
            await expect(editor).to_be_visible(timeout=30000)
            await asyncio.wait_for(initial_metadata_seen.wait(), 30)
            await expect(editor).to_be_disabled()
            release_initial_metadata.set()
            await expect(editor).to_be_enabled()
            new_task = page.get_by_role("button", name=re.compile(r"^(New Chat|新建对话)$"))
            await new_task.and_(page.locator('[aria-haspopup="menu"]')).last.press("Enter")
            await page.get_by_role(
                "menuitem", name=re.compile(r"^(New local task|新建本机任务)$")
            ).click()
            await expect(page).to_have_url(f"http://testserver/chat/{agent_id}")
            await editor.fill("Create the browser acceptance report")
            await editor.press("Enter")
            await expect(
                page.get_by_text("Browser complete stream finished.", exact=False).first
            ).to_be_visible(timeout=45000)
            thread_id = urlsplit(page.url).path.rsplit("/", 1)[-1]
            assert thread_id != existing_thread_id
            assert calls.count("create_thread") == 1
            async with asyncio.timeout(20):
                while runtime.work:
                    await asyncio.sleep(0.02)
            target = runtime.workspace / thread_id / "report.txt"
            assert target.read_text() == "browser report 42"
            first_task = runtime.tasks[thread_id]["task_id"]
            phase["mode"] = "reload"
            await expect(editor).to_be_enabled()
            await editor.fill("Continue the browser acceptance report")
            await editor.press("Enter")
            await asyncio.wait_for(generating.wait(), 30)
            await page.reload()
            await expect(page.locator("textarea").first).to_be_visible(timeout=30000)
            stop = page.get_by_role("button", name=re.compile(r"^(Stop|停止)$"))
            await expect(stop).to_be_visible(timeout=30000)
            await expect(
                page.get_by_text("Continue the browser acceptance report", exact=True)
            ).to_have_count(1)
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
            await page.reload()
            await expect(page.locator("textarea").first).to_be_visible(timeout=30000)
            await expect(
                page.get_by_text("Run the cancellable browser acceptance task", exact=True)
            ).to_have_count(1)
            await expect(
                page.get_by_text("Browser cancel stream started.", exact=False)
            ).to_have_count(1)
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
                await expect(
                    page.get_by_text("Remove the disposable approval fixture", exact=True)
                ).to_have_count(1)
                await expect(
                    page.get_by_text("Browser cancel stream started.", exact=False)
                ).to_have_count(1)
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

            history_turns = 16
            starts_before_history = calls.count("start")
            for index in range(history_turns):
                await expect(stop).to_have_count(0, timeout=20000)
                phase["mode"] = f"history-{index}"
                await editor.fill(f"Archive reading history {index}")
                await editor.press("Enter")
                await expect(
                    page.get_by_text(f"Archived reading fixture history-{index}.", exact=True)
                ).to_be_visible(timeout=30000)
                async with asyncio.timeout(20):
                    while runtime.work:
                        await asyncio.sleep(0.02)
            await expect(stop).to_have_count(0, timeout=20000)
            assert calls.count("start") == starts_before_history + history_turns

            phase["mode"] = "reading"
            starts_before_reading = calls.count("start")
            await editor.fill("Review the long streaming report")
            await editor.press("Enter")
            row = page.get_by_text(
                "Reading row 119: Keep this paragraph readable while the report grows.", exact=True
            )
            await expect(row).to_have_count(1, timeout=30000)
            scroller = (
                await row.evaluate_handle("""el => {
                  for (let node = el.parentElement; node; node = node.parentElement) {
                    if (getComputedStyle(node).overflowY === 'auto' &&
                        node.scrollHeight > node.clientHeight * 2) return node;
                  }
                  throw new Error('Long report has no scrollable reading surface');
                }""")
            ).as_element()
            assert scroller is not None
            assert await scroller.get_attribute("data-virtuoso-scroller") == "true"
            await page.wait_for_function(
                "el => el.scrollHeight - el.scrollTop - el.clientHeight <= 12", arg=scroller
            )
            jump = page.get_by_role("button", name=re.compile(r"^(New replies|有新回复)$"))
            reading_positions = {}

            def record_positions(label, before, after):
                reading_positions[label] = {"before": before, "after": after}
                (evidence / "reading-positions.json").write_text(
                    json.dumps(reading_positions, indent=2) + "\n"
                )

            async def position():
                return await scroller.evaluate("""async el => {
                  let previous = el.scrollTop, stable = 0;
                  for (let frame = 0; frame < 120 && stable < 4; frame++) {
                    await new Promise(requestAnimationFrame);
                    const current = el.scrollTop;
                    stable = Math.abs(current - previous) <= 1 ? stable + 1 : 0;
                    previous = current;
                  }
                  return {top: el.scrollTop, height: el.clientHeight, total: el.scrollHeight,
                    gap: el.scrollHeight - el.scrollTop - el.clientHeight,
                    gutter: el.offsetWidth - el.clientWidth,
                    pointer_bindings: window.acceptanceScrollBindings.get(el) || 0};
                }""")

            initial_geometry = await position()
            assert initial_geometry["pointer_bindings"] > 0

            async def append_while_reading(label):
                before = await position()
                assert before["gap"] > 200, "Fixture must actually be reading earlier text"
                marker = f"Reading update after {label}."
                await reading_deltas.put(marker)
                await expect(page.get_by_text(marker, exact=True)).to_have_count(1)
                after = await position()
                record_positions(label, before, after)
                assert abs(after["top"] - before["top"]) <= 2, (
                    f"New output moved the user's reading position after {label}: {before} -> {after}"
                )
                await expect(jump).to_be_visible()

            await scroller.hover()
            await page.mouse.wheel(0, -600)
            await expect(jump).to_be_visible()
            await append_while_reading("wheel")
            await page.set_viewport_size({"width": 1440, "height": 760})
            await append_while_reading("resize")
            await page.screenshot(path=str(evidence / "reading-free.png"))

            await jump.click()
            await expect(
                page.get_by_text("Reading update after resize.", exact=True)
            ).to_be_in_viewport()
            await expect(jump).not_to_be_visible()
            await reading_deltas.put("Reading ready for thumb interaction.")
            await expect(
                page.get_by_text("Reading ready for thumb interaction.", exact=True)
            ).to_be_in_viewport()

            async def press_thumb():
                await page.wait_for_function(
                    "el => el.scrollHeight - el.scrollTop - el.clientHeight <= 12", arg=scroller
                )
                box = await scroller.bounding_box()
                geometry = await position()
                assert box and geometry["gutter"] > 0, "Desktop fixture must expose its scrollbar"
                scrollbar_x = box["x"] + box["width"] - geometry["gutter"] / 2
                thumb_height = max(18, geometry["height"] ** 2 / geometry["total"])
                await page.mouse.move(scrollbar_x, box["y"] + box["height"] - thumb_height / 2)
                await page.mouse.down()
                (evidence / "thumb-pointer-events.json").write_text(
                    json.dumps(await page.evaluate("window.acceptancePointerEvents"), indent=2)
                    + "\n"
                )
                record_positions("pressed", geometry, await position())
                return box, geometry, scrollbar_x, thumb_height

            # A click without movement must resume follow after its React update.
            await press_thumb()
            await expect(jump).to_be_visible()
            stationary_before = await position()
            await page.mouse.up()
            record_positions("stationary", stationary_before, await position())
            await expect(jump).not_to_be_visible()
            await reading_deltas.put("Reading after stationary thumb release.")
            await expect(
                page.get_by_text("Reading after stationary thumb release.", exact=True)
            ).to_be_in_viewport()

            # Drag the actual CSS scrollbar to an earlier paragraph in this item.
            box, geometry, scrollbar_x, thumb_height = await press_thumb()
            await reading_deltas.put("Reading update while holding the scrollbar.")
            await expect(
                page.get_by_text("Reading update while holding the scrollbar.", exact=True)
            ).to_have_count(1)
            geometry = await position()
            target_top = await page.get_by_text(
                "Reading row 020: Keep this paragraph readable while the report grows.", exact=True
            ).evaluate(
                "(el, scroller) => scroller.scrollTop + el.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 80",
                scroller,
            )
            fraction = max(0, min(1, target_top / (geometry["total"] - geometry["height"])))
            await page.mouse.move(
                scrollbar_x,
                box["y"] + thumb_height / 2 + fraction * (geometry["height"] - thumb_height),
                steps=4,
            )
            await page.mouse.up()
            await page.wait_for_function(
                "el => el.scrollHeight - el.scrollTop - el.clientHeight > 200", arg=scroller
            )
            await append_while_reading("scrollbar")
            await jump.click()
            await reading_deltas.put("Reading follow resumed.")
            await expect(page.get_by_text("Reading follow resumed.", exact=True)).to_be_visible()
            await page.wait_for_function(
                "el => el.scrollHeight - el.scrollTop - el.clientHeight <= 12", arg=scroller
            )
            await reading_deltas.put(None)
            async with asyncio.timeout(20):
                while runtime.work:
                    await asyncio.sleep(0.02)
            assert calls.count("start") == starts_before_reading + 1
            final_geometry = await position()
            assert final_geometry["pointer_bindings"] == initial_geometry["pointer_bindings"], (
                "Streaming must not rebind the same scroller's pointer listener: "
                f"{initial_geometry} -> {final_geometry}"
            )
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
                        "stream_reading": {
                            "wheel": True,
                            "resize": True,
                            "scrollbar": True,
                            "stationary_thumb_resumed": True,
                            "virtual_history_turns": history_turns,
                            "scroller_pointer_bindings": {
                                "before": initial_geometry["pointer_bindings"],
                                "after": final_geometry["pointer_bindings"],
                            },
                            "explicit_follow_resumed": True,
                            "positions": reading_positions,
                        },
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
            release_initial_metadata.set()
            await page.screenshot(path=str(evidence / "final-state.png"))
            release_generation.set()
            release_sync.set()
            await runtime.close()
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            await context.tracing.stop(path=str(evidence / "trace.zip"))
            await browser.close()
