"""Helpers for attaching dashboard / chat UI to moss-browser sessions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any, Literal

from ai_dev_browser.remote.control import ControlError
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from octop.api.deps import current_user, get_server
from octop.infra.browser.control import profile_control
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.utils.browser_media import user_browser_profile

logger = logging.getLogger(__name__)

router = APIRouter()


async def control_owner_for(session_id: str, root: Path | None = None) -> str:
    """Report the actual shared input barrier, including restart recovery."""
    return (await profile_control(session_id, root)).owner


class HandoffBody(BaseModel):
    """Control handoff request: switch between agent and user control."""

    target: str = Field(description="Compatibility control target: agent or user.")
    reason: str = Field(default="", description="Optional explanation supplied by the caller.")


class HarnessSession(BaseModel):
    session_id: str = Field(
        description="Authenticated user's local browser profile; not a tenant grant."
    )
    profile_name: str
    conversation_id: str
    channel_source: str
    state: str
    control_owner: Literal["agent", "user"]
    current_url: str
    created_at: int = Field(description="Unix timestamp in milliseconds.")
    last_activity_at: int = Field(description="Unix timestamp in milliseconds.")


class HarnessSessionsResponse(BaseModel):
    ok: bool
    environment: str
    sessions: list[HarnessSession]


class HandoffResponse(BaseModel):
    ok: bool
    session: HarnessSession


class BrowserShutdownResponse(BaseModel):
    ok: bool
    profile: str


async def _is_session_alive(sess: Any, *, timeout: float = 2.0) -> bool:
    """Probe a cached session with a cheap CDP round-trip.

    A previously-registered session's underlying CDP WebSocket can die out
    from under us (browser crash, OOM kill, network blip) while the Python
    object stays in ``_registry`` forever. Reusing a dead session makes every
    subsequent action fail with confusing low-level errors like
    ``no close frame received or sent``. This does a fast, side-effect-free
    ``Runtime.evaluate`` to confirm the CDP connection is still usable.
    """
    try:
        client = sess._internal.client  # noqa: SLF001
        send = getattr(client, "send_passive", client.send)
        await asyncio.wait_for(
            send("Runtime.evaluate", {"expression": "1", "returnByValue": True}),
            timeout=timeout,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("cached harness session failed health check: %s", exc)
        return False


async def resolve_harness_session(
    profile_hint: str | None,
    *,
    server: Any | None = None,
    agent_id: str | None = None,
    create: bool = True,
) -> Any | None:
    """Return a live :class:`moss_browser.BrowserSession` for ``profile_hint``.

    *profile_hint* must be a concrete profile name (``user-<id>``). Empty /
    ``auto`` never falls back to another user's session or to ``default``.

    When ``create=True`` (screencast / interactive clients) a missing live
    session is launched. Listen-only dashboard hooks pass ``create=False``
    so a status WebSocket cannot spawn Chrome just to watch for updates.

    Cached entries are health-checked before reuse — a dead/stale session
    (e.g. browser crashed) is evicted and replaced with a freshly launched
    one rather than being handed back to fail again.

    When ``create=False`` and no live session exists, returns ``None``.
    """
    try:
        from moss_browser import BrowserSession
        from moss_browser.tool_interface import _registry
    except ImportError as exc:
        raise MossworkError(
            ErrorCode.INTERNAL_ERROR,
            "moss-browser not installed",
            status=503,
        ) from exc

    hint = (profile_hint or "").strip()
    if not hint or hint == "auto":
        if not create:
            return None
        raise MossworkError(ErrorCode.SLASH_BAD_ARGS, "browser profile is required")

    cached = _registry.get(hint)
    if cached is not None:
        if await _is_session_alive(cached):
            return cached
        logger.warning(
            "harness session %r is dead (stale CDP connection); discarding%s",
            hint,
            " and relaunching" if create else "",
        )
        _registry.pop(hint, None)
        with contextlib.suppress(Exception):
            await cached.close()

    if not create:
        return None

    profile = hint
    harness_settings = None
    if server is not None and agent_id:
        from octop.api.common.agent_workspace import (  # noqa: PLC0415
            resolve_agent_workspace_dir,
        )
        from octop.infra.utils.browser_media import (  # noqa: PLC0415
            agent_outbound_screenshots_dir,
            harness_settings_for_screenshots_dir,
        )

        shots = agent_outbound_screenshots_dir(resolve_agent_workspace_dir(server, agent_id))
        harness_settings = harness_settings_for_screenshots_dir(shots)

    from octop.infra.browser.setup import (  # noqa: PLC0415
        prepare_harness_profile_for_launch,
        resolve_browser_display,
    )

    # Shared ~/.mosswork/browser-profiles (not per-agent workspace).
    await prepare_harness_profile_for_launch(profile)
    # Virtual desktop (Xvnc :99) → headed Chrome so the window shows on
    # remote desktop; otherwise force headless (do not use mode=auto — a
    # stale $DISPLAY would still resolve to headed and crash Chromium).
    display = resolve_browser_display()
    launch_mode = "headed" if display else "headless"

    # Fresh ProfileManager picks up any BROWSER_USE_PROFILES_DIR relocation
    # done by prepare (default singleton is bound at import time).
    from moss_browser.profile import ProfileManager  # noqa: PLC0415
    from moss_browser.settings import settings as hb_settings  # noqa: PLC0415

    profile_manager = ProfileManager(base_dir=Path(hb_settings.profiles_dir))

    try:
        sess = await BrowserSession.create(
            profile=profile,
            mode=launch_mode,  # type: ignore[arg-type]
            settings=harness_settings,
            profile_manager=profile_manager,
        )
    except Exception as exc:
        # Chrome exit 21 / ProcessSingleton usually means a stale lock or a
        # non-writable profile left by a previous root/non-root mismatch.
        msg = str(exc)
        if (
            "returncode=21" in msg
            or "ProcessSingleton" in msg
            or "SingletonLock" in msg
            or "profile directory" in msg.lower()
            or "/run/user/" in msg
        ):
            logger.warning(
                "Browser launch failed for %r (%s); recovering profile and retrying",
                profile,
                exc,
            )
            await prepare_harness_profile_for_launch(profile, force_recover=True)
            display = resolve_browser_display()
            launch_mode = "headed" if display else "headless"
            profile_manager = ProfileManager(base_dir=Path(hb_settings.profiles_dir))
            try:
                sess = await BrowserSession.create(
                    profile=profile,
                    mode=launch_mode,  # type: ignore[arg-type]
                    settings=harness_settings,
                    profile_manager=profile_manager,
                )
            except Exception as retry_exc:
                raise MossworkError(
                    ErrorCode.INTERNAL_ERROR,
                    f"failed to attach browser profile {profile!r}: {retry_exc}",
                    status=503,
                ) from retry_exc
        else:
            raise MossworkError(
                ErrorCode.INTERNAL_ERROR,
                f"failed to attach browser profile {profile!r}: {exc}",
                status=503,
            ) from exc
    _registry[profile] = sess
    return sess


async def harness_page_url(sess: Any) -> str:
    try:
        client = sess._internal.client  # noqa: SLF001
        send = getattr(client, "send_passive", client.send)
        info = await send(
            "Runtime.evaluate",
            {
                "expression": "location.href",
                "returnByValue": True,
            },
        )
        return str(info.get("result", {}).get("value", "") or "")
    except Exception:
        return ""


async def harness_viewport(sess: Any) -> tuple[int, int] | None:
    """Observe actual viewport geometry before applying a viewer's start size."""
    try:
        client = sess._internal.client  # noqa: SLF001
        send = getattr(client, "send_passive", client.send)
        response = await send(
            "Runtime.evaluate",
            {"expression": "[window.innerWidth, window.innerHeight]", "returnByValue": True},
        )
        size = response.get("result", {}).get("value")
        if (
            isinstance(size, list)
            and len(size) == 2
            and all(type(value) is int and value > 0 for value in size)
        ):
            return size[0], size[1]
    except Exception:
        logger.debug("Could not observe the browser viewport", exc_info=True)
    return None


