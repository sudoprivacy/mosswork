"""Qualify the Mosswork transport against the published adb Worker and Chrome.

Uses an isolated profile and local HTTP, with explicit fixture identities and
credentials. This does not qualify native grants, a provider, UI or production.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from octop.infra.browser.worker import BrowserWorkerClient, BrowserWorkerError, WorkerBinding

HTML = """<!doctype html><meta charset=utf-8><title>Worker transport acceptance</title>
<h1>Same browser draft</h1><textarea id=draft style="width:400px;height:100px"></textarea>
<button id=submit onclick="fetch('/submit',{method:'POST',body:draft.value})">Submit once</button>
<script>document.cookie='worker_fixture=original;path=/';
window.trusted=[];draft.addEventListener('input',e=>trusted.push(e.isTrusted));</script>"""


async def serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None  # noqa: SLF001
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    return runner, f"http://127.0.0.1:{port}"


def epochs(state: dict[str, Any]) -> dict[str, int]:
    return {name: state[name] for name in ("controlEpoch", "targetEpoch")}


async def refused(
    client: BrowserWorkerClient, action: dict[str, Any], code: str, status: int | None
) -> BrowserWorkerError:
    try:
        await client.invoke(action)
    except BrowserWorkerError as error:
        assert (error.code, error.status) == (code, status), (error.code, error.status)
        return error
    raise AssertionError("Expected one fixture refusal without retry")


async def accept(chrome: Path, output: Path, *, headful: bool, chrome_args: list[str]) -> None:
    from ai_dev_browser.remote.worker import Worker, application  # noqa: PLC0415

    source = Path(__file__).resolve().parents[1]
    inputs = [Path(__file__), source / "src/octop/infra/browser/worker.py", source / "uv.lock"]
    hashes = {
        str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in inputs
    }
    checks: list[dict[str, Any]] = []

    def record(name: str) -> None:
        checks.append({"name": name, "passed": True})
        (output / "checks.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
        print(json.dumps(checks[-1]), flush=True)

    submissions: list[tuple[str, str | None]] = []
    submitted = asyncio.Event()

    async def page(_request: web.Request) -> web.Response:
        return web.Response(text=HTML, content_type="text/html")

    async def submit(request: web.Request) -> web.Response:
        submissions.append((await request.text(), request.cookies.get("worker_fixture")))
        submitted.set()
        return web.Response(text="accepted")

    fixture = web.Application()
    fixture.add_routes([web.get("/", page), web.post("/submit", submit)])
    page_runner, page_origin = await serve(fixture)
    host_token, ai_token = secrets.token_hex(32), secrets.token_hex(32)
    worker = Worker(
        output / "worker-profile",
        host_token,
        ai_token,
        chrome_path=str(chrome),
        display_mode="native",
        chrome_args=chrome_args,
    )
    worker_runner, worker_origin = await serve(application(worker))
    binding = WorkerBinding("fixture-org", "fixture-owner", "fixture-native-session")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as http:
            client = BrowserWorkerClient(http, worker_origin, host_token, binding, "user-fixture")
            # Explicit negative fixture: no production credentials or permission change.
            await refused(
                client,
                {"kind": "start", "transport": "cdp"},
                "explicit_browser_launch_mode_required",
                409,
            )
            assert worker.browser is None
            started = await client.invoke(
                {"kind": "start", "transport": "cdp", "isHeadless": not headful}
            )
            state = started["session"]
            assert state["isHeadless"] is (not headful)
            assert state["state"] == "human" and state["transport"] == "cdp"
            assert worker.display is None
            record("explicit_mode_uses_actual_worker_chrome_without_display_service")

            state = (
                await client.invoke({"kind": "navigate", "url": page_origin + "/", **epochs(state)})
            )["session"]
            for viewer in ("panel", "independent"):
                await client.invoke({"kind": "attach", "viewerId": viewer})
            frames = []
            for viewer in ("panel", "independent"):
                result = await client.invoke({"kind": "frame", "viewerId": viewer, **epochs(state)})
                assert result["session"]["browserSessionId"] == state["browserSessionId"]
                assert result["session"]["activeTargetId"] == state["activeTargetId"]
                assert epochs(result["frame"]) == epochs(state)
                frames.append(result["frame"])
            assert frames[0]["data"] and frames[1]["data"]
            (output / "worker-page.jpg").write_bytes(base64.b64decode(frames[0]["data"]))
            observed_page = await worker.session._connect_page()  # noqa: SLF001  # Read-only test oracle.
            rect = await observed_page.js(
                "(()=>{const r=draft.getBoundingClientRect();return {x:r.x+20,y:r.y+20}})()"
            )
            for event in ({"kind": "click", **rect}, {"kind": "text", "text": "同一个草稿 Ω"}):
                state = (await client.invoke({"kind": "input", "input": event, **epochs(state)}))[
                    "session"
                ]
            assert await observed_page.js("draft.value") == "同一个草稿 Ω"
            assert await observed_page.js("trusted.every(Boolean) && trusted.length>0")
            await client.invoke({"kind": "detach", "viewerId": "independent"})
            await client.invoke({"kind": "attach", "viewerId": "independent"})
            assert await observed_page.js("draft.value") == "同一个草稿 Ω"
            assert not submissions
            record("two_viewers_and_reconnection_preserve_same_target_trusted_unicode_and_draft")

            foreign = BrowserWorkerClient(
                http,
                worker_origin,
                host_token,
                WorkerBinding("fixture-org", "fixture-owner", "other-session"),
                "user-fixture",
            )
            await refused(foreign, {"kind": "state"}, "browser_profile_in_use", 409)
            assert worker.binding == binding.to_wire()
            stale = {"kind": "input", "input": {"kind": "text", "text": "STALE"}, **epochs(state)}
            returned = await client.invoke({"kind": "return", **epochs(state)})
            state = returned["session"]
            assert state["state"] == "ai" and returned["observation"]["frame"]["data"]
            await refused(client, stale, "stale_browser_operation", 409)
            command = {
                "kind": "command",
                "method": "Input.insertText",
                "params": {"text": " + AI"},
                **epochs(state),
            }
            async with http.post(
                worker_origin + "/agent",
                headers={"Authorization": "Bearer " + ai_token},
                json=command,
            ) as response:
                assert response.status == 200, await response.text()
            state = (await client.invoke({"kind": "take", **epochs(state)}))["session"]
            command = {**command, "params": {"text": "BLOCKED"}, **epochs(state)}
            async with http.post(
                worker_origin + "/agent",
                headers={"Authorization": "Bearer " + ai_token},
                json=command,
            ) as response:
                body = await response.json()
                assert response.status == 409 and body["error"] == "browser_control_held_by_user", (
                    body
                )
            assert await observed_page.js("draft.value") == "同一个草稿 Ω + AI"
            assert not submissions
            record("fixed_binding_stale_versions_and_human_lease_fence_actual_agent_writes")

            button = await observed_page.js(
                "(()=>{const r=document.querySelector('#submit').getBoundingClientRect();return {x:r.x+10,y:r.y+10}})()"
            )
            await client.invoke(
                {"kind": "input", "input": {"kind": "click", **button}, **epochs(state)}
            )
            await asyncio.wait_for(submitted.wait(), 10)
            assert submissions == [("同一个草稿 Ω + AI", "original")]
            record("one_trusted_submission_keeps_original_profile_cookie")
            for viewer in ("panel", "independent"):
                await client.invoke({"kind": "detach", "viewerId": viewer})
            await client.invoke({"kind": "stop", **epochs(state)})
            assert worker.session is None and worker.browser is None
    finally:
        await worker_runner.cleanup()
        await page_runner.cleanup()

    # Real HTTP counterexamples. Each intentionally rejected fixture is sent once.
    requests: list[str] = []
    leaked: list[str | None] = []

    async def sink(request: web.Request) -> web.Response:
        leaked.append(request.headers.get("Authorization"))
        return web.json_response({"session": {}})

    async def counterexample(request: web.Request) -> web.Response:
        kind = (await request.json())["action"]["kind"]
        requests.append(kind)
        if kind == "redirect":
            raise web.HTTPTemporaryRedirect(location="/sink")
        if kind == "drop":
            assert request.transport is not None
            request.transport.close()
            return web.Response()
        if kind == "malformed":
            return web.Response(text="not a worker reply")
        return web.json_response(
            {"error": "stale_browser_operation", "private": host_token}, status=409
        )

    faulty = web.Application()
    faulty.add_routes([web.post("/invoke", counterexample), web.post("/sink", sink)])
    runner, origin = await serve(faulty)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
            client = BrowserWorkerClient(http, origin, host_token, binding, "user-fixture")
            redirect = await refused(
                client, {"kind": "redirect"}, "browser_worker_request_failed", 307
            )
            assert redirect.uncertain and not leaked
            failure = await refused(client, {"kind": "refuse"}, "stale_browser_operation", 409)
            assert host_token not in repr(failure)
            malformed = await refused(
                client, {"kind": "malformed"}, "browser_worker_request_failed", 200
            )
            assert malformed.uncertain
            lost = await refused(client, {"kind": "drop"}, "browser_worker_outcome_unknown", None)
            assert lost.uncertain and requests == ["redirect", "refuse", "malformed", "drop"]
            record(
                "actual_http_redirect_refusal_malformed_reply_and_lost_reply_never_replay_or_leak_credentials"
            )
    finally:
        await runner.cleanup()
    assert hashes == {
        str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in inputs
    }
    receipt = {
        "passed": True,
        "checks": checks,
        "sdkVersion": version("ai-dev-browser"),
        "headful": headful,
        "chromeLaunch": launch_profile(chrome_args),
        "inputSha256": hashes,
        "scope": __doc__,
        "production": False,
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")


def launch_profile(chrome_args: list[str]) -> dict[str, bool | int]:
    """Keep explicit launch facts without recording private argument values."""
    return {
        "sandboxDisabled": "--no-sandbox" in chrome_args,
        "additionalArgumentCount": len(chrome_args),
    }


def write_launch_diagnostic(output: Path) -> None:
    """Publish bounded launch facts without private ownership IDs or raw errors."""
    private = output / "worker-profile/launch-failure.json"
    try:
        value = json.loads(private.read_text(encoding="utf-8"))
    except OSError:
        return
    except (ValueError, UnicodeError):
        value = None
    summary: dict[str, Any] = {"valid": isinstance(value, dict)}
    if isinstance(value, dict):
        code = value.get("error_code")
        summary.update(
            error_code=code
            if isinstance(code, str)
            and code
            in {"not_found", "validation", "unsupported", "conflict", "transient", "timeout"}
            else "unrecognized",
            started=value.get("started") is True,
            processIdentityPresent=type(value.get("pid")) is int,
            privateLaunchIdentityPresent=bool(value.get("launch_token")),
        )
    (output / "worker-launch-diagnostic.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--chrome-arg", action="append", default=[])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        asyncio.run(
            accept(args.chrome, args.output, headful=args.headful, chrome_args=args.chrome_arg)
        )
    except BaseException as error:
        write_launch_diagnostic(args.output)
        (args.output / "incomplete.json").write_text(
            json.dumps({"passed": False, "errorType": type(error).__name__, "error": str(error)}),
            encoding="utf-8",
        )
        raise


if __name__ == "__main__":
    main()
