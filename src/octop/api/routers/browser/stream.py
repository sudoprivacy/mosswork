"""WebSocket browser screencast — attaches to moss-browser sessions.

Wire protocol matches the dashboard ``useBrowserStream`` hook (screencast)
and ``useBrowserSessionState`` (listen-only status):

Client → Server::

  {"type": "start", "url": "", "width": 1280, "height": 800,
   "reuse_session": true, "session_id": "<profile>"}
  {"type": "navigate", "url": "https://..."}
  {"type": "click", "x": 100, "y": 200}
  {"type": "mousedown", "x": 100, "y": 200, "button": "left", "buttons": 1}
  {"type": "mousemove", "x": 150, "y": 250, "button": "left", "buttons": 1}
  {"type": "mousemove", "x": 160, "y": 260, "button": "none", "buttons": 0}
  {"type": "mouseup", "x": 150, "y": 250, "button": "left", "buttons": 0}
  {"type": "type", "text": "中文 / pasted text"}
  {"type": "keydown", "key": "Backspace", "code": "Backspace", "keyCode": 8,
   "ctrlKey": false, "altKey": false, "metaKey": false, "shiftKey": false}
  {"type": "keyup", "key": "Backspace", "code": "Backspace", "keyCode": 8}
  {"type": "stop"}

Server → Client::

  {"type": "status", "status": "streaming"}
  {"type": "frame", "data": "<base64 jpeg>"}
  {"type": "tabs", "tabs": [{id, url, title, active}]}
  {"type": "session_update", "session_id": "...", "current_url": "...", ...}
  {"type": "error", "message": "..."}

Auth: ``?token=<JWT>`` query param (browsers cannot set Authorization on WS).
Listen-only (``?listen_only=1``): still requires ``start``, but never launches
Chrome — attaches to an existing harness session or pushes idle updates.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import Awaitable
from functools import partial
from pathlib import Path
from typing import Any

from ai_dev_browser.remote.control import ControlError
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from octop.api.deps import resolve_user_from_token
from octop.api.routers.browser.harness import (
    harness_list_tabs,
    harness_page_url,
    harness_viewport,
    resolve_harness_session,
)
from octop.infra.browser.control import profile_control
from octop.infra.utils.browser_media import user_browser_profile

logger = logging.getLogger(__name__)

router = APIRouter()

_FRAME_INTERVAL_S = 0.25  # ~4 fps


def _normalize_nav_url(raw: str) -> str:
    """Prefix a host with ``https://`` unless it already has an http(s) scheme.

    Matches :func:`octop.infra.utils.url.normalize_nav_url`, including the
    case-insensitive scheme check: ``HTTPS://host`` must not become
    ``https://HTTPS://host``.
    """
    t = raw.strip()
    if not t:
        return ""
    if t.lower().startswith(("http://", "https://")):
        return t
    return f"https://{t}"


async def _capture_jpeg(sess: Any) -> str | None:
    try:
        # Check if internal session is fully connected to prevent race conditions during tab switching
        if not getattr(sess._internal, "_connected", False):  # noqa: SLF001
            return None

        # Wrap in a timeout to prevent hanging if the CDP client gets into an inconsistent state during reconnect
        result = await asyncio.wait_for(
            sess._internal.client.send(  # noqa: SLF001
                "Page.captureScreenshot",
                {"format": "jpeg", "quality": 80},
            ),
            timeout=1.5,
        )
        data = result.get("data")
        return str(data) if data else None
    except Exception as exc:
        logger.debug("capture jpeg failed: %s", exc)
        return None


async def _send_json(ws: WebSocket, payload: dict[str, Any]) -> None:
    if ws.application_state == WebSocketState.CONNECTED:
        await ws.send_text(json.dumps(payload))


async def _send_session_snapshot(
    send_json: Any,
    profile: str,
    *,
    sess: Any | None,
    root: Path | None = None,
) -> None:
    """Push one session_update (+ tabs) frame for *profile*."""
    controls = await profile_control(profile, root)
    metadata = {
        "control_owner": controls.owner,
        "controlEpoch": controls.core.epoch,
        "targetEpoch": controls.core.target_epoch,
    }
    if sess is None:
        await send_json(
            {
                "type": "session_update",
                "session_id": profile,
                "conversation_id": profile,
                "channel_source": "dashboard",
                "state": "idle",
                **metadata,
                "current_url": "",
            },
        )
        await send_json({"type": "tabs", "tabs": []})
        return

    epoch, target = controls.core.epoch, controls.core.target_epoch

    async def observe() -> tuple[str, list[dict[str, Any]]]:
        # Tab switching replaces the underlying CDP target. Metadata reads must
        # share the operation barrier with switching and screenshot capture.
        return await harness_page_url(sess), await harness_list_tabs(sess)

    try:
        url, tabs = await controls.core.run("ai", epoch, target, observe, observation=True)
    except ControlError:
        return
    if (epoch, target) != (controls.core.epoch, controls.core.target_epoch):
        return
    await send_json(
        {
            "type": "session_update",
            "session_id": profile,
            "conversation_id": profile,
            "channel_source": "dashboard",
            "state": "streaming" if url else "idle",
            **metadata,
            "current_url": url,
        },
    )
    await send_json({"type": "tabs", "tabs": tabs})


async def _report_stream_failure(
    operation: Awaitable[None], send_json: Any, is_connected: Any
) -> None:
    """Report background transport failure instead of leaving an inert viewer."""
    try:
        await operation
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.exception("browser background stream failed")
        if is_connected():
            await send_json({"type": "error", "message": str(exc)})
            await send_json({"type": "status", "status": "error"})


async def _stream_loop(
    send_json: Any,
    is_connected: Any,
    sess: Any,
    profile: str,
    *,
    listen_only: bool,
    root: Path | None = None,
) -> None:
    await send_json({"type": "status", "status": "browser_started"})
    await send_json({"type": "status", "status": "streaming"})

    while is_connected():
        await _send_session_snapshot(send_json, profile, sess=sess, root=root)

        if not listen_only:
            controls = await profile_control(profile, root)
            epoch, target = controls.core.epoch, controls.core.target_epoch
            try:
                frame = await controls.core.run(
                    "ai",
                    epoch,
                    target,
                    lambda: _capture_jpeg(sess),
                    observation=True,
                )
            except ControlError:
                await asyncio.sleep(_FRAME_INTERVAL_S)
                continue
            if frame and (epoch, target) == (controls.core.epoch, controls.core.target_epoch):
                await send_json(
                    {"type": "frame", "data": frame, "controlEpoch": epoch, "targetEpoch": target}
                )

        await asyncio.sleep(_FRAME_INTERVAL_S)


async def _listen_state_loop(
    send_json: Any,
    is_connected: Any,
    profile: str,
    root: Path | None = None,
) -> None:
    """Push session_update events without launching Chrome or capturing frames.

    Re-resolves the harness registry each tick so a browser started later by
    the agent becomes visible without reconnecting. Idle (no session) ticks
    use a longer interval to avoid a request/render storm on the dashboard.
    """
    await send_json({"type": "status", "status": "streaming"})

    while is_connected():
        sess = await resolve_harness_session(profile, create=False)
        await _send_session_snapshot(send_json, profile, sess=sess, root=root)
        await asyncio.sleep(_FRAME_INTERVAL_S if sess is not None else 2.0)


_CDP_BUTTON_MASK = {"left": 1, "right": 2, "middle": 4, "none": 0}


def _cdp_button(raw: object) -> str:
    name = str(raw or "left").lower()
    return name if name in _CDP_BUTTON_MASK else "left"


def _cdp_buttons(msg: dict[str, Any], *, button: str, event: str) -> int:
    if "buttons" in msg and msg["buttons"] is not None:
        try:
            return max(0, int(msg["buttons"]))
        except (TypeError, ValueError):
            pass
    if event == "mouseup" or button == "none":
        return 0
    return _CDP_BUTTON_MASK.get(button, 0)


async def _dispatch_mouse(
    sess: Any,
    *,
    event_type: str,
    x: int,
    y: int,
    button: str = "none",
    buttons: int = 0,
    click_count: int = 0,
) -> None:
    params: dict[str, Any] = {
        "type": event_type,
        "x": x,
        "y": y,
        "button": button,
        "buttons": buttons,
    }
    if click_count:
        params["clickCount"] = click_count
    await sess._internal.client.send("Input.dispatchMouseEvent", params)  # noqa: SLF001


def _cdp_click_count(msg: dict[str, Any]) -> int:
    raw = msg.get("clickCount", msg.get("click_count", 1))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


_KEY_CODES = {
    "Backspace": 8,
    "Tab": 9,
    "Enter": 13,
    "Shift": 16,
    "Control": 17,
    "Alt": 18,
    "CapsLock": 20,
    "Escape": 27,
    " ": 32,
    "PageUp": 33,
    "PageDown": 34,
    "End": 35,
    "Home": 36,
    "ArrowLeft": 37,
    "ArrowUp": 38,
    "ArrowRight": 39,
    "ArrowDown": 40,
    "Insert": 45,
    "Delete": 46,
    "Meta": 91,
}


def _key_commands(msg: dict[str, Any], key: str) -> list[str]:
    """Explicit editor commands also work when client/server OSes differ."""
    shift = bool(msg.get("shiftKey"))
    if (msg.get("ctrlKey") or msg.get("metaKey")) and not msg.get("altKey"):
        command = {
            "a": "selectAll",
            "c": "copy",
            "x": "cut",
            "z": "redo" if shift else "undo",
            "y": "redo",
        }.get(key.lower())
        return [command] if command else []
    if msg.get("altKey") or msg.get("ctrlKey") or msg.get("metaKey"):
        return []
    command = {
        "Backspace": "deleteBackward",
        "Delete": "deleteForward",
        "ArrowLeft": "moveLeft",
        "ArrowRight": "moveRight",
        "ArrowUp": "moveUp",
        "ArrowDown": "moveDown",
        "Home": "moveToBeginningOfLine",
        "End": "moveToEndOfLine",
        "PageUp": "movePageUp",
        "PageDown": "movePageDown",
    }.get(key)
    if command and shift and command.startswith("move"):
        command += "AndModifySelection"
    return [command] if command else []


async def _dispatch_key(sess: Any, msg: dict[str, Any]) -> None:
    key = str(msg.get("key") or "")
    if not key or key in {"Process", "Dead", "Unidentified"}:
        return
    key_up = msg.get("type") == "keyup"
    modifiers = sum(
        mask
        for name, mask in (("altKey", 1), ("ctrlKey", 2), ("metaKey", 4), ("shiftKey", 8))
        if msg.get(name)
    )
    key_code = int(msg.get("keyCode") or _KEY_CODES.get(key, 0))
    if not key_code and len(key) == 1 and key.isascii() and key.isalnum():
        key_code = ord(key.upper())
    if not key_code and key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        key_code = 111 + int(key[1:])
    params: dict[str, Any] = {
        "type": "keyUp" if key_up else "rawKeyDown",
        "key": key,
        "code": str(msg.get("code") or key),
        "windowsVirtualKeyCode": key_code,
        "modifiers": modifiers,
        "location": int(msg.get("location") or 0),
    }
    if not key_up:
        params["autoRepeat"] = bool(msg.get("repeat"))
        commands = _key_commands(msg, key)
        if commands:
            params["commands"] = commands
        if key == "Enter" and not modifiers & 7:
            params.update(type="keyDown", text="\r", unmodifiedText="\r")
    await sess._internal.client.send("Input.dispatchKeyEvent", params)  # noqa: SLF001
    # Older dashboards send a complete press as keydown, without code/keyup.
    if not key_up and "code" not in msg:
        await sess._internal.client.send(  # noqa: SLF001
            "Input.dispatchKeyEvent",
            {
                k: v
                for k, v in params.items()
                if k not in {"text", "unmodifiedText", "commands", "autoRepeat"}
            }
            | {"type": "keyUp"},
        )


async def _browser_action(operation: Awaitable[Any], action: str) -> None:
    result = await operation
    if getattr(result, "success", True) is False:
        raise ControlError("browser_action_failed", details={"action": action})


async def _handle_client_event(sess: Any, msg: dict[str, Any]) -> None:
    t = msg.get("type")
    if t == "navigate":
        url = _normalize_nav_url(str(msg.get("url") or ""))
        if url:
            await _browser_action(sess.navigate(url), "navigate")
    elif t == "goback":
        await _browser_action(sess.go_back(), "goback")
    elif t == "goforward":
        await _browser_action(sess.go_forward(), "goforward")
    elif t == "reload":
        await _browser_action(sess.reload(), "reload")
    elif t == "click":
        await _browser_action(
            sess.click(x=int(msg.get("x") or 0), y=int(msg.get("y") or 0)), "click"
        )
    elif t == "dblclick":
        x, y = int(msg.get("x") or 0), int(msg.get("y") or 0)
        await _browser_action(sess.click(x=x, y=y), "dblclick")
        await _browser_action(sess.click(x=x, y=y), "dblclick")
    elif t in ("mousedown", "mouseup", "mousemove"):
        x, y = int(msg.get("x") or 0), int(msg.get("y") or 0)
        button = _cdp_button(msg.get("button"))
        buttons = _cdp_buttons(msg, button=button, event=str(t))
        click_count = _cdp_click_count(msg)
        if t == "mousedown":
            await _dispatch_mouse(
                sess,
                event_type="mousePressed",
                x=x,
                y=y,
                button=button if button != "none" else "left",
                buttons=buttons or _CDP_BUTTON_MASK.get(button, 1),
                click_count=click_count,
            )
        elif t == "mouseup":
            await _dispatch_mouse(
                sess,
                event_type="mouseReleased",
                x=x,
                y=y,
                button=button if button != "none" else "left",
                buttons=0,
                click_count=click_count,
            )
        else:
            await _dispatch_mouse(
                sess,
                event_type="mouseMoved",
                x=x,
                y=y,
                button=button,
                buttons=buttons,
            )
    elif t == "scroll":
        delta_x = float(msg.get("deltaX") or msg.get("delta_x") or 0)
        delta_y = float(msg.get("deltaY") or msg.get("delta_y") or 0)
        x = int(msg.get("x") or 0)
        y = int(msg.get("y") or 0)
        if abs(delta_x) > 0.5 or abs(delta_y) > 0.5:
            with contextlib.suppress(Exception):
                await _dispatch_mouse(sess, event_type="mouseMoved", x=x, y=y)
                await sess._internal.client.send(  # noqa: SLF001
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": x,
                        "y": y,
                        "deltaX": delta_x,
                        "deltaY": delta_y,
                    },
                )
        else:
            direction = "down" if delta_y > 0 else "up"
            amount = max(int(abs(delta_y)), 50)
            await _browser_action(sess.scroll(direction=direction, amount=amount), "scroll")
    elif t == "type":
        text = str(msg.get("text") or "")
        if text:
            await sess._internal.client.send("Input.insertText", {"text": text})  # noqa: SLF001
    elif t in ("keydown", "keyup"):
        await _dispatch_key(sess, msg)
    elif t == "tab_switch":
        tab_id = msg.get("tab_id")
        if tab_id is not None:
            await _browser_action(sess.switch_tab(str(tab_id)), "tab_switch")
    elif t == "tab_close":
        tab_id = msg.get("tab_id")
        if tab_id is not None:
            await _browser_action(sess.close_tab(str(tab_id)), "tab_close")
    elif t == "tab_new":
        await _browser_action(sess.new_tab(), "tab_new")
    elif t == "resize":
        w = int(msg.get("width") or 0)
        h = int(msg.get("height") or 0)
        if w > 0 and h > 0:
            with contextlib.suppress(Exception):
                await sess._internal.client.send(  # noqa: SLF001
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": w,
                        "height": h,
                        "deviceScaleFactor": 1,
                        "mobile": False,
                    },
                )


@router.websocket("/browser-stream/ws")
async def browser_stream_ws(
    websocket: WebSocket,
    token: str | None = Query(default=None),
    listen_only: int = Query(default=0),
    width: int = Query(default=1280),
    height: int = Query(default=800),
) -> None:
    server = websocket.app.state.mosswork_server
    if not token:
        await websocket.close(code=4001, reason="missing token")
        return
    try:
        user = resolve_user_from_token(server, token)
    except Exception as exc:
        await websocket.close(code=4001, reason=f"auth failed: {exc}")
        return

    await websocket.accept()

    async def send_json(payload: dict[str, Any]) -> None:
        await _send_json(websocket, payload)

    def is_connected() -> bool:
        return websocket.application_state == WebSocketState.CONNECTED

    async def receive_text() -> str:
        return await websocket.receive_text()

    try:
        await run_browser_stream_session(
            send_json=send_json,
            is_connected=is_connected,
            receive_text=receive_text,
            user_id=int(user.id),
            listen_only=bool(listen_only),
            default_width=width,
            default_height=height,
            root=server.paths.root,
        )
    except WebSocketDisconnect:
        pass
    finally:
        if websocket.application_state == WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()


async def run_browser_stream_session(
    *,
    send_json: Any,
    is_connected: Any,
    receive_text: Any,
    user_id: int,
    listen_only: bool = False,
    default_width: int = 1280,
    default_height: int = 800,
    start_msg: dict[str, Any] | None = None,
    root: Path | None = None,
) -> None:
    """Run one screencast / listen-only session over arbitrary send/receive hooks.

    Used by the local dashboard WebSocket and by Bridge peer relay so both share
    the same Chrome / harness lifecycle.
    """
    sess: Any | None = None
    profile = user_browser_profile(user_id)
    stream_task: asyncio.Task[None] | None = None
    controls = await profile_control(profile, root)
    viewer = secrets.token_urlsafe(16)

    try:
        if start_msg is None:
            raw = await asyncio.wait_for(receive_text(), timeout=15.0)
            start_msg = json.loads(raw)
        if not isinstance(start_msg, dict) or start_msg.get("type") != "start":
            await send_json({"type": "error", "message": "expected start message"})
            return

        if listen_only:
            stream_task = asyncio.create_task(
                _report_stream_failure(
                    _listen_state_loop(send_json, is_connected, profile, root),
                    send_json,
                    is_connected,
                )
            )
        else:
            # Starting the first browser is a user action. Merely attaching to
            # an AI's existing browser stays read-only until native Stop.
            existing = await resolve_harness_session(profile, create=False)
            if existing is None:
                await controls.take()
            if existing is not None:
                sess = existing
            elif controls.owner == "user":
                sess = await controls.run_human(lambda: resolve_harness_session(profile))
            else:
                sess = await controls.core.run(
                    "ai",
                    controls.core.epoch,
                    controls.core.target_epoch,
                    lambda: resolve_harness_session(profile),
                )
            assert sess is not None  # create=True always returns or raises

            start_url = _normalize_nav_url(str(start_msg.get("url") or ""))
            if start_url and start_url not in {"about:blank"}:
                try:
                    event = {
                        "type": "navigate",
                        "url": start_url,
                        "controlEpoch": controls.core.epoch,
                        "targetEpoch": controls.core.target_epoch,
                    }
                    await controls.input(viewer, event, partial(_handle_client_event, sess, event))
                except Exception as exc:
                    logger.warning("initial navigate to %s failed: %s", start_url, exc)
                    await send_json(
                        {
                            "type": "error",
                            "message": f"导航到 {start_url} 失败：{exc}",
                        },
                    )

            vw = int(start_msg.get("width") or default_width)
            vh = int(start_msg.get("height") or default_height)
            if vw > 0 and vh > 0 and controls.owner == "user":
                with contextlib.suppress(Exception):
                    actual_size = await controls.core.run(
                        "ai",
                        controls.core.epoch,
                        controls.core.target_epoch,
                        lambda: harness_viewport(sess),
                        observation=True,
                    )
                    # Attaching another viewer at the existing size is a read.
                    # A redundant resize used to invalidate the first viewer's
                    # displayed frame and reject its next tab/input operation.
                    if actual_size != (vw, vh) and (actual_size is not None or existing is None):
                        event = {
                            "type": "resize",
                            "width": vw,
                            "height": vh,
                            "controlEpoch": controls.core.epoch,
                            "targetEpoch": controls.core.target_epoch,
                        }
                        await controls.input(
                            viewer, event, partial(_handle_client_event, sess, event)
                        )

            stream_task = asyncio.create_task(
                _report_stream_failure(
                    _stream_loop(
                        send_json,
                        is_connected,
                        sess,
                        profile,
                        listen_only=False,
                        root=root,
                    ),
                    send_json,
                    is_connected,
                )
            )

        while is_connected():
            try:
                raw = await receive_text()
            except WebSocketDisconnect:
                break
            try:
                msg = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "stop":
                break
            if sess is not None:
                try:
                    await controls.input(viewer, msg, partial(_handle_client_event, sess, msg))
                except ControlError as error:
                    await send_json(
                        {
                            "type": "error",
                            "message": error.code,
                            "details": error.details or {},
                            "recoverable": True,
                        }
                    )
                    await _send_session_snapshot(send_json, profile, sess=sess, root=root)
    except TimeoutError:
        await send_json({"type": "error", "message": "timed out waiting for start"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("browser stream failed")
        if is_connected():
            await send_json({"type": "error", "message": str(exc)})
            await send_json({"type": "status", "status": "error"})
    finally:
        try:
            if sess is not None:
                await controls.detach(viewer, lambda event: _handle_client_event(sess, event))
        finally:
            if stream_task is not None:
                stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_task