async def harness_list_tabs(sess: Any) -> list[dict[str, Any]]:
    """Read tabs through the connected transport, within its browser context."""
    try:
        client = sess._internal.client  # noqa: SLF001
        send = getattr(client, "send_passive", client.send)
        active = (await send("Target.getTargetInfo", {}))["targetInfo"]
        targets = (await send("Target.getTargets", {}))["targetInfos"]
        if active.get("type") != "page" or not active.get("targetId"):
            return []
    except Exception as exc:
        logger.debug("harness_list_tabs failed: %s", exc)
        return []

    pages = [
        t
        for t in targets
        if t.get("type") == "page" and t.get("browserContextId") == active.get("browserContextId")
    ]
    current_target_id = active["targetId"]
    tabs: list[dict[str, Any]] = []
    for i, t in enumerate(pages):
        url = str(t.get("url", "") or "")
        tabs.append(
            {
                "id": t.get("targetId", i),
                "idx": i,
                "url": url,
                "title": str(t.get("title", "") or ""),
                "active": t.get("targetId") == current_target_id,
            }
        )
    return tabs


async def harness_sessions_payload(
    profile_name: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """Shape expected by the dashboard ``BrowserSessionsResponse`` type."""
    try:
        from moss_browser.tool_interface import _registry
    except ImportError:
        return {"ok": False, "environment": "headless-server", "sessions": []}

    now = int(time.time() * 1000)
    sessions: list[dict[str, Any]] = []
    for profile, sess in list(_registry.items()):
        if profile != profile_name:
            continue
        url = await harness_page_url(sess)
        sessions.append(
            {
                "session_id": profile,
                "profile_name": profile,
                "conversation_id": profile,
                "channel_source": "dashboard",
                "state": "streaming" if url else "idle",
                "control_owner": await control_owner_for(profile, root),
                "current_url": url,
                "created_at": now,
                "last_activity_at": now,
            }
        )
    return {
        "ok": True,
        "environment": "headless-server",
        "sessions": sessions,
    }


@router.get(
    "/browser/harness-sessions",
    summary="List the authenticated user's live browser",
    response_model=HarnessSessionsResponse,
)
async def list_harness_sessions(
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """List the current user's live moss-browser profile."""
    return await harness_sessions_payload(user_browser_profile(user.id), server.paths.root)


@router.post(
    "/browser/sessions/{session_id}/handoff",
    summary="Transfer browser input through the shared operation barrier",
    description=(
        "Compatibility API. The UI uses native conversation Stop and new messages. "
        "Only the authenticated user's profile is selected. Human control drains an "
        "in-flight operation; AI continuation requires fresh observation and refuses held input."
    ),
    response_model=HandoffResponse,
)
async def handoff(
    session_id: str,
    body: HandoffBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Switch control of a browser session between the agent and the user.

    Drain the existing browser operation before granting human input. All
    browser tools use the same barrier. Returning control observes the page;
    every agent must consume its fresh observation before another action.
    """
    profile = user_browser_profile(user.id)
    if session_id not in {"", "auto"} and session_id != profile:
        logger.debug("handoff path %r ignored; using %r", session_id, profile)
    target = body.target
    if target not in ("agent", "user"):
        raise MossworkError(ErrorCode.SLASH_BAD_ARGS, "target must be 'agent' or 'user'")
    controls = await profile_control(profile, server.paths.root)
    try:
        if target == "user":
            await controls.take()
        else:
            await controls.give_back()
    except ControlError as error:
        raise MossworkError(
            ErrorCode.AGENT_BUSY,
            error.code,
            details={"browser_error": error.code},
        ) from error

    payload = await harness_sessions_payload(profile, server.paths.root)
    session = next(
        (s for s in payload.get("sessions", []) if s["session_id"] == profile),
        None,
    )
    if session is None:
        # Session not currently live — return a minimal payload so the UI can
        # still flip its local control-owner state.
        now = int(time.time() * 1000)
        session = {
            "session_id": profile,
            "profile_name": profile,
            "conversation_id": profile,
            "channel_source": "dashboard",
            "state": "idle",
            "control_owner": target,
            "current_url": "",
            "created_at": now,
            "last_activity_at": now,
        }
    return {"ok": True, "session": session}


@router.post(
    "/browser/shutdown",
    summary="Close the authenticated user's managed Chrome",
    description="Preserves cookies on disk and clears pending-input recovery only after Chrome closes successfully.",
    response_model=BrowserShutdownResponse,
)
async def shutdown_browser(
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Terminate the current user's mosswork-managed Chrome. Cookies stay on disk."""
    try:
        from moss_browser.tool_interface import browser_tool
    except ImportError as exc:
        raise MossworkError(
            ErrorCode.INTERNAL_ERROR,
            "moss-browser not installed",
            status=503,
        ) from exc

    hint = user_browser_profile(user.id)

    controls = await profile_control(hint, server.paths.root)
    await controls.take()

    async def close_browser() -> Any:
        result = await browser_tool(action="close_session", profile=hint, kill=True)
        if result.success:
            await controls.browser_closed()
        return result

    result = await controls.core.run(
        hint,
        controls.core.epoch,
        controls.core.target_epoch,
        close_browser,
    )
    if not result.success:
        raise MossworkError(
            ErrorCode.INTERNAL_ERROR,
            result.error or f"failed to stop browser profile {hint!r}",
            status=503,
        )
    return {"ok": True, "profile": hint}
