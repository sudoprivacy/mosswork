"""moss-browser: AI-friendly browser automation via CDP."""

from moss_browser.install import (
    InstallEvent,
    chromium_executable,
    ensure_chromium,
    install_chromium_stream,
)
from moss_browser.mode import BrowserMode
from moss_browser.models import ActionMetrics, TabInfo, ToolResult
from moss_browser.session import BrowserSession
from moss_browser.settings import MossBrowserSettings, settings
from moss_browser.tool_interface import browser_tool

__all__ = [
    "BrowserSession",
    "browser_tool",
    "BrowserMode",
    "MossBrowserSettings",
    "settings",
    "ToolResult",
    "ActionMetrics",
    "TabInfo",
    "InstallEvent",
    "chromium_executable",
    "ensure_chromium",
    "install_chromium_stream",
]
