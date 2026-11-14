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


def commands(root: Path, changed: list[str]) -> list[list[str]]:
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
    checks = []
    if sources:
        checks.append([npx, "eslint", *sources])
    if formatted:
        checks.append([npx, "prettier", "--check", *formatted])
    return checks + [
        [npx, "tsc", "-b", "--force"],
        [npm, "test", "--", "--maxWorkers=2", "--bail=1"],
        [npm, "run", "build"],
    ]


def raw_sources(inputs: dict[str, str]) -> dict[str, str]:
    # This is the existing build destination, never authored application input.
    return {
        name: value for name, value in inputs.items() if not name.startswith("src/octop/dashboard/")
    }


def installed_modules(root: Path) -> dict[str, Any]:
    directory = root / "dashboard/node_modules"
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
            target = (
                cache if relative.split("/", 1)[0] in {".vite", ".vite-temp", ".cache"} else sources
            )
            link = os.readlink(entry.path).encode() if linked else b""
            target.update(relative.encode() + b"\0" + link + b"\0" + payload)
            count += 1
        pending.extend(reversed(directories))
    return {"sources": sources.hexdigest(), "cache": cache.hexdigest(), "files": count}


def validate(root: Path, changed: list[str], gate: Any) -> int:
    if not changed or not all(frontend_path(name) for name in changed):
        raise ValueError("Frontend validation requires a nonempty complete frontend-only diff")
    gate.normalize_launch_environment()
    for name in ("GIT_AUTHOR_DATE", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_NAME", "GIT_EDITOR"):
        os.environ.pop(name, None)
    for name in gate.GIT_CONTEXT:
        os.environ.pop(name, None)
    storage = (
        Path(gate.git(root, "rev-parse", "--absolute-git-dir").decode().strip())
        / "mosswork-frontend-gate"
    )
    storage.mkdir(exist_ok=True)
    with gate.gate_lock(storage):
        before = gate.inputs(root)
        index_before = gate.git(root, "ls-files", "--stage", "-z")

        def installed() -> dict[str, str]:
            return {
                "node": subprocess.check_output(["node", "--version"], cwd=root).decode().strip(),
                "node_executable": subprocess.check_output(
                    ["node", "-p", "process.execPath"], cwd=root
                )
                .decode()
                .strip(),
                "installed_lock": hashlib.sha256(
                    (root / "dashboard/node_modules/.package-lock.json").read_bytes()
                ).hexdigest(),
            }

        versions = installed()
        node = Path(versions["node_executable"])
        with node.open("rb") as handle:
            versions["node_payload"] = hashlib.file_digest(handle, "sha256").hexdigest()
        modules = installed_modules(root)
        environment = gate.digest({"parent": gate.environment("2"), **versions})
        checks = commands(root, changed)
        recipe = gate.digest(checks)
        prior = None
        history = storage / "history.json"
        if history.is_file() and not history.is_symlink():
            with suppress(OSError, ValueError):
                prior = json.loads(history.read_text(encoding="utf-8"))
        if not isinstance(prior, dict):
            prior = None
        record: dict[str, Any] = {
            "profile": "frontend",
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
            after = gate.inputs(root)
            if raw_sources(after) != raw_sources(before):
                raise ValueError(
                    "Authored source changed during frontend validation; retain the failure and validate the current source"
                )
            if gate.git(root, "ls-files", "--stage", "-z") != index_before:
                raise ValueError("The Git index changed during frontend validation")
            current_versions = installed()
            with Path(current_versions["node_executable"]).open("rb") as handle:
                current_versions["node_payload"] = hashlib.file_digest(handle, "sha256").hexdigest()
            if gate.digest({"parent": gate.environment("2"), **current_versions}) != environment:
                raise ValueError("The validation environment changed")
            return after

        try:
            reusable = (
                prior is not None
                and prior.get("passed") is True
                and all(
                    prior.get(name) == record[name]
                    for name in ("profile", "root", "inputs", "environment", "recipe")
                )
                and prior.get("installed_modules") == modules
            )
            if reusable:
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
                        and all(
                            proof.get(name) == record[name]
                            for name in (
                                "profile",
                                "root",
                                "inputs",
                                "environment",
                                "recipe",
                                "installed_modules",
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
                    "FRONTEND_GATE: reusing this worktree's matching actual tests and build",
                    flush=True,
                )
            else:
                print(
                    "FRONTEND_GATE: changed-file lint/format, current-source TypeScript check, full frontend tests and Vite build. Type errors stop before tests; mixed/unknown inputs require the application gate.",
                    flush=True,
                )
                for argv in checks:
                    print("FRONTEND_GATE_COMMAND: " + json.dumps(argv), flush=True)
                    item = {"argv": argv, "started_ns": time.time_ns()}
                    record["checks"].append(item)
                    save()
                    result = subprocess.run(argv, cwd=root / "dashboard", check=False)
                    item["exitCode"] = result.returncode
                    save()
                    if result.returncode:
                        raise ValueError(
                            f"Frontend command failed ({result.returncode}); later checks were not run"
                        )
                    assert_stable()
            after = assert_stable()
            completed_modules = installed_modules(root)
            if completed_modules["sources"] != modules["sources"]:
                raise ValueError("Installed frontend code changed during validation")
            record["installed_modules"] = completed_modules
            record.update(inputs=after, passed=True)
            return 0
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            record["error"] = str(error)
            print("FRONTEND_GATE_REFUSED: " + str(error), file=sys.stderr, flush=True)
            return 1
        finally:
            record.update(attempt=str(attempt), seconds=time.monotonic() - started)
            save()
            print("FRONTEND_GATE_RECEIPT: " + str(attempt), flush=True)


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
        status = gate.git_launch_context(
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
