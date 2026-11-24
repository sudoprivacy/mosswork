"""Real Chromium, desktop proxy, canonical login, durable queue and SudoCode.

The SMS sink and this employee's resource grant are local fixtures. Build the
desktop dashboard with desktop/build-dashboard.py before selecting this test.
The Router account is supplied by the operator and billed for three turns.
"""

import asyncio
import json
import os
import re
import socket
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import pytest
import uvicorn
from moss_harness.acp.models import ACPConfig, ACPRunnerConfig
from moss_harness.acp.service import ACPService

from octop.api.app import build_app
from octop.infra.agents.manager import AgentManager
from octop.infra.execution.admission import CanonicalRunAdmissions
from octop.infra.execution.gateway import GatewayExecution
from octop.infra.execution.service import ExecutionService
from octop.infra.identity.composition import build_canonical_business
from octop.infra.native_host.context import HostSelection
from tests.unit.identity.test_human_sessions import _NUMBER
from tests.unit.test_canonical_business_product_composition import (
    application as application,
)
from tests.unit.test_canonical_business_product_composition import (
    environment as environment,
)
from tests.unit.test_canonical_business_product_composition import (
    legacy_db as legacy_db,
)
from tests.unit.test_canonical_business_product_composition import (
    product as product,
)
from tests.unit.test_canonical_business_product_composition import (
    retained_original,
)


