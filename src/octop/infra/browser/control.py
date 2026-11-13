"""Adapt the shared browser input barrier to authenticated Mosswork profiles.

This module owns browser input coordination, not user/session authorization.
Callers must derive profiles from the authenticated user. The public browser
core owns operation draining, cancellation and control epochs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar, cast

from ai_dev_browser.remote.control import BrowserControl, ControlError
from ai_dev_browser.remote.profile_lease import ProfileLease

from octop.infra.utils.paths import PathLayout

T = TypeVar("T")
_controls: dict[tuple[Path, str], ProfileControl] = {}
_loading: dict[tuple[Path, str], asyncio.Lock] = {}
_TARGET_ACTIONS = {
    "navigate",
    "go_back",
    "go_forward",
    "reload",
    "new_tab",
    "switch_tab",
    "close_tab",
}


async def observe_profile(profile: str) -> dict[str, Any]:
    """Observe the existing page without starting Chrome or changing targets."""
    from moss_browser.tool_interface import _registry  # noqa: PLC0415

    session = _registry.get(profile)
    if session is None:
        return {"running": False}
    result = await session._internal.client.send(  # noqa: SLF001
        "Runtime.evaluate",
        {
            "expression": "(()=>{const el=document.activeElement;"
            "const focused=el&&el.type!=='password'?{tag:el.tagName,type:el.type??'',"
            "value:String(el.isContentEditable?el.innerText:el.value??'').slice(0,8192)}:null;"
            "return JSON.stringify({url:location.href,title:document.title,focused,"
            "text:(document.body?.innerText??'').slice(0,8192)});})()",
            "returnByValue": True,
        },
    )
    if result.get("exceptionDetails"):
        raise ControlError("browser_observation_required")
    return {"running": True, **json.loads(result["result"]["value"])}


class ProfileControl:
    """One shared core barrier for every browser tool and viewer of a profile."""

    def __init__(self, root: Path, profile: str) -> None:
        self.profile = profile
        self.core = BrowserControl()  # type: ignore[no-untyped-call]  # Public SDK constructor.
        self._transfer = asyncio.Lock()
        self._journal_lock = asyncio.Lock()
        self._observed: dict[str, tuple[int, int]] = {}
        self._fresh_observation_required = False
        self._inputs: dict[str, dict[str, dict[str, Any]]] = {}
        self._input_recovery_required = False
        self.directory = root / "browser-control" / profile
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lease = ProfileLease(self.directory)  # type: ignore[no-untyped-call]
        self.journal = self.directory / "control.json"
        try:
            if self.journal.exists():
                saved = json.loads(self.journal.read_text(encoding="utf-8"))
                for field in ("controlEpoch", "targetEpoch"):
                    if type(saved[field]) is not int or saved[field] < 0:
                        raise ControlError("invalid_browser_control_journal")
                if saved.get("controller") not in (None, profile):
                    raise ControlError("invalid_browser_control_journal")
                self.core.epoch = saved["controlEpoch"] + 1
                self.core.target_epoch = saved["targetEpoch"] + 1
                self.core.controller = saved.get("controller")
                pending_input = saved.get("inputPending", True)
                if type(pending_input) is not bool:
                    raise ControlError("invalid_browser_control_journal")
                self._input_recovery_required = pending_input
                self.core.state = "error"
                self.core.error = "browser_restart_requires_observation"
                self._fresh_observation_required = True
            self._write({**self.core.snapshot(), "inputPending": self._input_recovery_required})
        except BaseException:
            self._lease.close()  # type: ignore[no-untyped-call]
            raise

    def _write(self, value: dict[str, Any]) -> None:
        temporary = self.directory / (".control-" + secrets.token_hex(8))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.journal)
        finally:
            temporary.unlink(missing_ok=True)

    async def save(self, value: dict[str, Any] | None = None) -> None:
        try:
            async with self._journal_lock:
                snapshot = {
                    **(value or self.core.snapshot()),
                    # Persist only whether recovery is required, never typed
                    # characters, composition text or password-field content.
                    "inputPending": self._input_recovery_required or any(self._inputs.values()),
                }
                await asyncio.to_thread(self._write, snapshot)
        except BaseException:
            self.core.state = "error"
            self.core.error = "browser_control_not_saved"
            raise

    @property
    def owner(self) -> str:
        return "agent" if self.core.state == "ai" else "user"

    async def take(self) -> dict[str, Any]:
        async with self._transfer:
            # Start the public core's pause before waiting for filesystem I/O.
            # No viewer input is admitted until this transfer is saved.
            transfer = asyncio.create_task(self.core.take(self.profile))
            try:
                await asyncio.sleep(0)
                await self.save()
                result = await transfer
            except BaseException:
                transfer.cancel()
                with contextlib.suppress(BaseException):
                    await transfer
                raise
            self._observed.clear()
            self._fresh_observation_required = True
            await self.save()
            return result

    async def give_back(self, *, expected_epoch: int | None = None) -> dict[str, Any]:
        async with self._transfer:
            if expected_epoch is not None and expected_epoch != self.core.epoch:
                raise ControlError("stale_browser_operation")
            if self._input_recovery_required:
                raise ControlError("browser_input_recovery_required")
            if any(self._inputs.values()):
                raise ControlError("browser_input_in_progress")
            if self.core.state == "error":
                await self.core.take(self.profile)
            await self.save(
                {**self.core.snapshot(), "state": "observing", "controlEpoch": self.core.epoch + 1}
            )
            try:
                result, observation = await self.core.give_back(
                    self.profile,
                    lambda: observe_profile(self.profile),
                )
            finally:
                await self.save()
            self._observed.clear()
            self._fresh_observation_required = True
            return {**result, "observation": observation}

    async def run_ai(
        self,
        actor: str,
        action: str,
        operation: Callable[[], Awaitable[T]],
        *,
        epoch: int | None = None,
        before_effect: Callable[[], Awaitable[None]] | None = None,
    ) -> T:
        """Run one input with the host's current assertion inside the barrier.

        Managed callers supply an assertion derived from trusted execution,
        never from tool arguments. It runs after waiting for earlier input and
        before even observing the page. None is the only successful completion;
        the native service still owns atomic grant and effect fencing.
        """
        epoch = self.core.epoch if epoch is None else epoch
        target = self.core.target_epoch

        async def guarded() -> T:
            if before_effect is not None:
                assertion: Awaitable[object] = before_effect()
                if await assertion is not None:
                    raise ControlError("invalid_browser_authorization")
                # Stop starts pausing immediately, including while a host check
                # awaits I/O. The operation lock alone cannot fence that case.
                if epoch != self.core.epoch or target != self.core.target_epoch:
                    raise ControlError("stale_browser_operation")
                if self.core.state != "ai":
                    raise ControlError("browser_control_held_by_user")
            if self._fresh_observation_required and self._observed.get(actor) != (epoch, target):
                observation = await observe_profile(self.profile)
                self._observed[actor] = (epoch, target)
                raise ControlError(
                    "browser_reobserve_required",
                    details={
                        "observation": observation,
                        "actionExecuted": False,
                    },
                )
            result = await operation()
            if action in _TARGET_ACTIONS:
                self.core.change_target()
                self._fresh_observation_required = True
                self._observed[actor] = (self.core.epoch, self.core.target_epoch)
                await self.save()
            return result

        return cast(T, await self.core.run("ai", epoch, target, guarded))

    async def run_human(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        epoch: int | None = None,
        target: int | None = None,
    ) -> T:
        if self._transfer.locked():
            raise ControlError("browser_control_transfer_in_progress")
        if self._input_recovery_required:
            raise ControlError("browser_input_recovery_required")
        return cast(
            T,
            await self.core.run(
                self.profile,
                self.core.epoch if epoch is None else epoch,
                self.core.target_epoch if target is None else target,
                operation,
            ),
        )

    async def input(
        self,
        viewer: str,
        event: dict[str, Any],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        epoch, target = event.get("controlEpoch"), event.get("targetEpoch")
        if type(epoch) is not int or type(target) is not int:
            raise ControlError("stale_browser_operation")
        kind = event.get("type")
        changing_target = kind in {
            "navigate",
            "goback",
            "goforward",
            "reload",
            "tab_switch",
            "tab_new",
            "tab_close",
            "resize",
        }

        async def guarded() -> T:
            if any(held for owner, held in self._inputs.items() if owner != viewer):
                raise ControlError("browser_input_in_progress")
            if changing_target and any(self._inputs.values()):
                raise ControlError("browser_input_in_progress")
            held = self._inputs.setdefault(viewer, {})
            if kind in {"mousedown", "mouseup"}:
                key = "mouse:" + str(event.get("button") or "left")
            elif kind in {"compositionstart", "compositionend"}:
                key = "composition"
            else:
                key = str(event.get("code") or event.get("key"))
            if kind in {"mousedown", "keydown", "compositionstart"}:
                held[key] = event.copy()
                await self.save()
            result = await operation()
            if kind in {"mouseup", "keyup", "compositionend"}:
                held.pop(key, None)
                await self.save()
            if changing_target:
                self.core.change_target()
                await self.save()
            return result

        return await self.run_human(guarded, epoch=epoch, target=target)

    async def detach(
        self,
        viewer: str,
        release: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        held = self._inputs.get(viewer, {})
        if not held:
            self._inputs.pop(viewer, None)
            return

        async def cleanup() -> None:
            for key, event in list(held.items()):
                kind = (
                    "compositionend"
                    if key == "composition"
                    else ("mouseup" if key.startswith("mouse:") else "keyup")
                )
                await release({**event, "type": kind})
                held.pop(key, None)
            self._inputs.pop(viewer, None)
            await self.save()

        try:
            await self.run_human(cleanup)
        except BaseException:
            self.core.state = "error"
            self.core.error = "browser_input_release_failed"
            await self.save()
            raise

    async def browser_closed(self) -> None:
        """A successful native browser shutdown clears unknown held input.

        A host restart cannot safely guess missing key-up events. Keep both
        humans and AI fenced until the user explicitly closes that browser.
        """
        self._inputs.clear()
        self._input_recovery_required = False
        self.core.change_target()
        await self.save()

    def close(self) -> None:
        self._lease.close()  # type: ignore[no-untyped-call]


async def profile_control(profile: str, root: Path | None = None) -> ProfileControl:
    if not re.fullmatch(r"user-[1-9][0-9]*", profile):
        raise ControlError("invalid_browser_profile")
    root = Path(os.path.abspath(root or PathLayout.from_env().root))
    key = root, profile
    async with _loading.setdefault(key, asyncio.Lock()):
        if key not in _controls:
            _controls[key] = await asyncio.to_thread(ProfileControl, root, profile)
        return _controls[key]


async def close_profile_controls(root: Path) -> None:
    root = Path(os.path.abspath(root))
    for key in [key for key in _controls if key[0] == root]:
        await asyncio.to_thread(_controls.pop(key).close)
        _loading.pop(key, None)


async def pause_user_browser(user_id: int, root: Path) -> None:
    """Pause a loaded profile from the host's authenticated native Stop path.

    Do not create a browser or profile merely because a conversation is stopped.
    """
    key = Path(os.path.abspath(root)), f"user-{user_id}"
    controls = _controls.get(key)
    if controls is not None:
        await controls.take()


async def resume_user_browser(user_id: int, root: Path, admitted_epoch: int | None) -> int:
    """Use a host-stamped user turn, never a timeout or a queued old instruction.

    Old turns retain their old capability: they may converse, but their browser
    tools remain fenced. This function does not admit or cancel agent runs.
    """
    if admitted_epoch is None:
        return 0
    if type(admitted_epoch) is not int or admitted_epoch < 0:
        raise ControlError("invalid_browser_control_epoch")
    controls = await profile_control(f"user-{user_id}", root)
    if admitted_epoch != controls.core.epoch:
        return admitted_epoch
    if controls.core.state != "ai":
        result = await controls.give_back(expected_epoch=admitted_epoch)
        return cast(int, result["controlEpoch"])
    return admitted_epoch
