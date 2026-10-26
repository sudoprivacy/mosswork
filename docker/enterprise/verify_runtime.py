"""Offline image check: load embedding dependencies and render with the real browser."""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import os
import tempfile
from pathlib import Path


async def verify() -> dict[str, object]:
    dependencies = {}
    for name in ("fastembed", "huggingface_hub", "onnxruntime"):
        importlib.import_module(name)
        dependencies[name] = importlib.metadata.version(name)

    from importlib import import_module, util
    from importlib.metadata import version

    release_version = version("mosswork")
    for component in ("harness", "memory", "browser", "gateway"):
        import_module(f"moss_{component}")
        if version(f"moss-{component}") != release_version:
            raise RuntimeError(f"moss-{component} version differs from mosswork")
        if util.find_spec(f"octop_{component}") is not None:
            raise RuntimeError(f"Unexpected upstream octop_{component}")

    from moss_browser import BrowserSession
    from moss_browser.profile import ProfileManager
    from moss_browser.settings import MossBrowserSettings

    from octop.api.routers.browser.env import _probe_env

    probe = _probe_env()
    if not probe["browsers_ok"] or not probe["harness_browser"]:
        raise RuntimeError(f"Browser environment is incomplete: {probe}")

    with tempfile.TemporaryDirectory(prefix="mosswork-runtime-check-") as temporary:
        root = Path(temporary)
        settings = MossBrowserSettings(
            profiles_dir=root / "profiles", screenshots_dir=root / "shots"
        )
        session = await BrowserSession.create(
            profile="image-check",
            headless=True,
            profile_manager=ProfileManager(settings=settings),
            settings=settings,
        )
        try:
            navigation = await session.navigate(
                "data:text/html,<title>mosswork runtime check</title><h1>Browser ready</h1>"
            )
            if not navigation.success:
                raise RuntimeError(f"Browser navigation failed: {navigation.error}")
            screenshot = root / "browser.png"
            result = await session.screenshot(path=str(screenshot))
            if not result.success:
                raise RuntimeError(f"Browser screenshot failed: {result.error}")
            if (result.metadata or {}).get("title") != "mosswork runtime check":
                raise RuntimeError("Browser did not render the verification page")
            data = screenshot.read_bytes()
            if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 1000:
                raise RuntimeError("Browser did not produce a valid screenshot")
        finally:
            await session.close(kill=True)

    return {
        "uid": os.getuid(),
        "dependencies": dependencies,
        "browser": probe,
        "browser_launch_verified": True,
        "browser_screenshot_bytes": len(data),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(asyncio.wait_for(verify(), timeout=60))))