@pytest.mark.live
@pytest.mark.skipif(
    not os.getenv("MOSSWORK_LIVE_EMPLOYEE_BROWSER"), reason="Opt-in live browser/model test"
)
async def test_employee_opens_and_continues_retained_sudocode(application, environment, tmp_path):
    from playwright.async_api import async_playwright, expect

    root = Path(__file__).resolve().parents[2]
    module = root / "desktop/src"
    dashboard = module / "assets/company-dashboard.zip"
    assert dashboard.is_file(), "Build the desktop dashboard before the browser test"
    proxy = tmp_path / ("browser-host.exe" if os.name == "nt" else "browser-host")
    build = await asyncio.create_subprocess_exec(
        "go",
        "build",
        "-p",
        "1",
        "-o",
        str(proxy),
        "./internal/companyui/testdata/browser-host",
        cwd=module,
    )
    assert await build.wait() == 0
    model = os.environ["MOSSWORK_LIVE_SUDOCODE_MODEL"]
    config = tmp_path / "engine"
    config.mkdir()
    profile = {
        "auth_modes": {
            "proxy": {
                "live": {
                    "baseUrl": os.environ["MOSSWORK_LIVE_ROUTER_URL"],
                    "apiKeyEnv": "MOSSWORK_LIVE_ROUTER_KEY",
                }
            }
        },
        "models": {
            model: {
                "alias": model,
                "name": model,
                "input": ["text"],
                "providers": {"proxy": {"provider": "live", "model": model}},
            }
        },
    }
    (config / "sudocode.json").write_text(json.dumps(profile), encoding="utf-8")
    runner = ACPRunnerConfig(
        enabled=True,
        command=os.getenv("MOSSWORK_LIVE_SUDOCODE_BINARY", "scode"),
        args=[
            "--auth",
            "proxy",
            "--account",
            "live",
            "--model",
            model,
            "--permission-mode",
            "read-only",
            "acp",
        ],
        env={"SCODE_GLOBAL_CONFIG_DIR": str(config)},
    )
    engine = ACPService(config=ACPConfig(runners={"sudocode": runner}))
    thread_id = "existing-product-thread"
    project_name = f"Maple Garden {uuid.uuid4().int % 100000:05d}"
    observed_sessions = []

    async def notify(event, _last):
        assert event.get("type") != "tool_call"

    async def ask(text, existing):
        async with asyncio.timeout(90):
            reply = await engine.run_turn(
                thread_id=thread_id,
                runner="sudocode",
                cwd=str(tmp_path),
                prompt_blocks=[{"type": "text", "text": text}],
                on_message=notify,
                require_existing=existing,
            )
        session = await engine.get_session(thread_id, "sudocode")
        assert reply["status"] == "completed" and session and session.acp_session_id
        observed_sessions.append(session.acp_session_id)
        return reply["event"]["text"]

    host = web = web_task = queue = None
    try:
        assert project_name in await ask(
            f"Our demo project is named {project_name}. Suggest a meeting title that includes "
            "the full project name.",
            False,
        )
        server = application.state.mosswork_server
        now = environment.kwargs["clock"]()
        issuer = build_canonical_business(
            environment.path, **{**environment.kwargs, "clock": lambda: now}
        )
        application = build_app(server, **issuer.app_kwargs())
        original = await retained_original(
            application, environment, durable_sid=observed_sessions[0]
        )
        now += environment.kwargs["phone_config"].resend_cooldown_sec + 1

        async def check(agent, thread, *, admission, operation):
            assert operation in {"observe", "submit"} and agent == original.selection.agent_id
            actor = await admission.revalidate()
            assert actor.projection == original.principal.identity.projection
            binding = await original.selections.current(
                actor.snapshot, HostSelection(agent, thread)
            )
            assert binding.durable_session_id == observed_sessions[0]

        async def boot():
            session = await engine.get_session(thread_id, "sudocode")
            assert session and session.acp_session_id == observed_sessions[0]

        followup_started = asyncio.Event()
        continue_followup = asyncio.Event()
        followup = "Write a short agenda heading that includes the exact same project name."

        async def stream(agent, request, *, admission):
            await check(agent, request["thread_id"], admission=admission, operation="submit")
            if request["text"] == followup:
                followup_started.set()
                await asyncio.wait_for(continue_followup.wait(), 30)
            yield {"type": "token", "content": await ask(request["text"], True)}
            yield {"type": "done"}

        backend = SimpleNamespace(assert_chat_current=check, stream=stream, boot=boot)
        registry = AgentManager(
            repos=server.services.repos,
            paths=server.paths,
            config=server.config,
            managed_backend=backend,
        )
        await registry.boot()
        admissions = CanonicalRunAdmissions(server.services.execution_repo)
        queue = ExecutionService(
            admissions.repo,
            node="browser-test",
            incarnation=uuid.uuid4().hex,
            role="monolith",
            human_admissions=admissions,
        )
        processor = SimpleNamespace()
        server.canonical_configuration = object()
        server.canonical = SimpleNamespace(
            admissions=admissions,
            backend=backend,
            runtime_inputs=SimpleNamespace(selections=original.selections),
        )
        server.app_runtime = SimpleNamespace(
            user_manager=None, agent_registry=registry, gateway=SimpleNamespace(processor=processor)
        )
        server.execution = queue
        processor.execution = registry.execution = GatewayExecution(server, queue)
        await queue.start()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            web = uvicorn.Server(
                uvicorn.Config(application, access_log=False, log_level="error", lifespan="off")
            )
            web_task = asyncio.create_task(web.serve(sockets=[sock]))
            async with asyncio.timeout(15):
                while not web.started:
                    await asyncio.sleep(0.05)
            host = await asyncio.create_subprocess_exec(
                str(proxy),
                f"http://127.0.0.1:{sock.getsockname()[1]}",
                str(dashboard),
                str(tmp_path / "desktop"),
                stdin=-1,
                stdout=-1,
            )
            parts = urlsplit((await host.stdout.readline()).decode().strip())
            assert parts.hostname == "127.0.0.1"
            entry = urlunsplit(parts._replace(query="ui_locale=zh"))
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(executable_path=pw.chromium.executable_path)

                async def login(page):
                    await page.goto(entry)
                    await page.locator("#canonical-phone").fill(_NUMBER)
                    async with page.expect_response(
                        lambda r: urlsplit(r.url).path.endswith("/phone/send-code")
                    ) as sent:
                        await page.get_by_role("button", name="发送验证码").click()
                    assert (await sent.value).ok
                    await page.locator("#canonical-code").fill(environment.sender.sent[-1][1])
                    await page.locator("form button[type=submit]").click()
                    await expect(page.get_by_role("heading", name="Original")).to_be_visible()

                try:
                    page = await browser.new_page(locale="zh-CN")
                    await login(page)
                    questions = [
                        "What was the full project name in the meeting title we discussed?",
                        followup,
                    ]
                    answers = []
                    for i, question in enumerate(questions):
                        if i:
                            await page.reload()
                            await expect(page.get_by_role("log")).to_contain_text(answers[0])
                            await page.get_by_role("button", name="退出登录").click()
                            await expect(page.locator("#canonical-phone")).to_be_visible()
                            await browser.close()
                            now += environment.kwargs["phone_config"].resend_cooldown_sec + 1
                            browser = await pw.chromium.launch(
                                executable_path=pw.chromium.executable_path
                            )
                            page = await browser.new_page(locale="zh-CN")
                            await login(page)
                            await expect(page.get_by_role("log").locator("p")).to_have_text(
                                [questions[0], answers[0]]
                            )
                            assert len(observed_sessions) == 2
                        prompt = page.get_by_role("textbox", name="给你的智能体发消息")
                        await prompt.fill(question)
                        await page.get_by_role("button", name=re.compile(r"^发\s*送$")).click()
                        if i:
                            await asyncio.wait_for(followup_started.wait(), 15)
                            try:
                                await page.reload()
                                await expect(page.get_by_role("log").locator("p")).to_have_text(
                                    [questions[0], answers[0], question, ""]
                                )
                                await expect(prompt).to_be_disabled()
                            finally:
                                continue_followup.set()
                        reply = page.get_by_role("log").locator("div").nth(i).locator("p").nth(1)
                        await expect(reply).to_contain_text(project_name, timeout=95000)
                        await expect(prompt).to_be_enabled()
                        answers.append(await reply.inner_text())
                    await page.reload()
                    transcript = page.get_by_role("log").locator("p")
                    await expect(transcript).to_have_text(
                        [questions[0], answers[0], questions[1], answers[1]]
                    )
                    assert observed_sessions == [observed_sessions[0]] * 3
                finally:
                    await browser.close()
    finally:
        if host:
            host.stdin.close()
            await asyncio.wait_for(host.wait(), 10)
        if web:
            web.should_exit = True
        if web_task:
            await asyncio.wait_for(web_task, 10)
        if queue:
            await queue.close()
        await engine.close_all_sessions()
