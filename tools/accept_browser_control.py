"""Exercise the real browser tool, shared barrier and Chrome on an isolated page.

No model or production credential is used. This qualifies the browser tool and
input boundary; it does not qualify the product frontend or Nexus integration.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HTML = """<!doctype html><meta charset=utf-8><title>Browser control acceptance</title>
<h1>Shared browser draft</h1><textarea id=draft aria-label=Draft></textarea>
<button id=ai onclick="fetch('/ai',{method:'POST'})">AI operation</button>
<button id=submit onclick="fetch('/submit',{method:'POST',body:document.querySelector('#draft').value})">
Submit once</button>"""


class Fixture:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.authorization_started = threading.Event()
        self.authorization_release = threading.Event()
        self.ai_actions = 0
        self.submissions: list[str] = []

    def handler(self) -> type[BaseHTTPRequestHandler]:
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/started":
                    fixture.started.set()
                if self.path == "/authorization-held":
                    fixture.authorization_started.set()
                    if not fixture.authorization_release.wait(10):
                        self.send_error(504)
                        return
                data = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/ai":
                    fixture.ai_actions += 1
                elif self.path == "/submit":
                    fixture.submissions.append(data.decode("utf-8"))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args: Any) -> None:
                pass

        return Handler


async def accept(chrome: Path, output: Path, *, headful: bool) -> None:
    # Set the isolated application root before importing the real initializer.
    os.environ["MOSSWORK_HOME"] = str(output / "home")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import httpx  # noqa: PLC0415
    from ai_dev_browser.remote.control import ControlError  # noqa: PLC0415
    from langchain_core.runnables import RunnableLambda  # noqa: PLC0415
    from langgraph.prebuilt.tool_node import ToolCallRequest  # noqa: PLC0415
    from moss_browser import BrowserSession, MossBrowserSettings  # noqa: PLC0415
    from moss_browser.profile import ProfileManager  # noqa: PLC0415
    from moss_browser.tool_interface import _registry  # noqa: PLC0415
    from moss_harness.builtin.tools.browser_use import browser_use  # noqa: PLC0415
    from playwright.async_api import async_playwright  # noqa: PLC0415

    import octop  # noqa: F401, PLC0415
    from octop.api.routers.browser.stream import _handle_client_event  # noqa: PLC0415
    from octop.infra.agents.middleware.browser_profile import (
        BrowserProfileMiddleware,  # noqa: PLC0415
    )
    from octop.infra.browser.control import close_profile_controls, profile_control  # noqa: PLC0415

    checks: list[dict[str, Any]] = []

    def record(name: str, **data: Any) -> None:
        checks.append({"name": name, "passed": True, **data})
        (output / "checks.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
        print(json.dumps(checks[-1], ensure_ascii=False), flush=True)

    fixture = Fixture()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.handler())
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    profile = "user-42"
    app_root = output / "home"
    middleware = BrowserProfileMiddleware(app_root)
    session = None
    proof = None
    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(output / "chrome-profile"),
                executable_path=str(chrome),
                headless=not headful,
                args=["--remote-debugging-port=0"],
            )
            try:
                port = int(
                    (output / "chrome-profile/DevToolsActivePort").read_text().splitlines()[0]
                )
                async with httpx.AsyncClient(trust_env=False) as client:
                    targets = (await client.get(f"http://127.0.0.1:{port}/json/list")).json()
                target = next(target for target in targets if target.get("type") == "page")
                session = await BrowserSession.create(
                    profile=profile,
                    settings=MossBrowserSettings(cdp_ws_url=target["webSocketDebuggerUrl"]),
                    profile_manager=ProfileManager(output / "moss-profiles"),
                )
                _registry[profile] = session
                controls = await profile_control(profile, app_root)

                async def invoke(args: dict[str, Any]) -> Any:
                    request = ToolCallRequest(
                        tool_call={
                            "name": "browser_use",
                            "args": args,
                            "id": str(uuid.uuid4()),
                            "type": "tool_call",
                        },
                        tool=browser_use,
                        state={},
                        runtime=None,
                    )
                    return await middleware.awrap_tool_call(
                        request,
                        lambda bound: browser_use.ainvoke(bound.tool_call),
                    )

                tool = RunnableLambda(invoke)

                async def call(actor: str, **args: Any) -> Any:
                    return await tool.ainvoke(
                        args,
                        config={
                            "configurable": {
                                "user": "42",
                                "thread_id": actor,
                                "mosswork_browser_epoch": controls.core.epoch,
                            }
                        },
                    )

                navigated = await call("main", action="navigate", url=url, profile="default")
                assert navigated.status == "success", navigated.content
                page = context.pages[0]
                assert await page.title() == "Browser control acceptance"
                record("real_tool_uses_authenticated_profile_and_same_chrome")

                pending = asyncio.create_task(
                    call(
                        "main",
                        action="eval_js",
                        expression="fetch('/started');new Promise(resolve=>setTimeout(()=>{"
                        "document.querySelector('#ai').click();resolve('finished')},350))",
                    )
                )
                assert await asyncio.to_thread(fixture.started.wait, 10), (
                    "AI did not enter the real page"
                )
                takeover = asyncio.create_task(controls.take())
                for _ in range(100):
                    if controls.core.state == "pausing":
                        break
                    await asyncio.sleep(0.01)
                assert controls.core.state == "pausing"
                blocked = await call("child", action="click", selector="#ai")
                assert (
                    blocked.status == "error" and "browser_control_held_by_user" in blocked.content
                )
                pending.cancel()
                await takeover
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
                assert fixture.ai_actions == 1
                record("takeover_drains_sent_operation_and_blocks_other_agent_after_cancellation")

                async def input_event(**event: Any) -> None:
                    packet = {
                        "controlEpoch": controls.core.epoch,
                        "targetEpoch": controls.core.target_epoch,
                        **event,
                    }
                    await controls.input(
                        "viewer", packet, lambda: _handle_client_event(session, packet)
                    )

                box = await page.locator("#draft").bounding_box()
                assert box is not None
                x, y = int(box["x"] + 20), int(box["y"] + 10)
                stale = {
                    "type": "click",
                    "x": x,
                    "y": y,
                    "controlEpoch": controls.core.epoch - 1,
                    "targetEpoch": controls.core.target_epoch,
                }
                try:
                    await controls.input(
                        "viewer", stale, lambda: _handle_client_event(session, stale)
                    )
                    raise AssertionError("Stale input was accepted")
                except ControlError as error:
                    assert error.code == "stale_browser_operation"
                await input_event(type="click", x=x, y=y)
                await input_event(type="compositionstart")
                try:
                    await controls.give_back()
                    raise AssertionError("AI resumed before an IME composition committed")
                except ControlError as error:
                    assert error.code == "browser_input_in_progress"
                draft = "真实中文草稿\nsecond line"
                await input_event(type="type", text=draft)
                await input_event(type="compositionend")
                assert await page.locator("#draft").input_value() == draft
                assert not fixture.submissions
                record("stale_pixels_refused_and_unicode_draft_inserted_without_submission")

                await input_event(type="mousedown", x=x, y=y, button="left", buttons=1)
                try:
                    await controls.input(
                        "second-viewer",
                        {
                            "type": "click",
                            "x": x,
                            "y": y,
                            "controlEpoch": controls.core.epoch,
                            "targetEpoch": controls.core.target_epoch,
                        },
                        lambda: session.click(x=x, y=y),
                    )
                    raise AssertionError("A second viewer interleaved a held pointer")
                except ControlError as error:
                    assert error.code == "browser_input_in_progress"
                try:
                    await controls.give_back()
                    raise AssertionError("AI resumed while mouse input was held")
                except ControlError as error:
                    assert error.code == "browser_input_in_progress"
                await controls.detach("viewer", lambda event: _handle_client_event(session, event))
                assert controls.owner == "user" and not fixture.submissions
                returned = await controls.give_back()
                assert returned["observation"]["focused"]["value"] == draft
                record(
                    "disconnect_releases_input_without_returning_ai_then_explicit_resume_observes_draft"
                )

                for actor in ("main", "child"):
                    fresh = await call(actor, action="click", selector="#submit")
                    assert fresh.status == "error" and "browser_reobserve_required" in fresh.content
                    assert not fixture.submissions
                submitted = await call("main", action="click", selector="#submit")
                assert submitted.status == "success", submitted.content
                for _ in range(100):
                    if fixture.submissions:
                        break
                    await asyncio.sleep(0.01)
                assert fixture.submissions == [draft]
                record("each_agent_receives_fresh_observation_before_exactly_one_submission")

                # Hold the real tool inside the shared barrier. A second action
                # must check its host grant after this wait, not at enqueue.
                holding = asyncio.create_task(
                    call(
                        "main",
                        action="eval_js",
                        expression="fetch('/authorization-held').then(r=>{"
                        "if(!r.ok)throw Error('fixture wait failed');return 'released'})",
                    )
                )
                assert await asyncio.to_thread(fixture.authorization_started.wait, 10)
                grant_current = True
                grant_checked = asyncio.Event()

                async def require_current_grant() -> None:
                    grant_checked.set()
                    if not grant_current:
                        raise PermissionError("fixture_browser_grant_revoked")

                async def real_click() -> Any:
                    return await browser_use.ainvoke(
                        {
                            "name": "browser_use",
                            "args": {"action": "click", "selector": "#ai", "profile": profile},
                            "id": str(uuid.uuid4()),
                            "type": "tool_call",
                        }
                    )

                waiting = asyncio.create_task(
                    controls.run_ai(
                        "managed-main", "click", real_click, before_effect=require_current_grant
                    )
                )
                await asyncio.sleep(0)
                assert not grant_checked.is_set() and not waiting.done()
                grant_current = False
                fixture.authorization_release.set()
                held_result = await holding
                assert held_result.status == "success", held_result.content
                try:
                    await waiting
                    raise AssertionError("A queued action used a revoked host grant")
                except PermissionError as error:
                    assert str(error) == "fixture_browser_grant_revoked"
                assert grant_checked.is_set() and fixture.ai_actions == 1
                grant_current = True
                try:
                    await controls.run_ai(
                        "managed-main", "click", real_click, before_effect=require_current_grant
                    )
                    raise AssertionError("A new actor acted before observing the shared page")
                except ControlError as error:
                    assert error.code == "browser_reobserve_required"
                    assert error.details is not None and error.details["actionExecuted"] is False
                    assert error.details["observation"]["url"] == url
                assert fixture.ai_actions == 1
                accepted = await controls.run_ai(
                    "managed-main", "click", real_click, before_effect=require_current_grant
                )
                assert accepted.status == "success", accepted.content
                for _ in range(100):
                    if fixture.ai_actions == 2:
                        break
                    await asyncio.sleep(0.01)
                assert fixture.ai_actions == 2
                record("queued_browser_action_rechecks_host_grant_before_real_tool_effect")

                async def invalid_grant_completion() -> None:
                    return False  # type: ignore[return-value]  # Deliberately malformed provider.

                try:
                    await controls.run_ai(
                        "managed-main", "click", real_click, before_effect=invalid_grant_completion
                    )
                    raise AssertionError("A boolean was accepted as host authorization")
                except ControlError as error:
                    assert error.code == "invalid_browser_authorization"
                assert fixture.ai_actions == 2
                record("malformed_host_authorization_cannot_send_browser_input")

                check_started, check_release = asyncio.Event(), asyncio.Event()

                async def awaiting_grant() -> None:
                    check_started.set()
                    await check_release.wait()

                checking = asyncio.create_task(
                    controls.run_ai(
                        "managed-main", "click", real_click, before_effect=awaiting_grant
                    )
                )
                await asyncio.wait_for(check_started.wait(), 10)
                pausing = asyncio.create_task(controls.take())
                for _ in range(100):
                    if controls.core.state == "pausing":
                        break
                    await asyncio.sleep(0.01)
                assert controls.core.state == "pausing"
                check_release.set()
                try:
                    await checking
                    raise AssertionError("An unsent action passed Stop during its host check")
                except ControlError as error:
                    assert error.code == "stale_browser_operation"
                await pausing
                assert controls.owner == "user" and fixture.ai_actions == 2
                record("stop_during_host_authorization_fences_unsent_browser_effect")
                await controls.give_back()

                old_epoch = controls.core.epoch
                await close_profile_controls(app_root)
                recovered = await profile_control(profile, app_root)
                assert recovered.core.epoch > old_epoch and recovered.owner == "user"
                blocked = await call("main", action="click", selector="#submit")
                assert blocked.status == "error" and fixture.submissions == [draft]
                record("restart_fences_old_control_and_never_implicitly_resumes_agent")

                await recovered.give_back()
                await recovered.take()
                controls = recovered
                unsupported = {
                    "type": "tab_switch",
                    "tab_id": "unsupported-page-ws-target",
                    "controlEpoch": controls.core.epoch,
                    "targetEpoch": controls.core.target_epoch,
                }
                try:
                    await controls.input(
                        "viewer", unsupported, lambda: _handle_client_event(session, unsupported)
                    )
                    raise AssertionError("An unsupported browser action was silently acknowledged")
                except ControlError as error:
                    assert error.code == "browser_action_failed"
                assert await page.locator("#draft").input_value() == draft
                assert fixture.submissions == [draft]
                record(
                    "unsupported_page_ws_tab_action_reports_failure_without_replaying_or_retargeting"
                )
                await input_event(type="keydown", key="Shift", code="ShiftLeft", keyCode=16)
                journal = json.loads(recovered.journal.read_text(encoding="utf-8"))
                assert journal["inputPending"] is True and "Shift" not in json.dumps(journal)
                await close_profile_controls(app_root)
                recovered = await profile_control(profile, app_root)
                try:
                    await recovered.give_back()
                    raise AssertionError("AI resumed after a crash lost held input state")
                except ControlError as error:
                    assert error.code == "browser_input_recovery_required"
                record("restart_with_held_input_stays_fenced_without_journaling_typed_keys")

                proof = {
                    "passed": True,
                    "checks": checks,
                    "headful": headful,
                    "chrome": context.browser.version if context.browser else None,
                    "sourceSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "sourceInputs": {
                        name: hashlib.sha256(
                            (Path(__file__).resolve().parents[1] / name).read_bytes()
                        ).hexdigest()
                        for name in (
                            "src/octop/infra/browser/control.py",
                            "src/octop/infra/agents/middleware/browser_profile.py",
                            "src/octop/api/routers/browser/stream.py",
                            "src/octop/api/routers/browser/harness.py",
                            "src/octop/api/routers/chat/turn.py",
                            "src/octop/api/routers/chat/history.py",
                            "src/octop/infra/gateway/gateway.py",
                            "src/octop/infra/gateway/process/processor.py",
                            "pyproject.toml",
                            "uv.lock",
                        )
                    },
                    "scope": "Actual Chrome, Mosswork initializer, LangChain tool middleware and input handler; no LLM call, production identity grant, frontend or mobile acceptance.",
                }
            finally:
                await close_profile_controls(app_root)
                _registry.pop(profile, None)
                if session is not None:
                    await session.close()
                await context.close()
    except BaseException as error:
        (output / "incomplete.json").write_text(
            json.dumps(
                {
                    "passed": False,
                    "checks": checks,
                    "error": f"{type(error).__name__}: {error}",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        raise
    finally:
        fixture.authorization_release.set()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
    assert proof is not None
    (output / "receipt.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--headful", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    asyncio.run(accept(args.chrome.resolve(strict=True), args.output, headful=args.headful))
