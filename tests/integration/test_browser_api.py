"""tests/integration/test_browser_api.py — remote browser endpoints.

We deliberately don't spawn a real Chrome in the test suite. These
cases drive env-status, ``_probe_env``, install SSE, harness-sessions,
and shutdown gates. Live screencast is covered by dashboard + WS unit
tests.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# --- env-status -------------------------------------------------------------


async def test_env_status_returns_shape(env: Any) -> None:
    c, _srv, auth = env
    r = await c.get("/api/browser/env-status", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "playwright" in body
    assert "browsers_ok" in body
    assert isinstance(body["playwright"], bool)
    assert isinstance(body["browsers_ok"], bool)


async def test_env_status_requires_auth(env: Any) -> None:
    c, _srv, _auth = env
    r = await c.get("/api/browser/env-status")
    assert r.status_code == 401


def test_probe_env_when_playwright_missing() -> None:
    """``_probe_env`` should not raise even with playwright import broken."""
    import builtins

    from octop.api.routers.browser import env as br

    original_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kw: Any) -> Any:
        if name == "playwright":
            raise ImportError("simulated")
        return original_import(name, *args, **kw)

    with patch("builtins.__import__", side_effect=fake_import):
        out = br._probe_env()
    assert out["playwright"] is False
    if not out.get("harness_browser"):
        assert out["browsers_ok"] is False
        assert out["error"]


def test_probe_env_harness_browser_without_chromium() -> None:
    """``browsers_ok`` must stay false when no Chrome/Chromium is available."""
    from octop.api.routers.browser import env as br

    try:
        import moss_browser  # noqa: F401
    except ImportError:
        return

    with patch(
        "moss_browser.cdp.launcher.find_chrome",
        return_value=None,
    ):
        out = br._probe_env()
    assert out["harness_browser"] is True
    assert out["browsers_ok"] is False
    assert out["error"]


def test_probe_env_accepts_system_chrome() -> None:
    """System Chrome via find_chrome is enough for browsers_ok (same as launch)."""
    from octop.api.routers.browser import env as br

    try:
        import moss_browser  # noqa: F401
    except ImportError:
        return

    with (
        patch(
            "moss_browser.cdp.launcher.find_chrome",
            return_value="/usr/bin/google-chrome",
        ),
        patch(
            "octop.infra.browser.setup.playwright_chromium_installed",
            return_value=False,
        ),
        patch(
            "octop.infra.browser.setup.chrome_source_for_path",
            return_value="system",
        ),
    ):
        out = br._probe_env()
    assert out["harness_browser"] is True
    assert out["browsers_ok"] is True
    assert out["chrome_path"] == "/usr/bin/google-chrome"
    assert out["chrome_source"] == "system"
    assert out["playwright_chromium"] is False


def test_verify_browser_binary_ok(tmp_path: Path) -> None:
    from octop.api.routers.browser import env as br

    if os.name == "nt":
        # Windows cannot exec a shebang script; use a .bat that ignores args.
        exe = tmp_path / "fake-chrome.bat"
        exe.write_text("@echo Chrome 1.0\r\n", encoding="utf-8")
    else:
        exe = tmp_path / "fake-chrome"
        exe.write_text("#!/bin/sh\necho 'Chrome 1.0'\n", encoding="utf-8")
        exe.chmod(0o755)
    ok, msg = br._verify_browser_binary(str(exe))
    assert ok is True
    assert "Chrome" in msg


async def test_legacy_playwright_session_routes_removed(env: Any) -> None:
    """In-process Playwright CRUD is gone; the product path is harness-sessions."""
    c, _srv, auth = env
    r = await c.get("/api/browser/sessions", headers=auth)
    assert r.status_code in (404, 405)
    r = await c.post("/api/browser/sessions", headers=auth)
    assert r.status_code in (404, 405)
    assert r.status_code != 201


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_browser_probe_preserves_non_utf8_failure_details(tmp_path: Path) -> None:
    """A real failed command keeps diagnostics even on a UTF-8 Python host."""
    from octop.api.routers.browser.env import _verify_browser_binary

    program = (
        "import sys;sys.stderr.buffer.write(bytes([255])+b'Chromium probe failed');sys.exit(7)"
    )
    if os.name == "nt":
        command = tmp_path / "probe.bat"
        command.write_text(f'@"{sys.executable}" -c "{program}"\r\n', encoding="utf-8")
    else:
        command = tmp_path / "probe"
        command.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(program)}\n",
            encoding="utf-8",
        )
        command.chmod(0o755)
    ok, detail = _verify_browser_binary(str(command))
    assert ok is False
    assert "code 7" in detail and "Chromium probe failed" in detail


async def test_harness_sessions_shape(env: Any) -> None:
    c, _srv, auth = env
    r = await c.get("/api/browser/harness-sessions", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "sessions" in body
    assert isinstance(body["sessions"], list)


async def test_harness_list_tabs_uses_connected_target_and_browser_context() -> None:
    from octop.api.routers.browser import harness as harness_router

    pages = [
        {
            "type": "page",
            "targetId": "TAB-1",
            "browserContextId": "own",
            "url": "https://www.bilibili.com/",
            "title": "B站",
        },
        {
            "type": "page",
            "targetId": "TAB-2",
            "browserContextId": "own",
            "url": "https://weibo.com/",
            "title": "微博",
        },
        {
            "type": "page",
            "targetId": "OTHER",
            "browserContextId": "other",
            "url": "https://example.com/private",
        },
    ]
    client = SimpleNamespace(
        send=AsyncMock(side_effect=[{"targetInfo": pages[1]}, {"targetInfos": pages}])
    )
    session = SimpleNamespace(_internal=SimpleNamespace(client=client))
    tabs = await harness_router.harness_list_tabs(session)
    assert [tab["id"] for tab in tabs] == ["TAB-1", "TAB-2"]
    assert [tab["active"] for tab in tabs] == [False, True]


async def test_shutdown_requires_auth(env: Any) -> None:
    c, _srv, _auth = env
    r = await c.post("/api/browser/shutdown")
    assert r.status_code == 401


async def test_shutdown_ignores_client_profile(env: Any) -> None:
    c, _srv, auth = env
    result = SimpleNamespace(success=True, error=None)
    with patch(
        "moss_browser.tool_interface.browser_tool",
        new=AsyncMock(return_value=result),
    ) as tool:
        r = await c.post("/api/browser/shutdown?profile=work", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "profile": "user-1"}
    tool.assert_awaited_once_with(
        action="close_session",
        profile="user-1",
        kill=True,
    )


# --- install spawn ---------------------------------------------------------


async def test_install_returns_pid(env: Any) -> None:
    c, _srv, auth = env

    async def fake_stream():
        yield {"log": "downloading chromium"}
        yield {"done": True, "success": True}

    with patch(
        "moss_browser.install_chromium_stream",
        side_effect=lambda: fake_stream(),
    ):
        r = await c.post("/api/browser/install", headers=auth)
    assert r.status_code in (200, 202)
