"""Run the compiled Mosswork viewer against real local HTTP/WS and Chrome.

The application, authentication, browser and UI are real. The existing test
harness replaces the model only. No provider key, tenant grant or remote
service is used; this is not production or physical-phone acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import sys
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


def dashboard_files(directory: Path) -> dict[str, dict[str, Any]]:
    files = {}
    for path in sorted(directory.rglob("*")):
        assert not path.is_symlink(), "Require regular compiled dashboard files"
        if path.is_file():
            data = path.read_bytes()
            files[path.relative_to(directory).as_posix()] = {
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
    assert "index.html" in files and any(name.endswith(".js") for name in files)
    return files


async def accept(
    chrome: Path,
    output: Path,
    review_fixture: Path | None = None,
    review_manifest_sha256: str | None = None,
    review_intake: Path | None = None,
) -> None:
    user_home = output / "user-home"
    user_home.mkdir()
    home = user_home / ".mosswork"
    os.environ.update(HOME=str(user_home), USERPROFILE=str(user_home), MOSSWORK_HOME=str(home))
    repository = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(repository / "src"), str(repository)]
    inputs = [
        "src/octop/api/routers/browser/harness.py",
        "src/octop/api/routers/browser/stream.py",
        "src/octop/infra/browser/control.py",
        "src/octop/infra/agents/middleware/browser_profile.py",
        "src/octop/infra/agents/execution_backend.py",
        "src/octop/infra/agents/manager.py",
        "src/octop/infra/execution/gateway.py",
        "src/octop/infra/server.py",
        "src/octop/api/routers/chat/turn.py",
        "src/octop/api/routers/chat/history.py",
        "src/octop/infra/gateway/gateway.py",
        "src/octop/infra/gateway/process/processor.py",
        "dashboard/src/hooks/useBrowserStream.ts",
        "src/octop/dashboard/index.html",
        "tools/accept_browser_viewer.py",
        "tools/accept_browser_review.py",
        "tools/accept_browser_fixture.py",
        "tools/accept_browser_fixture.json",
        "uv.lock",
    ]
    source_hashes = {
        name: hashlib.sha256((repository / name).read_bytes()).hexdigest() for name in inputs
    }
    dashboard_directory = repository / "src/octop/dashboard"
    compiled_files = dashboard_files(dashboard_directory)

    from http.server import ThreadingHTTPServer  # noqa: PLC0415

    import httpx  # noqa: PLC0415
    import uvicorn  # noqa: PLC0415
    from accept_browser_control import Fixture  # noqa: PLC0415
    from moss_browser import BrowserSession, MossBrowserSettings  # noqa: PLC0415
    from moss_browser.profile import ProfileManager  # noqa: PLC0415
    from moss_browser.tool_interface import _registry  # noqa: PLC0415
    from playwright.async_api import async_playwright, expect  # noqa: PLC0415
    from tests.support.app import mosswork_client, write_mosswork_config  # noqa: PLC0415
    from tests.support.auth import (  # noqa: PLC0415
        auth_header,
        bootstrap_admin,
        create_agent,
        seed_openai_provider,
    )
    from tests.support.http import chat_ws_path  # noqa: PLC0415
    from websockets.asyncio.client import connect  # noqa: PLC0415

    from octop.infra.browser.control import profile_control  # noqa: PLC0415

    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    transport: list[dict[str, Any]] = []
    proof = None

    def record(name: str, **details: Any) -> None:
        checks.append({"name": name, "passed": True, **details})
        print(json.dumps(checks[-1], ensure_ascii=False), flush=True)

    async def draft_point(canvas: Any, target: Any) -> dict[str, float]:
        box = await target.bounding_box()
        assert box is not None
        return await canvas.evaluate(
            """(canvas, point) => {
                const r=canvas.getBoundingClientRect();
                const s=Math.min(r.width/canvas.width,r.height/canvas.height);
                return {x:r.x+(r.width-canvas.width*s)/2+point.x*s,
                        y:r.y+(r.height-canvas.height*s)/2+point.y*s};
            }""",
            {"x": box["x"] + 20, "y": box["y"] + 10},
        )

    if review_fixture is not None:
        from accept_browser_review import ReviewFixture  # noqa: PLC0415

        assert review_manifest_sha256 is not None, "Require the pinned review manifest SHA-256"
        fixture = ReviewFixture(review_fixture, review_manifest_sha256, review_intake)
        if fixture.publication is not None:
            (output / "published-fixture-intake.json").write_text(
                json.dumps(fixture.publication, indent=2) + "\n", encoding="utf-8"
            )
    else:
        assert review_intake is None, "Published intake requires its review fixture"
        fixture = Fixture()
    fixture_server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.handler())
    threading.Thread(target=fixture_server.serve_forever, daemon=True).start()
    target_url = f"http://127.0.0.1:{fixture_server.server_port}/"
    try:
        write_mosswork_config(home, enable_api_docs=True)
        async with mosswork_client(home) as (client, server):
            await bootstrap_admin(client, home)
            auth = await auth_header(client)
            await seed_openai_provider(client, auth)
            agent = await create_agent(client, auth)
            spec_response = await client.get("/api/openapi.json", headers=auth)
            spec_response.raise_for_status()
            spec = spec_response.json()
            for path, method in (
                ("/api/browser/harness-sessions", "get"),
                ("/api/browser/sessions/{session_id}/handoff", "post"),
                ("/api/browser/shutdown", "post"),
            ):
                operation = spec["paths"][path][method]
                assert operation["summary"]
                assert (
                    "$ref" in operation["responses"]["200"]["content"]["application/json"]["schema"]
                )
            response = await client.post(f"/api/agents/{agent}/threads", headers=auth)
            response.raise_for_status()
            thread = response.json()["thread_id"]
            controls = await profile_control("user-1", home)
            app = client._mosswork_app  # type: ignore[attr-defined]
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            web = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
            web_task = asyncio.create_task(web.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(10):
                    while not web.started:
                        if web_task.done():
                            await web_task
                            raise RuntimeError("Local UI server exited before readiness")
                        await asyncio.sleep(0.01)
                origin = f"http://127.0.0.1:{port}"
                async with async_playwright() as playwright:
                    execution = await playwright.chromium.launch_persistent_context(
                        str(output / "execution-profile"),
                        executable_path=str(chrome),
                        headless=True,
                        viewport={"width": 1280, "height": 800},
                        args=["--remote-debugging-port=0"],
                    )
                    viewer = None
                    session = None
                    try:
                        execution_page = execution.pages[0]
                        await execution_page.goto(target_url)
                        cdp_port = int(
                            (output / "execution-profile/DevToolsActivePort")
                            .read_text()
                            .splitlines()[0]
                        )
                        async with httpx.AsyncClient(trust_env=False) as probe:
                            targets = (
                                await probe.get(f"http://127.0.0.1:{cdp_port}/json/list")
                            ).json()
                        target = next(item for item in targets if item.get("type") == "page")
                        assert target["url"] == target_url
                        settings = MossBrowserSettings(
                            cdp_host="127.0.0.1",
                            cdp_port_start=cdp_port,
                            viewport_width=1280,
                            viewport_height=800,
                        )
                        session = await BrowserSession.create(
                            profile="user-1",
                            settings=settings,
                            profile_manager=ProfileManager(
                                output / "moss-profiles", settings=settings
                            ),
                        )
                        _registry["user-1"] = session
                        stopped = await client.post(
                            f"/api/agents/{agent}/threads/{thread}/cancel", headers=auth
                        )
                        stopped.raise_for_status()
                        assert controls.owner == "user"

                        viewer = await playwright.chromium.launch(
                            executable_path=str(chrome), headless=True
                        )
                        context = await viewer.new_context(viewport={"width": 1440, "height": 1000})
                        token = auth["Authorization"].removeprefix("Bearer ")
                        await context.add_init_script(
                            """(token => {
                            localStorage.setItem('auth_token', token);
                            localStorage.setItem('mosswork:remote-browser:stream-active', '1');
                            localStorage.setItem('mosswork:remote-browser:harness-profile:1', 'user-1');
                            localStorage.setItem('mosswork:remote-browser:viewport', '1280x800');
                        })("""
                            + json.dumps(token)
                            + ");"
                        )
                        page = await context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        static_responses: dict[str, str] = {}
                        static_tasks: list[asyncio.Task[None]] = []

                        async def check_static_response(response: Any, name: str) -> None:
                            digest = hashlib.sha256(await response.body()).hexdigest()
                            assert digest == compiled_files[name]["sha256"], (
                                "Served dashboard bytes differ from the pinned artifact",
                                name,
                            )
                            static_responses[name] = digest

                        def observe_static_response(response: Any) -> None:
                            address = urlsplit(response.url)
                            if (
                                address.scheme + "://" + address.netloc != origin
                                or response.status != 200
                            ):
                                return
                            name = unquote(address.path).lstrip("/")
                            if name not in compiled_files:
                                if response.request.resource_type != "document":
                                    return
                                name = "index.html"
                            static_tasks.append(
                                asyncio.create_task(check_static_response(response, name))
                            )

                        page.on("response", observe_static_response)
                        frame_count = 0
                        current_socket = None

                        def watch_socket(websocket: Any) -> None:
                            nonlocal current_socket
                            if "/browser-stream/ws" not in websocket.url:
                                return
                            current_socket = websocket

                            def received(raw: Any) -> None:
                                nonlocal frame_count
                                message = json.loads(raw)
                                if message.get("type") != "frame":
                                    transport.append(
                                        {
                                            key: message[key]
                                            for key in (
                                                "type",
                                                "controlEpoch",
                                                "targetEpoch",
                                                "control_owner",
                                                "message",
                                                "recoverable",
                                            )
                                            if key in message
                                        }
                                    )
                                if current_socket is websocket and message.get("type") == "frame":
                                    frame_count += 1

                            websocket.on("framereceived", received)
                            websocket.on(
                                "framesent",
                                lambda raw: transport.append(
                                    {
                                        "direction": "sent",
                                        **{
                                            key: value
                                            for key, value in json.loads(raw).items()
                                            if key in ("type", "controlEpoch", "targetEpoch")
                                        },
                                    }
                                ),
                            )

                        page.on("websocket", watch_socket)

                        async def fresh_frames(view: Any = page) -> None:
                            previous = frame_count
                            async with asyncio.timeout(10):
                                while frame_count < previous + 2:
                                    await asyncio.sleep(0.01)
                            await view.evaluate("() => new Promise(requestAnimationFrame)")

                        await page.goto(origin + "/remote-browser", wait_until="domcontentloaded")

                        async def ready(view: Any, *, fixed: bool = True) -> Any:
                            await view.wait_for_function(
                                """fixed => [...document.querySelectorAll('canvas')].some(c =>
                                (fixed ? c.width === 1280 && c.height === 800 : c.width > 0 && c.height > 0)
                                && c.getBoundingClientRect().width > 0)""",
                                arg=fixed,
                                timeout=15000,
                            )
                            selector = (
                                'canvas[width="1280"][height="800"]' if fixed else "canvas:visible"
                            )
                            canvas = view.locator(selector).first
                            await expect(canvas.locator("..").locator("textarea")).to_be_enabled()
                            return canvas

                        canvas = await ready(page)
                        await page.wait_for_function(
                            "url => [...document.querySelectorAll('input')].some(el => el.value === url)",
                            arg=target_url,
                            timeout=10000,
                        )
                        await expect(
                            page.get_by_role("tab", name="Browser control acceptance")
                        ).to_have_count(1)
                        assert await page.get_by_text("交还控制权给专家", exact=True).count() == 0
                        assert (
                            await page.get_by_text("Return control to Agent", exact=True).count()
                            == 0
                        )
                        record(
                            "compiled_viewer_displays_same_execution_browser_without_handoff_buttons",
                            typedApiSchemas=True,
                        )

                        point = await draft_point(canvas, execution_page.locator("#draft"))
                        await page.mouse.click(point["x"], point["y"])
                        draft = "WebUI 中文草稿\nsecond line"
                        await page.keyboard.insert_text(draft)
                        await execution_page.wait_for_function(
                            "draft => document.querySelector('#draft').value === draft",
                            arg=draft,
                            timeout=10000,
                        )
                        assert not fixture.submissions
                        await fresh_frames()
                        await page.screenshot(
                            path=str(output / "desktop-viewer.png"), full_page=True
                        )
                        record(
                            "real_canvas_pointer_and_unicode_input_edit_same_page_without_submission"
                        )

                        await page.reload(wait_until="domcontentloaded")
                        await ready(page)
                        assert await execution_page.locator("#draft").input_value() == draft
                        before_attach = controls.core.target_epoch
                        second = await context.new_page()
                        await second.goto(origin + "/remote-browser", wait_until="domcontentloaded")
                        await ready(second)
                        assert controls.core.target_epoch == before_attach, (
                            "Attaching a same-size viewer must not retarget the execution browser"
                        )
                        assert await execution_page.locator("#draft").input_value() == draft
                        record("reload_and_second_viewer_keep_profile_page_and_unsubmitted_draft")

                        await page.get_by_role("button", name="新标签页", exact=True).click()
                        await expect(page.get_by_role("tab")).to_have_count(2, timeout=10000)
                        await expect(second.get_by_role("tab")).to_have_count(2, timeout=10000)
                        await fresh_frames()
                        await page.get_by_role("tab", name="Browser control acceptance").click()
                        await page.wait_for_function(
                            "url => [...document.querySelectorAll('input')].some(el => el.value === url)",
                            arg=target_url,
                            timeout=10000,
                        )
                        await fresh_frames()
                        assert await execution_page.locator("#draft").input_value() == draft
                        record("native_cdp_tabs_create_and_switch_back_to_same_unsubmitted_draft")

                        async def native_message(text: str) -> None:
                            async with connect(
                                f"ws://127.0.0.1:{port}" + chat_ws_path(agent, auth), proxy=None
                            ) as websocket:
                                await websocket.send(
                                    json.dumps(
                                        {"type": "user_turn", "text": text, "thread_id": thread}
                                    )
                                )
                                async with asyncio.timeout(15):
                                    while True:
                                        message = json.loads(await websocket.recv())
                                        assert message.get("type") != "error", message
                                        if message.get("type") == "done":
                                            return

                        await native_message("Continue")
                        assert controls.owner == "agent"
                        for view in (page, second):
                            remote_input = (
                                view.locator('canvas[width="1280"][height="800"]')
                                .first.locator("..")
                                .locator("textarea")
                            )
                            await expect(remote_input).to_be_disabled(timeout=10000)
                        await native_message("/stop")
                        assert controls.owner == "user"
                        for view in (page, second):
                            remote_input = (
                                view.locator('canvas[width="1280"][height="800"]')
                                .first.locator("..")
                                .locator("textarea")
                            )
                            await expect(remote_input).to_be_enabled(timeout=10000)
                        assert await execution_page.locator("#draft").input_value() == draft
                        assert not fixture.submissions and not errors, errors
                        assert not [event for event in transport if event.get("type") == "error"], (
                            transport
                        )
                        record(
                            "native_conversation_continue_and_stop_update_both_viewers_without_replaying_input"
                        )

                        panel = await context.new_page()
                        panel.on("pageerror", lambda error: errors.append(str(error)))
                        panel.on("websocket", watch_socket)
                        await panel.goto(
                            origin + f"/chat/{agent}/{thread}", wait_until="domcontentloaded"
                        )
                        await panel.get_by_role("button", name="查看浏览器", exact=True).click()
                        panel_canvas = await ready(panel)
                        await panel.wait_for_function(
                            "url => [...document.querySelectorAll('input')].some(el => el.value === url)",
                            arg=target_url,
                            timeout=10000,
                        )
                        assert await panel_canvas.is_visible()
                        assert await execution_page.locator("#draft").input_value() == draft
                        assert await panel.get_by_text("交还控制权给专家", exact=True).count() == 0
                        point = await draft_point(panel_canvas, execution_page.locator("#draft"))
                        await panel.mouse.click(point["x"], point["y"])
                        await panel.keyboard.press("Control+End")
                        await panel.keyboard.insert_text(" panel中文")
                        await execution_page.wait_for_function(
                            "draft => document.querySelector('#draft').value === draft",
                            arg=draft + " panel中文",
                            timeout=10000,
                        )
                        draft += " panel中文"
                        assert not fixture.submissions
                        await fresh_frames(panel)
                        await panel.screenshot(
                            path=str(output / "chat-browser-panel.png"), full_page=True
                        )
                        record("native_chat_browser_panel_edits_same_unsubmitted_draft")
                        await panel.close()
                        await page.close()
                        await second.close()

                        mobile_context = await viewer.new_context(
                            viewport={"width": 390, "height": 844},
                            is_mobile=True,
                            has_touch=True,
                        )
                        await mobile_context.add_init_script(
                            """(token => {
                            localStorage.setItem('auth_token', token);
                            localStorage.setItem('mosswork:remote-browser:stream-active', '1');
                            localStorage.setItem('mosswork:remote-browser:harness-profile:1', 'user-1');
                            localStorage.setItem('mosswork:remote-browser:viewport', '1280x800');
                        })("""
                            + json.dumps(token)
                            + ");"
                        )
                        mobile = await mobile_context.new_page()
                        mobile.on("pageerror", lambda error: errors.append(str(error)))
                        mobile.on("websocket", watch_socket)
                        await mobile.goto(origin + "/remote-browser", wait_until="domcontentloaded")
                        mobile_canvas = await ready(mobile, fixed=False)
                        await fresh_frames(mobile)
                        await ready(mobile, fixed=False)
                        assert await mobile.evaluate(
                            "document.documentElement.scrollWidth <= window.innerWidth"
                        ), "Mobile viewer overflows the device viewport"
                        point = await draft_point(mobile_canvas, execution_page.locator("#draft"))
                        await mobile.touchscreen.tap(point["x"], point["y"])
                        await mobile.keyboard.insert_text(" mobile中文")
                        await execution_page.wait_for_function(
                            "draft => document.querySelector('#draft').value.includes(draft)",
                            arg=" mobile中文",
                            timeout=10000,
                        )
                        mobile_draft = await execution_page.locator("#draft").input_value()
                        assert mobile_draft.replace(" mobile中文", "") == draft
                        assert not fixture.submissions and not errors, errors
                        assert not [event for event in transport if event.get("type") == "error"], (
                            transport
                        )
                        await fresh_frames(mobile)
                        await mobile.screenshot(
                            path=str(output / "mobile-viewer.png"), full_page=True
                        )
                        record(
                            "mobile_emulated_viewer_touch_and_unicode_preserve_unsubmitted_draft",
                            physicalPhone=False,
                            engine="Chromium",
                            viewport="390x844",
                        )
                        await mobile_context.close()
                        if review_fixture is not None:
                            from accept_browser_review import qualify_review  # noqa: PLC0415

                            graph_view = await context.new_page()
                            graph_view.on("pageerror", lambda error: errors.append(str(error)))
                            graph_view.on("websocket", watch_socket)
                            await graph_view.goto(
                                origin + "/remote-browser", wait_until="domcontentloaded"
                            )
                            graph_canvas = await ready(graph_view)
                            details = await qualify_review(
                                fixture,
                                execution_page,
                                graph_view,
                                graph_canvas,
                                target_url + "graph",
                                fresh_frames,
                                output,
                            )
                            assert not errors, errors
                            assert not [
                                event for event in transport if event.get("type") == "error"
                            ], transport
                            record(
                                "compiler_graph_selection_expansion_language_and_zoom_through_compiled_viewer",
                                **details,
                            )
                            record(
                                "compiler_graph_record_page_matches_original_pointer_and_returns_to_field",
                                **details["recordPage"],
                            )
                            await graph_view.close()
                        logout_view = await context.new_page()
                        logout_view.on("pageerror", lambda error: errors.append(str(error)))
                        logout_view.on("websocket", watch_socket)
                        await logout_view.goto(
                            origin + "/remote-browser", wait_until="domcontentloaded"
                        )
                        await ready(logout_view)
                        await fresh_frames(logout_view)
                        assert current_socket is not None
                        logout_socket = current_socket
                        socket_closed = asyncio.Event()
                        logout_socket.on("close", lambda *_: socket_closed.set())
                        logout_source = await context.new_page()
                        await logout_source.goto(origin + "/favicon.ico")

                        async def remote_page_state() -> dict[str, Any]:
                            return await execution_page.evaluate(
                                """() => ({url: location.href,
                                draft: document.querySelector('#draft')?.value ?? null})"""
                            )

                        before_logout = await remote_page_state()
                        await logout_source.evaluate("localStorage.removeItem('auth_token')")
                        await asyncio.wait_for(socket_closed.wait(), timeout=10)
                        # An independent viewer is still mounted when another
                        # tab logs out. Its old authenticated socket must close;
                        # native browser lifetime/cleanup is a separate concern.
                        await logout_view.mouse.click(100, 200)
                        await logout_view.keyboard.insert_text("LOGOUT_MUST_NOT_REACH_BROWSER")
                        assert await remote_page_state() == before_logout
                        assert not fixture.submissions and not errors, errors
                        record(
                            "cross_tab_logout_closes_actual_viewer_socket_and_preserves_remote_page",
                            viewerSocketClosed=socket_closed.is_set(),
                            executionBrowserStillRunning=not execution_page.is_closed(),
                        )
                        await logout_source.close()
                        await logout_view.close()
                        assert source_hashes == {
                            name: hashlib.sha256((repository / name).read_bytes()).hexdigest()
                            for name in inputs
                        }, "Acceptance source changed during the live run"
                        await asyncio.gather(*static_tasks)
                        assert compiled_files == dashboard_files(dashboard_directory), (
                            "Compiled dashboard changed during the live run"
                        )
                        assert "index.html" in static_responses
                        assert any(name.endswith(".js") for name in static_responses)
                        assert any(name.endswith(".css") for name in static_responses)
                        dashboard_manifest = {
                            "files": compiled_files,
                            "servedFileSha256": static_responses,
                            "scope": "Exact compiled WebUI acceptance input; not a release build or source-to-build provenance attestation",
                        }
                        manifest_bytes = (json.dumps(dashboard_manifest, indent=2) + "\n").encode()
                        (output / "dashboard-content-manifest.json").write_bytes(manifest_bytes)
                        record(
                            "compiled_dashboard_artifact_matches_actual_http_responses",
                            resources=len(compiled_files),
                            servedResources=len(static_responses),
                        )
                        proof = {
                            "passed": True,
                            "checks": checks,
                            "pageErrors": errors,
                            "scope": __doc__,
                            "headful": False,
                            "physicalPhone": False,
                            "bundleIndexSha256": hashlib.sha256(
                                (repository / "src/octop/dashboard/index.html").read_bytes()
                            ).hexdigest(),
                            "scriptSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                            "runtimeInputSha256": source_hashes,
                            "dashboardContentManifestSha256": hashlib.sha256(
                                manifest_bytes
                            ).hexdigest(),
                            "publishedFixtureIntakeSha256": (
                                hashlib.sha256(
                                    (output / "published-fixture-intake.json").read_bytes()
                                ).hexdigest()
                                if review_intake is not None
                                else None
                            ),
                        }
                    except BaseException:
                        if viewer is not None:
                            for number, view in enumerate(
                                page for context in viewer.contexts for page in context.pages
                            ):
                                with suppress(Exception):
                                    await view.screenshot(
                                        path=str(output / f"failure-viewer-{number}.png"),
                                        timeout=3000,
                                    )
                        raise
                    finally:
                        _registry.pop("user-1", None)
                        if session is not None:
                            await session.close()
                        if viewer is not None:
                            await viewer.close()
                        await execution.close()
            finally:
                web.should_exit = True
                await web_task
                listener.close()
    except BaseException as error:
        (output / "incomplete.json").write_text(
            json.dumps(
                {
                    "passed": False,
                    "checks": checks,
                    "error": f"{type(error).__name__}: {error}",
                    "pageErrors": errors,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        raise
    finally:
        await asyncio.to_thread(fixture_server.shutdown)
        fixture_server.server_close()
        (output / "transport.json").write_text(json.dumps(transport, indent=2), encoding="utf-8")
    assert proof is not None
    (output / "receipt.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-fixture", type=Path)
    parser.add_argument("--review-manifest-sha256")
    parser.add_argument("--review-intake", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    asyncio.run(
        accept(
            args.chrome.resolve(strict=True),
            args.output,
            args.review_fixture,
            args.review_manifest_sha256,
            args.review_intake,
        )
    )
