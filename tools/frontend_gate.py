"""Validate a complete frontend-only candidate and reuse its actual local result.

The dispatcher and this entry both check the complete branch/worktree scope.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any


def frontend_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.as_posix() != name or ".." in path.parts or "\\" in name:
        return False
    return (
        name.startswith(("dashboard/src/", "dashboard/public/"))
        and path.suffix
        in {
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".css",
            ".less",
            ".json",
            ".svg",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".ico",
            ".woff",
            ".woff2",
        }
    ) or name == "dashboard/index.html"


def commands(root: Path, changed: list[str], node: Path) -> list[list[str]]:
    sources = [
        n.removeprefix("dashboard/")
        for n in changed
        if n.endswith((".ts", ".tsx")) and (root / n).is_file()
    ]
    formatted = [
        n.removeprefix("dashboard/")
        for n in changed
        if n.endswith((".ts", ".tsx", ".js", ".jsx", ".json", ".less", ".css", ".html"))
        and (root / n).is_file()
    ]
    npm = "npm.cmd" if os.name == "nt" else "npm"
    npx = "npx.cmd" if os.name == "nt" else "npx"
    compiler = root / "dashboard/node_modules/typescript/bin/tsc"
    if not compiler.is_file():
        raise ValueError("Owned TypeScript installation is missing; run npm ci in this dashboard")
    checks = []
    if sources:
        checks.append([npx, "eslint", *sources])
    if formatted:
        checks.append([npx, "prettier", "--check", *formatted])
    return checks + [
        [str(node), str(compiler), "-b", "--force"],
        [npm, "test", "--", "--maxWorkers=2", "--bail=1"],
        [npm, "run", "build"],
    ]


def raw_sources(inputs: dict[str, str]) -> dict[str, str]:
    # This is the existing build destination, never authored application input.
    return {
        name: value for name, value in inputs.items() if not name.startswith("src/octop/dashboard/")
    }


def installed_modules(root: Path) -> dict[str, Any]:
    return installed_tree(root / "dashboard/node_modules", frontend_cache=True)


def installed_tree(directory: Path, *, frontend_cache: bool = False) -> dict[str, Any]:
    if directory.is_symlink() or directory.is_junction():
        raise ValueError(
            "Use an owned locked frontend installation; shared dependency links stay unchanged"
        )
    sources, cache = hashlib.sha256(), hashlib.sha256()
    count = 0
    boundary = directory.resolve()
    pending = [(str(directory), "")]
    while pending:
        current, prefix = pending.pop()
        # DirEntry retains the metadata returned by directory enumeration.
        # Re-statting every installed file dominates this check on Windows.
        with os.scandir(current) as stream:
            entries = sorted(stream, key=lambda entry: entry.name)
        directories = []
        for entry in entries:
            relative = prefix + entry.name
            if entry.is_dir():
                reparse = os.name == "nt" and entry.stat(follow_symlinks=False).st_reparse_tag
                if entry.is_symlink() or reparse:
                    raise ValueError(
                        "Linked dependency directories cannot qualify reusable validation; install the locked frontend dependencies"
                    )
                directories.append((entry.path, relative + "/"))
                continue
            if not entry.is_file():
                raise ValueError("Installed frontend input is not a regular file")
            linked = entry.is_symlink()
            if linked and not Path(entry.path).resolve().is_relative_to(boundary):
                raise ValueError("Installed dependency leaves this dependency tree")
            with open(entry.path, "rb") as handle:
                payload = hashlib.file_digest(handle, "sha256").digest()
            generated = frontend_cache and (
                relative.split("/", 1)[0] in {".vite", ".vite-temp", ".cache"}
                or relative in {".tmp/tsconfig.app.tsbuildinfo", ".tmp/tsconfig.node.tsbuildinfo"}
            )
            target = cache if generated else sources
            link = os.readlink(entry.path).encode() if linked else b""
            target.update(relative.encode() + b"\0" + link + b"\0" + payload)
            count += 1
        pending.extend(reversed(directories))
    return {"sources": sources.hexdigest(), "cache": cache.hexdigest(), "files": count}


def tool_modules() -> dict[str, Any]:
    """Bind external tool programs and npm's installed code, without running them."""
    payload: dict[str, Any] = {}
    for name in ("git", "uv", "make", "node", "npm", "npx"):
        found = shutil.which(name)
        if found is None:
            payload[name] = None
            continue
        path = Path(found).resolve()
        with path.open("rb") as handle:
            payload[name] = {
                "path": str(path),
                "sha256": hashlib.file_digest(handle, "sha256").hexdigest(),
            }
        if name == "npm":
            package = path.parent / "node_modules/npm" if os.name == "nt" else path.parent.parent
            if not (package / "package.json").is_file():
                raise ValueError(
                    "Installed npm code cannot be bound; original checks remain required"
                )
            payload["npm_code"] = installed_tree(package)
    return {
        "sources": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        "cache": "",
        "files": len(payload),
    }


