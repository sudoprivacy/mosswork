"""Supervise local tool processes without loading model or harness integrations."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import ntpath
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from octop.infra.utils.asyncio import offload

_TOOL_CALL_ID: ContextVar[str] = ContextVar("company_tool_call_id", default="")


class ProcessOutcome(Protocol):
    @property
    def output(self) -> str: ...

    @property
    def exit_code(self) -> int | None: ...

    @property
    def truncated(self) -> bool: ...


@dataclass(frozen=True)
class ProcessResult:
    output: str
    exit_code: int | None
    truncated: bool = False


def windows_python_command(command: str) -> list[str] | None:
    """Keep a quoted Python -c program out of cmd's second quoting/AutoRun pass."""
    match = re.fullmatch(r'\s*"([^"\r\n]+)"\s+(?:-u\s+)?-c\s+"([^"\r\n]*)"\s*', command)
    if not match:
        return None
    executable, code = match.groups()
    if not ntpath.isabs(executable) or ntpath.basename(executable).lower() not in {
        "python.exe",
        "python3.exe",
    }:
        return None
    return [executable, "-X", "utf8", "-u", "-c", code]


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as out:
        os.chmod(temporary, 0o600)
        json.dump(value, out, ensure_ascii=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


class ProjectProcesses:
    """Own the process handles, bounded output, and original operation receipts."""

    def __init__(
        self, project: Path, state: Path, emit: Callable[..., None], *, tool_path: str
    ) -> None:
        self.project, self.state, self.emit = project, state, emit
        self.tool_path = tool_path
        self._default_timeout = 900
        self.packages = os.environ.get("MOSSWORK_COMPANY_PACKAGES", "")
        self.processes: set[asyncio.subprocess.Process] = set()

    async def stop_process(self, process: asyncio.subprocess.Process) -> None:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        await process.wait()

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ProcessOutcome:
        effective_timeout = min(timeout if timeout is not None else self._default_timeout, 3600)
        if effective_timeout <= 0:
            return ProcessResult(output="timeout must be positive", exit_code=1)
        # No provider keys, company tokens or inherited cloud credentials reach children.
        allowed = (
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
            "LANG",
            "LC_ALL",
            "HOME",
            "USERPROFILE",
        )
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        env.update(
            PATH=self.tool_path,
            PYTHONNOUSERSITE="1",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
        )
        if self.packages:
            env["PYTHONPATH"] = self.packages
        kwargs: dict[str, Any] = {
            "cwd": self.project,
            "env": env,
            # The worker stdin carries private JSONL requests. Tools must never
            # read that channel or wait for interactive input on it.
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        else:
            kwargs["start_new_session"] = True
        python_args = windows_python_command(command) if os.name == "nt" else None
        if python_args:
            process = await asyncio.create_subprocess_exec(*python_args, **kwargs)
        else:
            process = await asyncio.create_subprocess_shell(command, **kwargs)
        self.processes.add(process)
        process_dir = self.state / "processes" / str(process.pid)
        tails: dict[str, bytes] = {}
        truncated = False
        readers: asyncio.Future[Any] | None = None

        async def drain(name: str, stream: Any) -> None:
            nonlocal truncated
            tail = b""
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            preview = ""
            emitted_at = 0.0

            def emit_preview() -> None:
                nonlocal preview, emitted_at
                if preview:
                    self.emit(
                        {
                            "event": "process_output",
                            "pid": process.pid,
                            "stream": name,
                            "tool_call_id": _TOOL_CALL_ID.get(),
                            "content": preview,
                        }
                    )
                    preview = ""
                    emitted_at = time.monotonic()

            with (process_dir / (name + ".log")).open("wb") as out:
                logged = 0
                while chunk := await stream.read(16384):
                    # Disk logs are bounded too; keep draining to avoid deadlock.
                    if logged < 8 * 1024 * 1024:
                        out.write(chunk)
                        logged += len(chunk)
                    tail += chunk
                    if len(tail) > 131072:
                        tail = tail[-131072:]
                        truncated = True
                    tails[name] = tail
                    preview = (preview + decoder.decode(chunk))[-16384:]
                    if time.monotonic() - emitted_at >= 0.15:
                        emit_preview()
                tails[name] = tail
                preview = (preview + decoder.decode(b"", final=True))[-16384:]
                emit_preview()

        status = "completed"

        async def wait_output() -> None:
            await process.wait()
            assert readers is not None
            await asyncio.shield(readers)

        try:
            await offload(process_dir.mkdir, parents=True, exist_ok=True)
            self.emit(
                {
                    "event": "process",
                    "pid": process.pid,
                    "status": "running",
                    "cwd": str(self.project),
                }
            )
            readers = asyncio.gather(
                drain("stdout", process.stdout), drain("stderr", process.stderr)
            )
            await asyncio.wait_for(wait_output(), timeout=effective_timeout)
            if process.returncode:
                status = "failed"
            return ProcessResult(
                output="stdout:\n"
                + tails["stdout"].decode("utf-8", "replace").replace("\r\n", "\n")
                + "\nstderr:\n"
                + tails["stderr"].decode("utf-8", "replace").replace("\r\n", "\n"),
                exit_code=process.returncode,
                truncated=truncated,
            )
        except BaseException as exc:
            status = (
                "timed_out"
                if isinstance(exc, TimeoutError)
                else "cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else "failed"
            )
            cleanup_failed = False
            try:
                await asyncio.wait_for(self.stop_process(process), timeout=5)
            except Exception:
                cleanup_failed = True
                status = "unknown"
            if readers is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(readers), timeout=1)
                except TimeoutError:
                    # A descendant can retain the pipe even after cmd has exited.
                    # Closing a tool must not wait indefinitely for that EOF.
                    readers.cancel()
                    await asyncio.gather(readers, return_exceptions=True)
            if isinstance(exc, TimeoutError):
                return ProcessResult(
                    output="stdout:\n"
                    + tails.get("stdout", b"").decode("utf-8", "replace").replace("\r\n", "\n")
                    + "\nstderr:\n"
                    + tails.get("stderr", b"").decode("utf-8", "replace").replace("\r\n", "\n")
                    + f"\nprocess timed out after {effective_timeout} seconds; inspect output files before retry"
                    + ("; process termination could not be confirmed" if cleanup_failed else ""),
                    exit_code=124,
                    truncated=truncated,
                )
            raise
        finally:
            # Kill descendants even if a shell daemonized them and exited first.
            if sys.platform != "win32":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            self.processes.discard(process)
            await offload(
                atomic_json,
                process_dir / "status.json",
                {
                    "pid": process.pid,
                    "status": status,
                    "exit_code": process.returncode,
                    "cwd": str(self.project),
                },
            )
            self.emit(
                {
                    "event": "process",
                    "pid": process.pid,
                    "status": status,
                    "exit_code": process.returncode,
                }
            )

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(asyncio.wait_for(self.stop_process(p), timeout=5) for p in list(self.processes)),
            return_exceptions=True,
        )
