"""Share source-bound testmon history between full validation and the commit hook."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

GIT_CONTEXT = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX"}
CACHE_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
VOLATILE_ENV = GIT_CONTEXT | {
    "_",
    "PWD",
    "OLDPWD",
    "SHLVL",
    "MAKEFLAGS",
    "MFLAGS",
    "MAKELEVEL",
    "PYTEST_CURRENT_TEST",
    "PYTEST_ADDOPTS",
    "UV_RUN_RECURSION_DEPTH",
    "CODEX_TOOL_CALL_ID",
    "MOSSWORK_TEST_REQUIRE_REUSE",
}
VALIDATION_STORAGE = {
    "tools/test_gate.py": "mosswork-test-gate",
    "tools/frontend_gate.py": "mosswork-frontend-gate",
    "tools/precommit.py": "mosswork-tools-gate",
}


def git_launch_context(
    root: Path, arguments: list[str], options: dict[str, Any], *, entry: str = "tools/test_gate.py"
) -> int | None:
    """Use the same Git shell for Make and commits, preserving Git policy inputs."""
    frontend = entry == "tools/frontend_gate.py"
    tools = entry == "tools/precommit.py"
    if entry not in VALIDATION_STORAGE:
        raise ValueError("Unknown validation entry")
    hook = root / ".githooks/test-gate"
    if not hook.is_file():
        return None
    root = root.resolve()
    try:
        top = Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        if top != root:
            return None
        before = inputs(root)
        storage = (
            Path(git(root, "rev-parse", "--absolute-git-dir").decode().strip())
            / VALIDATION_STORAGE[entry]
        )
        storage.mkdir(exist_ok=True)
    except (OSError, ValueError, subprocess.CalledProcessError):
        # Source archives and unrecordable inputs retain the original full fallback.
        return None
    environment = {key: value for key, value in os.environ.items() if key not in GIT_CONTEXT}
    # Commit metadata belongs to the parent Git operation, not product tests.
    for name in ("GIT_AUTHOR_DATE", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_NAME", "GIT_EDITOR"):
        environment.pop(name, None)
    current = subprocess.run(
        ["git", "config", "--path", "--get", "core.hooksPath"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    argv = ["git"]
    if (root / current.stdout.strip()).resolve() != hook.parent.resolve():
        argv += ["-c", "core.hooksPath=.githooks"]
    help_result = subprocess.run(
        ["git", "hook", "run", "-h"],
        cwd=root,
        env=environment,
        capture_output=True,
        check=False,
    )
    argv += ["hook", "run"]
    # New Git versions require explicit opt-in for repository-specific hook names.
    if b"--allow-unknown-hook-name" in help_result.stdout + help_result.stderr:
        argv += ["--allow-unknown-hook-name"]
    argv += [
        "test-gate",
        "--",
        *(["uv", "run", "--frozen", "python"] if tools else [sys.executable]),
        str(root / entry),
        "--git-context",
        "--launch-id",
        (launch_id := uuid.uuid4().hex),
        *arguments,
    ]
    marker = storage / ("launch-" + launch_id + ".json")
    try:
        result = subprocess.run(argv, cwd=root, env=environment, check=False)
        if result.returncode:
            return result.returncode
        try:
            completion = (
                json.loads(marker.read_text(encoding="utf-8")) if not marker.is_symlink() else {}
            )
        except (OSError, ValueError):
            completion = {}
        if completion != {"launch_id": launch_id, "root": str(root), "options": options}:
            raise ValueError(
                "Git shell did not complete the requested actual test gate. "
                "Restore .githooks/test-gate and rerun make all; a zero shell exit cannot replace validation."
            )
        after = inputs(root)
        if frontend:
            before = {
                name: value
                for name, value in before.items()
                if not name.startswith("src/octop/dashboard/")
            }
            after = {
                name: value
                for name, value in after.items()
                if not name.startswith("src/octop/dashboard/")
            }
        if after != before:
            raise ValueError(
                "Source changed across the Git shell; preserve the output and rerun the frozen gate"
            )
        return 0
    finally:
        marker.unlink(missing_ok=True)


def complete_git_launch(
    root: Path, launch_id: str, options: dict[str, Any], *, entry: str = "tools/test_gate.py"
) -> None:
    if entry not in VALIDATION_STORAGE or re.fullmatch(r"[a-f0-9]{32}", launch_id) is None:
        raise ValueError("Invalid validation launch completion")
    root = root.resolve()
    storage = (
        Path(git(root, "rev-parse", "--absolute-git-dir").decode().strip())
        / VALIDATION_STORAGE[entry]
    )
    with (storage / ("launch-" + launch_id + ".json")).open("x", encoding="utf-8") as marker:
        json.dump({"launch_id": launch_id, "root": str(root), "options": options}, marker)


def normalize_launch_environment() -> None:
    """Make transport variables and repeated Git PATH prefixes are not test inputs."""
    for name in ("RUN", "TEST_PYTHON", "PYTEST_JOBS"):
        os.environ.pop(name, None)
    # This one per-call routing option starts the shell above. Do not propagate
    # it into private Git fixtures or confuse it with an application policy input.
    parameters = os.environ.get("GIT_CONFIG_PARAMETERS")
    if parameters is not None:
        try:
            values = shlex.split(parameters)
            if values and all(value == "core.hooksPath=.githooks" for value in values):
                os.environ.pop("GIT_CONFIG_PARAMETERS")
        except ValueError:
            pass  # Preserve malformed/other Git policy inputs and their refusal.
    if sys.platform != "win32":
        # uv may insert the interpreter directory between nested Git prefixes.
        # Remove repeated Git entries only inside that leading transport block.
        prefix = os.environ.get("GIT_EXEC_PATH")
        entries = os.environ.get("PATH", "").split(os.pathsep)
        if prefix and Path(prefix).is_dir():
            transport = {prefix, str(Path(sys.executable).parent)}
            leading, seen_git = True, False
            normalized = []
            for entry in entries:
                if leading and entry in transport:
                    if entry == prefix:
                        if seen_git:
                            continue
                        seen_git = True
                else:
                    leading = False
                normalized.append(entry)
            os.environ["PATH"] = os.pathsep.join(normalized)
        return
    # Git for Windows prepends its own directories again in nested hooks. Keep
    # one consistent order for that installation and retain every other PATH entry.
    executable_path = Path(os.environ.get("GIT_EXEC_PATH", ""))
    if len(executable_path.parents) < 3:
        return
    git_root = executable_path.parents[2]
    directories = [
        executable_path,
        executable_path.parents[1] / "bin",
        git_root / "usr/bin",
        git_root / "bin",
    ]
    if not all(path.is_dir() for path in directories) or not (git_root / "bin/git.exe").is_file():
        return
    if not os.environ.get("EXEPATH"):
        os.environ["EXEPATH"] = str(git_root / "bin")
    prefix = [os.path.normcase(os.path.normpath(str(path))) for path in directories]
    result = list(prefix)
    seen = set(prefix)
    for part in os.environ.get("PATH", "").split(os.pathsep):
        normalized = os.path.normcase(os.path.normpath(part)) if part else ""
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    os.environ["PATH"] = os.pathsep.join(result)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def git(root: Path, *args: str) -> bytes:
    environment = {k: v for k, v in os.environ.items() if k not in GIT_CONTEXT}
    return subprocess.check_output(["git", *args], cwd=root, env=environment)


def _pytest_options(root: Path) -> tuple[set[Path], list[str]]:
    """Exclude only declared untracked reports, never a tracked source file."""
    values = shlex.split(os.environ.get("PYTEST_ADDOPTS", ""))
    reports: set[Path] = set()
    stable: list[str] = []
    i = 0
    while i < len(values):
        value = values[i]
        if value.startswith(("--junitxml=", "--junit-xml=")):
            reports.add((root / value.split("=", 1)[1]).resolve())
        elif value in {"--junitxml", "--junit-xml"} and i + 1 < len(values):
            reports.add((root / values[i + 1]).resolve())
            i += 1
        elif (
            (
                value == "-p"
                and i + 1 < len(values)
                and values[i + 1]
                in {
                    "testmon.pytest_testmon",
                    "xdist.plugin",
                }
            )
            or value in {"-n", "--numprocesses"}
            and i + 1 < len(values)
        ):
            if value != "-p" and not (
                values[i + 1].isdigit() or values[i + 1] in {"auto", "logical"}
            ):
                raise ValueError("Use an integer, auto or logical for pytest worker counts")
            stable.extend([value, values[i + 1]])
            i += 1
        elif (
            value.startswith("-n")
            and not value.startswith("--")
            and value[2:]
            or value
            in {
                "-q",
                "-qq",
                "-v",
                "-vv",
                "-ra",
                "-rA",
                "--disable-warnings",
                "--no-header",
                "--no-summary",
                "--failed-first",
                "--ff",
                "--new-first",
                "--nf",
            }
            or value.startswith(("--maxfail=", "--maxschedchunk=", "--color=", "--tb="))
        ):
            stable.append(value)
        else:
            raise ValueError(
                "Acceptance cannot use unsupported PYTEST_ADDOPTS: "
                + repr(value)
                + ". Use uv run pytest for ad hoc selection/collection, then rerun this gate with only report, verbosity or failure-limit options."
            )
        i += 1
    return reports, stable


def inputs(root: Path) -> dict[str, str]:
    tracked = set(git(root, "ls-files", "-z").split(b"\0")) - {b""}
    names = tracked | (
        set(git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")) - {b""}
    )
    reports, _ = _pytest_options(root)
    result: dict[str, str] = {}
    for encoded in names:
        name = encoded.decode("utf-8", errors="surrogateescape")
        path = root / name
        if encoded not in tracked:
            if any(p in CACHE_NAMES for p in path.relative_to(root).parts):
                continue
            if name.startswith(".testmondata") or path.resolve() in reports:
                continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(
                "Source links require the full original test command; no reusable baseline can be recorded"
            )
        if path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Built dashboard files can be read by packaging tests even though ignored by Git.
    for path in (root / "src/octop/dashboard").rglob("*"):
        if path.is_file():
            if not path.resolve().is_relative_to(root):
                raise ValueError("Dashboard output leaves this worktree")
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def environment(workers: str) -> str:
    values = {k: v for k, v in os.environ.items() if k not in VOLATILE_ENV}
    return digest(
        {
            "variables": values,
            "workers": workers,
            "python": sys.version,
            "executable": str(Path(sys.executable).resolve()),
            "platform": sys.platform,
            "packages": sorted(
                (d.metadata["Name"], d.version) for d in importlib.metadata.distributions()
            ),
            "pytest_options": _pytest_options(Path.cwd())[1],
        }
    )


def environment_variables() -> dict[str, str]:
    """Record only hashes so fallback diagnostics can name changed keys safely."""
    return {
        k: hashlib.sha256(v.encode()).hexdigest()
        for k, v in os.environ.items()
        if k not in VOLATILE_ENV
    }


@contextmanager
def gate_lock(storage: Path) -> Iterator[None]:
    """Serialize only this worktree's gate; a crashed parent releases the OS lock."""
    with (storage / "lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("This worktree's test gate is already running; wait for it") from error
        try:
            yield
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def cache_digest(path: Path) -> dict[str, str] | None:
    """Bind the real private SQLite graph, including any uncheckpointed WAL."""
    if not path.is_file() or path.is_symlink():
        return None
    with path.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\0":
            return None
    result = {"database": hashlib.sha256(path.read_bytes()).hexdigest()}
    wal = Path(str(path) + "-wal")
    if wal.exists():
        if wal.is_symlink():
            return None
        result["wal"] = hashlib.sha256(wal.read_bytes()).hexdigest()
    return result


def full_test_inputs(previous: dict[str, str], current: dict[str, str]) -> list[str]:
    """Name inputs that cannot use the existing actual Python dependency graph."""
    changed = {p for p in previous.keys() | current.keys() if previous.get(p) != current.get(p)}
    blocked = []
    for name in sorted(changed):
        path = Path(name)
        package_source = (
            len(path.parts) >= 4 and path.parts[0] == "packages" and path.parts[2] == "src"
        )
        eligible = (
            path.suffix == ".py"
            and path.name != "conftest.py"
            and (name.startswith(("src/", "tests/")) or package_source)
            and not name.startswith("tests/support/")
        )
        if eligible and name.startswith("tests/"):
            # Testmon discovers new ordinary modules and runs their unseen cases.
            # Removals and new fixture/helper files retain the full original run.
            eligible = name in current and (
                name in previous
                or (
                    name.startswith(("tests/unit/", "tests/integration/"))
                    and path.name.startswith("test_")
                )
            )
        if not eligible:
            blocked.append(name)
    return blocked


def python_only(previous: dict[str, str], current: dict[str, str]) -> bool:
    return not full_test_inputs(previous, current)


def run(
    root: Path,
    workers: str,
    force_full: bool,
    require_reuse: bool = False,
    include_live: bool = False,
) -> int:
    root = root.resolve()
    runner = [sys.executable, "-m"]
    child_environment = {k: v for k, v in os.environ.items() if k not in GIT_CONTEXT}
    child_environment.pop("MOSSWORK_TEST_REQUIRE_REUSE", None)
    _pytest_options(root)

    def full_without_record(reason: str) -> int:
        if require_reuse:
            raise ValueError(
                "Reusable history unavailable: "
                + reason
                + ". No tests ran; run make all without the reuse-only guard (--require-reuse or MOSSWORK_TEST_REQUIRE_REUSE=1) for required full validation."
            )
        scope = "complete" if include_live else "non-live"
        print("TEST_GATE: full " + scope + " suite; no reusable baseline: " + reason, flush=True)
        scope_arguments = [] if include_live else ["-m", "not live"]
        return subprocess.run(
            [*runner, "pytest", "--testmon-noselect", *scope_arguments, "-n", workers],
            cwd=root,
            env=child_environment,
            check=False,
        ).returncode

    try:
        top = Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    except (OSError, subprocess.CalledProcessError):
        return full_without_record("Git source history is unavailable")
    if top != root:
        raise ValueError("Run the acceptance gate from the repository root")
    try:
        storage = (
            Path(git(root, "rev-parse", "--absolute-git-dir").decode().strip())
            / "mosswork-test-gate"
        )
        storage.mkdir(exist_ok=True)
        inputs(root)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        return full_without_record(str(error))
    with gate_lock(storage):
        return run_locked(
            root,
            storage,
            workers,
            force_full,
            runner,
            child_environment,
            require_reuse,
            include_live,
        )


def run_locked(
    root: Path,
    storage: Path,
    workers: str,
    force_full: bool,
    runner: list[str],
    child_environment: dict[str, str],
    require_reuse: bool,
    include_live: bool = False,
) -> int:
    before, env = inputs(root), environment(workers)
    variables = environment_variables()
    baseline_path = storage / "baseline.json"
    history_path = storage / "history.json"
    try:
        previous = json.loads(
            (history_path if history_path.exists() else baseline_path).read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ValueError):
        previous = {}
    if (
        not isinstance(previous, dict)
        or previous.get("schema") != 2
        or not isinstance(previous.get("inputs"), dict)
    ):
        previous = {}
    graph_name = previous.get("graph", "")
    graph = storage / graph_name if isinstance(graph_name, str) else storage
    valid_graph = (
        re.fullmatch(r"graph-[a-f0-9]{32}\.sqlite3", str(graph_name)) is not None
        and graph.resolve().parent == storage.resolve()
        and previous.get("cache_digest") == cache_digest(graph)
        and previous.get("cache_digest") is not None
    )
    blocked_inputs = full_test_inputs(previous.get("inputs", {}), before)
    pending = storage / "incomplete.json"
    reusable = bool(
        not force_full
        and not pending.exists()
        and (previous.get("passed") or previous.get("trusted_graph"))
        and previous.get("root") == str(root)
        and previous.get("environment") == env
        and valid_graph
        and not blocked_inputs
    )
    mode = "affected" if reusable else "full"
    reason = (
        "verified real testmon history; failed and unexecuted cases remain required"
        if reusable
        else "full requested, or baseline/graph/environment/input scope is unproven"
    )
    if not reusable:
        if previous.get("environment") and previous.get("environment") != env:
            old_variables = previous.get("environment_variables")
            changed = (
                sorted(
                    k
                    for k in variables.keys() | old_variables.keys()
                    if variables.get(k) != old_variables.get(k)
                )
                if isinstance(old_variables, dict)
                else ["older history has no per-variable detail"]
            )
            reason = "environment changed: " + (
                ", ".join(changed) if changed else "Python, installed packages or worker settings"
            )
        elif pending.exists():
            reason = "previous run incomplete; its graph cannot be reused"
        elif not previous:
            reason = "no verified history in this worktree"
        elif not valid_graph:
            reason = "private graph missing or content changed"
        elif blocked_inputs:
            reason = "configuration, unknown input or test inventory changed: " + json.dumps(
                blocked_inputs[:20], ensure_ascii=True
            )
            if len(blocked_inputs) > 20:
                reason += f" (+{len(blocked_inputs) - 20} more inputs)"
        if require_reuse:
            raise ValueError(
                "Reusable history unavailable: "
                + reason
                + ". No tests ran; run make all without the reuse-only guard (--require-reuse or MOSSWORK_TEST_REQUIRE_REUSE=1) for required full validation."
            )
    nonce = uuid.uuid4().hex
    if not reusable:
        graph = storage / ("graph-" + nonce + ".sqlite3")
    # A stopped run may still have children. Never reuse its graph namespace.
    pending.write_text(json.dumps({"nonce": nonce, "graph": graph.name}) + "\n", encoding="utf-8")
    child_environment.pop("TESTMON_DATAFILE", None)
    path = storage / (str(time.time_ns()) + "-" + nonce + ".json")
    session_path = path.with_suffix(".session.json")
    # Private output paths live in the main checkout's Git directory for linked
    # worktrees. Pin config/root before pytest interprets those option values.
    argv = [*runner, "pytest", "--rootdir", str(root), "-c", str(root / "pyproject.toml")]
    argv += [
        "--testmon-forceselect"
        if reusable and not include_live
        else "--testmon"
        if reusable
        else "--testmon-noselect"
    ]
    if not include_live:
        # Supported marker intersection keeps live cases unexecuted, rather than
        # caching an artificial skip that the later commit hook might reuse.
        argv += ["-m", "not live"]
    argv += ["-n", workers]
    # Before conftest registers these options, pytest treats separated existing
    # paths as potential collection roots. Keep private outputs inside each option.
    argv += ["--gate-report=" + str(session_path), "--gate-nonce=" + nonce]
    argv += ["--gate-datafile=" + str(graph)]
    attempt: dict[str, Any] = {
        "schema": 2,
        "graph": graph.name,
        "root": str(root),
        "passed": False,
        "mode": mode,
        "scope": "including-live" if include_live else "non-live",
        "reason": reason,
        "inputs": before,
        "environment": env,
        "environment_variables": variables,
        "argv": argv,
        "prior_history": previous.get("attempt"),
        "prior_passed": bool(previous.get("passed")),
        "started_at": time.time(),
    }
    attempt["attempt"] = str(path)
    print(
        "TEST_GATE: "
        + json.dumps(
            {
                "mode": mode,
                "scope": attempt["scope"],
                "workers": workers,
                "reason": reason,
                "prior_history": attempt["prior_history"],
                "prior_passed": attempt["prior_passed"],
                "receipt": str(path),
                "graph": str(graph),
            },
            ensure_ascii=True,
        ),
        flush=True,
    )
    print(
        "TEST_GATE: This run is "
        + mode
        + " validation. Original assertions and deadlines remain required. "
        + (
            "Hook selection includes live cases. "
            if include_live
            else "Make selection is non-live. "
        )
        + "To request every case explicitly: make test.",
        flush=True,
    )
    started = time.monotonic()
    try:
        process = subprocess.run(argv, cwd=root, env=child_environment, check=False)
        attempt["exit_code"] = process.returncode
        session = (
            json.loads(session_path.read_text(encoding="utf-8")) if session_path.is_file() else {}
        )
        if not isinstance(session, dict):
            session = {}
        attempt["pytest_session"] = session
        actual_session = (
            session.get("nonce") == nonce
            and session.get("root") == str(root)
            and session.get("exit_code") == process.returncode
            and session.get("collect_only") is False
        )
        successful_session = (
            actual_session
            and session.get("interrupted") is False
            and session.get("worker_crashed") is False
            and session.get("errors") == 0
            and (reusable or session.get("collected", 0) > 0)
        )
        if process.returncode == 0 and not successful_session:
            raise ValueError(
                "Actual pytest completion receipt is missing or mismatched; no baseline accepted"
            )
        after = inputs(root)
        attempt["changed_during_validation"] = sorted(
            p for p in before.keys() | after.keys() if before.get(p) != after.get(p)
        )
        if attempt["changed_during_validation"] or environment(workers) != env:
            raise ValueError(
                "Source/environment changed during validation; this result cannot update the baseline. Retain this receipt and rerun on the frozen candidate."
            )
        attempt["passed"] = process.returncode == 0
        # An assertion failure is still a failed gate. Its real per-test history
        # can retain passed cases while testmon selects failed/unexecuted cases.
        ordinary_failure = (
            (
                process.returncode == 1
                and session.get("interrupted") is False
                or process.returncode == 2
                and session.get("maxfail_stop") is True
            )
            and session.get("failed", 0) > 0
            and session.get("errors") == 0
            and session.get("worker_crashed") is False
            and session.get("collected", 0) > 0
        )
        if actual_session and (attempt["passed"] or ordinary_failure):
            attempt["cache_digest"] = cache_digest(graph)
            if attempt["cache_digest"] is not None:
                attempt["trusted_graph"] = True
                temporary = storage / "history.tmp"
                temporary.write_text(json.dumps(attempt, indent=2) + "\n", encoding="utf-8")
                temporary.replace(history_path)
                if attempt["passed"]:
                    temporary = storage / "baseline.tmp"
                    temporary.write_text(json.dumps(attempt, indent=2) + "\n", encoding="utf-8")
                    temporary.replace(baseline_path)
                pending.unlink()
        return process.returncode
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        attempt["error"] = str(error)
        raise
    finally:
        attempt["seconds"] = time.monotonic() - started
        path.write_text(json.dumps(attempt, indent=2) + "\n", encoding="utf-8")
        print("TEST_GATE_RECEIPT: " + str(path), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", default="2")
    parser.add_argument("--full", action="store_true")
    parser.add_argument(
        "--include-live", action="store_true", help="Preserve the commit hook's live-test selection"
    )
    parser.add_argument("--git-context", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--launch-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--require-reuse",
        action="store_true",
        help="Refuse before pytest if a reuse measurement would fall back to full",
    )
    args = parser.parse_args()
    options = {
        "workers": args.workers,
        "full": args.full,
        "include_live": args.include_live,
        "require_reuse": args.require_reuse or os.environ.get("MOSSWORK_TEST_REQUIRE_REUSE") == "1",
    }
    try:
        if args.launch_id is not None and (
            not args.git_context or re.fullmatch(r"[a-f0-9]{32}", args.launch_id) is None
        ):
            raise ValueError("Invalid internal Git launch identity; use the supported Make entry")
        if not args.git_context:
            status = git_launch_context(Path.cwd(), sys.argv[1:], options)
            if status is not None:
                return status
        else:
            normalize_launch_environment()
        status = run(
            Path.cwd(),
            args.workers,
            args.full,
            args.require_reuse or os.environ.get("MOSSWORK_TEST_REQUIRE_REUSE") == "1",
            args.include_live,
        )
        if status == 0 and args.launch_id is not None:
            complete_git_launch(Path.cwd(), args.launch_id, options)
        return status
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("TEST_GATE_REFUSED: " + str(error), file=sys.stderr, flush=True)
        return 1


# Frontend/scoped dispatchers load these standard-library helpers under private
# module names. Only the supported pytest plugin entry registers pytest hooks.
if __name__ == "tools.test_gate":
    import pytest

    def pytest_addoption(parser: Any) -> None:
        parser.addoption("--gate-datafile", default=None, help="Internal worktree-private graph")
        parser.addoption(
            "--gate-report", default=None, help="Internal source-bound gate session receipt"
        )
        parser.addoption("--gate-nonce", default=None, help="Internal gate attempt identity")

    class _GateInterrupt:
        def __init__(self, config: Any) -> None:
            self.config = config

        def pytest_keyboard_interrupt(self, excinfo: Any) -> None:
            self.config._test_gate_interrupted = True
            error = excinfo.value
            stop = re.fullmatch(r"stopping after ([1-9][0-9]*) failures", str(error))
            maxfail = self.config.option.maxfail
            self.config._test_gate_maxfail_stop = bool(
                type(error).__module__ == "xdist.dsession"
                and type(error).__name__ == "Interrupted"
                and stop is not None
                and maxfail > 0
                and int(stop.group(1)) >= maxfail
            )

    @pytest.hookimpl(tryfirst=True)
    def pytest_configure(config: Any) -> None:
        graph = config.getoption("--gate-datafile")
        if graph is None:
            return
        config.pluginmanager.register(_GateInterrupt(config), "mosswork-gate-interrupt")
        storage = (
            Path(git(Path(config.rootpath), "rev-parse", "--absolute-git-dir").decode().strip())
            / "mosswork-test-gate"
        )
        path = Path(graph).resolve()
        if (
            path.parent != storage.resolve()
            or re.fullmatch(r"graph-[a-f0-9]{32}\.sqlite3", path.name) is None
        ):
            raise ValueError("Invalid private graph; use the supported Make entry")
        config._test_gate_previous_datafile = os.environ.get("TESTMON_DATAFILE")
        # Testmon opens its controller/worker database in pytest_configure. Scope its
        # supported environment option to startup; test subprocesses must not inherit it.
        os.environ["TESTMON_DATAFILE"] = str(path)

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(session: Any) -> None:
        if not session.config.getoption("--gate-datafile"):
            return
        previous = session.config._test_gate_previous_datafile
        if previous is None:
            os.environ.pop("TESTMON_DATAFILE", None)
        else:
            os.environ["TESTMON_DATAFILE"] = previous

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(node: Any, error: Any) -> None:
        if error is not None:
            node.config._test_gate_worker_crashed = True

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(session: Any, exitstatus: Any) -> Iterator[None]:
        # Testmon converts its legitimate empty selection from exit 5 to exit 0.
        # Observe the completed hook chain, matching the process's final status.
        yield
        if hasattr(session.config, "workerinput"):
            return
        destination = session.config.getoption("--gate-report")
        if destination is None:
            return
        nonce = session.config.getoption("--gate-nonce")
        root = Path(session.config.rootpath).resolve()
        allowed = (
            Path(git(root, "rev-parse", "--absolute-git-dir").decode().strip())
            / "mosswork-test-gate"
        )
        target = Path(destination).resolve()
        if (
            not isinstance(nonce, str)
            or re.fullmatch(r"[a-f0-9]{32}", nonce) is None
            or target.parent != allowed.resolve()
            or not target.name.endswith("-" + nonce + ".session.json")
        ):
            raise ValueError("Invalid gate session output; use the supported Make entry")
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        stats = reporter.stats if reporter is not None else {}
        target.write_text(
            json.dumps(
                {
                    "nonce": nonce,
                    "root": str(root),
                    "exit_code": int(session.exitstatus),
                    "collect_only": bool(session.config.option.collectonly),
                    "collected": session.testscollected,
                    "passed": len(stats.get("passed", [])),
                    "skipped": len(stats.get("skipped", [])),
                    "failed": len(stats.get("failed", [])),
                    "errors": len(stats.get("error", [])),
                    "worker_crashed": bool(
                        getattr(session.config, "_test_gate_worker_crashed", False)
                    ),
                    "interrupted": bool(getattr(session.config, "_test_gate_interrupted", False)),
                    "maxfail_stop": bool(getattr(session.config, "_test_gate_maxfail_stop", False)),
                    "deselected": len(stats.get("deselected", [])),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    raise SystemExit(main())