def validate(
    root: Path,
    changed: list[str],
    gate: Any,
    *,
    tool_checks: list[list[str]] | None = None,
    reuse_allowed: bool = True,
    outcome: dict[str, Any] | None = None,
) -> int:
    tools = tool_checks is not None
    profile = "tools" if tools else "frontend"
    prefix = "TOOLS_GATE" if tools else "FRONTEND_GATE"
    if not tools and (not changed or not all(frontend_path(name) for name in changed)):
        raise ValueError("Frontend validation requires a nonempty complete frontend-only diff")
    gate.normalize_launch_environment()
    for name in ("GIT_AUTHOR_DATE", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_NAME", "GIT_EDITOR"):
        os.environ.pop(name, None)
    for name in gate.GIT_CONTEXT:
        os.environ.pop(name, None)
    storage = Path(gate.git(root, "rev-parse", "--absolute-git-dir").decode().strip()) / (
        "mosswork-tools-gate" if tools else "mosswork-frontend-gate"
    )
    storage.mkdir(exist_ok=True)
    with gate.gate_lock(storage):
        before = gate.inputs(root)
        index_before = gate.git(root, "ls-files", "--stage", "-z")

        def installed() -> dict[str, str]:
            if tools:
                executable = Path(sys.executable).resolve()
                with executable.open("rb") as handle:
                    executable_hash = hashlib.file_digest(handle, "sha256").hexdigest()
                return {
                    "python_executable": str(executable),
                    "python_payload": executable_hash,
                    "git_head": gate.git(root, "rev-parse", "HEAD").decode().strip(),
                    "git_configuration": hashlib.sha256(
                        gate.git(root, "config", "--null", "--list")
                    ).hexdigest(),
                }
            metadata = json.loads(
                subprocess.check_output(
                    [
                        "node",
                        "-p",
                        "JSON.stringify({node:process.version,node_executable:process.execPath})",
                    ],
                    cwd=root,
                )
            )
            return {
                "node": metadata["node"],
                "node_executable": metadata["node_executable"],
                "installed_lock": hashlib.sha256(
                    (root / "dashboard/node_modules/.package-lock.json").read_bytes()
                ).hexdigest(),
            }

        versions = installed()
        if not tools:
            node = Path(versions["node_executable"])
            with node.open("rb") as handle:
                versions["node_payload"] = hashlib.file_digest(handle, "sha256").hexdigest()
        module_inputs = tool_modules if tools else lambda: installed_modules(root)
        modules = module_inputs()
        workers = "tools" if tools else "2"
        environment = gate.digest({"parent": gate.environment(workers), **versions})
        checks = tool_checks if tools else commands(root, changed, node)
        if not checks:
            raise ValueError("Reusable validation requires nonempty actual checks")
        recipe = gate.digest(checks)
        prior = None
        history = storage / "history.json"
        if history.is_file() and not history.is_symlink():
            with suppress(OSError, ValueError):
                prior = json.loads(history.read_text(encoding="utf-8"))
        if not isinstance(prior, dict):
            prior = None
        record: dict[str, Any] = {
            "profile": profile,
            "complete": False,
            "reuse_allowed": reuse_allowed,
            "root": str(root.resolve()),
            "passed": False,
            "inputs": before,
            "environment": environment,
            "recipe": recipe,
            "versions": versions,
            "checks": [],
            "paths": changed,
            "environment_keys": gate.environment_variables(),
            "installed_modules": modules,
        }
        attempt = storage / (str(time.time_ns()) + "-" + uuid.uuid4().hex + ".json")
        started = time.monotonic()

        def save() -> None:
            text = json.dumps(record, indent=2, ensure_ascii=True) + "\n"
            attempt.write_text(text, encoding="utf-8")
            temporary = storage / "history.tmp"
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(history)

        def assert_stable() -> dict[str, str]:
            after: dict[str, str] = gate.inputs(root)
            after_authored = after if tools else raw_sources(after)
            before_authored = before if tools else raw_sources(before)
            if after_authored != before_authored:
                raise ValueError(
                    f"Authored source changed during {profile} validation; retain the failure and validate the current source"
                )
            if gate.git(root, "ls-files", "--stage", "-z") != index_before:
                raise ValueError(f"The Git index changed during {profile} validation")
            current_versions = installed()
            if not tools:
                with Path(current_versions["node_executable"]).open("rb") as handle:
                    current_versions["node_payload"] = hashlib.file_digest(
                        handle, "sha256"
                    ).hexdigest()
            if (
                gate.digest({"parent": gate.environment(workers), **current_versions})
                != environment
            ):
                raise ValueError("The validation environment changed")
            return after

        try:
            reusable = (
                reuse_allowed
                and prior is not None
                and prior.get("passed") is True
                and prior.get("complete") is True
                and all(
                    prior.get(name) == record[name]
                    for name in (
                        "profile",
                        "root",
                        "inputs",
                        "environment",
                        "recipe",
                        "reuse_allowed",
                    )
                )
                and prior.get("installed_modules") == modules
            )
            if reusable and prior is not None:
                try:
                    previous = Path(prior["attempt"])
                    reusable = (
                        previous.parent.resolve() == storage.resolve()
                        and not previous.is_symlink()
                        and json.loads(previous.read_text(encoding="utf-8")) == prior
                    )
                    original = prior.get("reused") or {
                        "attempt": str(previous),
                        "sha256": hashlib.sha256(previous.read_bytes()).hexdigest(),
                    }
                    proof_path = Path(original["attempt"])
                    proof_bytes = (
                        proof_path.read_bytes()
                        if proof_path.parent.resolve() == storage.resolve()
                        and not proof_path.is_symlink()
                        else b""
                    )
                    proof = json.loads(proof_bytes)
                    reusable = (
                        reusable
                        and isinstance(proof, dict)
                        and hashlib.sha256(proof_bytes).hexdigest() == original["sha256"]
                        and proof.get("passed") is True
                        and proof.get("complete") is True
                        and all(
                            proof.get(name) == record[name]
                            for name in (
                                "profile",
                                "root",
                                "inputs",
                                "environment",
                                "recipe",
                                "installed_modules",
                                "reuse_allowed",
                            )
                        )
                        and [item.get("argv") for item in proof.get("checks", [])] == checks
                        and all(item.get("exitCode") == 0 for item in proof["checks"])
                    )
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    reusable = False
            save()  # An interrupted new attempt cannot leave an older success current.
            if reusable:
                record["reused"] = original
                print(
                    f"{prefix}: reusing this worktree's matching actual checks from {original['attempt']}; no checks run in this attempt",
                    flush=True,
                )
            else:
                if tools and prior is not None:
                    fields = [
                        name
                        for name in (
                            "profile",
                            "inputs",
                            "environment",
                            "recipe",
                            "installed_modules",
                            "reuse_allowed",
                        )
                        if prior.get(name) != record.get(name)
                    ]
                    previous_keys = prior.get("environment_keys", {})
                    current_keys = record["environment_keys"]
                    changed_keys = (
                        sorted(
                            name
                            for name in previous_keys.keys() | current_keys.keys()
                            if previous_keys.get(name) != current_keys.get(name)
                        )
                        if isinstance(previous_keys, dict)
                        else []
                    )
                    print(
                        "TOOLS_GATE_FRESH: "
                        + (", ".join(fields) or "no intact complete original success")
                        + (
                            "; environment keys: " + ", ".join(changed_keys) if changed_keys else ""
                        ),
                        flush=True,
                    )
                print(
                    "TOOLS_GATE: execute the original tool checks; only complete matching source, commands, target environment and installed tools can reuse actual success."
                    if tools
                    else "FRONTEND_GATE: changed-file lint/format, current-source TypeScript check, full frontend tests and Vite build. Type errors stop before tests; mixed/unknown inputs require the application gate.",
                    flush=True,
                )
                for argv in checks:
                    print(prefix + "_COMMAND: " + json.dumps(argv), flush=True)
                    item = {"argv": argv, "started_ns": time.time_ns()}
                    record["checks"].append(item)
                    save()
                    result = subprocess.run(
                        argv, cwd=root if tools else root / "dashboard", check=False
                    )
                    item["exitCode"] = result.returncode
                    save()
                    if result.returncode:
                        raise ValueError(
                            f"{profile.title()} command failed ({result.returncode}); later checks were not run"
                        )
                    assert_stable()
            after = assert_stable()
            completed_modules = module_inputs()
            if completed_modules["sources"] != modules["sources"]:
                raise ValueError(f"Installed {profile} code changed during validation")
            record["installed_modules"] = completed_modules
            record.update(inputs=after, passed=True)
            return 0
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            record["error"] = str(error)
            print(prefix + "_REFUSED: " + str(error), file=sys.stderr, flush=True)
            return 1
        finally:
            record.update(attempt=str(attempt), complete=True, seconds=time.monotonic() - started)
            save()
            if outcome is not None:
                outcome.update(record)
            print(prefix + "_RECEIPT: " + str(attempt), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-context", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--launch-id", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.launch_id is not None and (
        not args.git_context or re.fullmatch(r"[a-f0-9]{32}", args.launch_id) is None
    ):
        raise ValueError("Invalid internal frontend launch identity")
    root = Path.cwd()
    source = root / "tools/test_gate.py"
    spec = importlib.util.spec_from_file_location("mosswork_frontend_shared_inputs", source)
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    spec = importlib.util.spec_from_file_location(
        "mosswork_frontend_scope", root / "tools/precommit.py"
    )
    assert spec is not None and spec.loader is not None
    dispatcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatcher)
    changed, base, _ = dispatcher.worktree_paths(root)
    if base is None or not dispatcher.frontend_only(root, changed):
        raise ValueError(
            "The complete branch/worktree is not frontend-only; use the original application gate"
        )
    options = {"profile": "frontend", "workers": 2, "full": True}
    if not args.git_context:
        status: int | None = gate.git_launch_context(
            root, sys.argv[1:], options, entry="tools/frontend_gate.py"
        )
        if status is not None:
            return status
    status = validate(root, changed, gate)
    if status == 0 and args.launch_id is not None:
        gate.complete_git_launch(root, args.launch_id, options, entry="tools/frontend_gate.py")
    return status


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("FRONTEND_GATE_REFUSED: " + str(error), file=sys.stderr, flush=True)
        raise SystemExit(1) from error
