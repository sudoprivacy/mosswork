"""Run independent documentation/tool checks, or request the legacy full gate.

Exit 3 means the shell hook must execute its existing make/frontend checks.
Validation failures always exit 1 and must never select that fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

FULL_CHECKS = 3
ROOT_DOCS = frozenset(
    {
        "README.md",
        "README_CN.md",
        "README_EN.md",
        "CONTRIBUTING.md",
        "CONTRIBUTING_EN.md",
        "CHANGELOG.md",
        "SECURITY.md",
    }
)
TOOL_FILES = (
    "tools/precommit.py",
    "tools/check_public_source.py",
    "tools/export_public_source.py",
    "tools/accept_sudograph_worker.py",
    "tests/unit/deployment/test_precommit.py",
    "tests/unit/deployment/test_export_public_source.py",
)
TOOL_SCOPE = frozenset(TOOL_FILES) | {
    ".githooks/pre-commit",
    ".github/workflows/mosswork-ci.yml",
    "AGENTS.md",
}
GIT_CONTEXT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX")


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.check_output(["git", *arguments], cwd=root)


def paths(data: bytes) -> list[str]:
    return [item.decode("utf-8", errors="surrogateescape") for item in data.split(b"\0") if item]


def staged_paths(root: Path) -> list[str]:
    # Treat renames as deletion + addition, so moving runtime code into docs
    # cannot hide the original path. Include deletions and NUL-delimited names.
    return paths(git(root, "diff", "--cached", "--name-only", "--no-renames", "-z"))


def documentation(path: str) -> bool:
    p = PurePosixPath(path)
    return path in ROOT_DOCS or (
        p.parts[0] == "docs" and p.suffix.lower() in {".md", ".png", ".jpg", ".jpeg", ".svg"}
    )


def profile(root: Path, changed: list[str]) -> str:
    if not changed:
        return "empty"
    index = git(root, "ls-files", "--stage", "-z")
    for item in index.split(b"\0"):
        if not item:
            continue
        metadata, name = item.split(b"\t", 1)
        if (
            name.decode("utf-8", errors="surrogateescape") in changed
            and not metadata.startswith(b"100644 ")
            and not metadata.startswith(b"100755 ")
        ):
            return "full"  # Symlinks, submodules and unresolved entries stay conservative.
    if all(documentation(path) for path in changed):
        return "docs"
    scope = TOOL_SCOPE
    if ".gitattributes" in changed:
        if git(root, "show", ":.gitattributes").strip() != b".githooks/* text eol=lf":
            return "full"
        scope = scope | {".gitattributes"}
    if all(documentation(path) or path in scope for path in changed) and all(
        (root / path).is_file() for path in TOOL_FILES
    ):
        return "tools"
    return "full"


def candidate(root: Path) -> str:
    # Fingerprint the entire indexed tree, including unchanged dependencies,
    # without writing Git objects or acquiring the parent commit's index lock.
    return hashlib.sha256(git(root, "ls-files", "--stage", "-z")).hexdigest()


def assert_staged_content(root: Path, changed: list[str]) -> None:
    partial = set(paths(git(root, "diff", "--name-only", "-z"))) & set(changed)
    if partial:
        raise ValueError(
            "Staged files also have unstaged changes: "
            + repr(sorted(partial))
            + ". Stage the intended complete files or separate the edits before committing; "
            "the hook does not stage them for you."
        )


def commands(selected: str) -> list[list[str]]:
    if selected != "tools":
        return []
    return [
        ["uv", "run", "--frozen", "ruff", "check", *TOOL_FILES],
        ["uv", "run", "--frozen", "ruff", "format", "--check", *TOOL_FILES],
        [
            "uv",
            "run",
            "--frozen",
            "pytest",
            "tests/unit/deployment/test_precommit.py",
            "tests/unit/deployment/test_export_public_source.py",
            "-q",
        ],
    ]


def references(root: Path, changed: list[str]) -> list[str]:
    source = Path(__file__).with_name("check_public_source.py")
    spec = importlib.util.spec_from_file_location("mosswork_source_checks", source)
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    return checker.check_references(
        root, tuple(root / path for path in changed if path.endswith(".md"))
    )


def run_commands(root: Path, checks: list[list[str]], results: list[dict]) -> None:
    environment = dict(os.environ)
    # Nested Git fixture repositories must not inherit the parent commit's
    # temporary index or worktree. Keep authentication and policy settings.
    for name in GIT_CONTEXT:
        environment.pop(name, None)
    for argv in checks:
        print("[pre-commit] " + repr(argv), flush=True)
        started = time.monotonic()
        result = subprocess.run(argv, cwd=root, env=environment, check=False)
        results.append(
            {
                "argv": argv,
                "returncode": result.returncode,
                "elapsed_seconds": time.monotonic() - started,
            }
        )
        if result.returncode:
            raise ValueError(
                f"Required validation exited {result.returncode}; later checks were not run"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="Show selection without running checks")
    args = parser.parse_args()
    root = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True, encoding="utf-8"
        ).strip()
    )
    changed = staged_paths(root)
    selected = profile(root, changed)
    plan = {"profile": selected, "paths": changed, "commands": commands(selected)}
    if args.plan:
        print(json.dumps(plan, ensure_ascii=True))
        return 0
    receipt = (
        Path(git(root, "rev-parse", "--absolute-git-dir").decode().strip())
        / "mosswork-precommit.json"
    )
    result = {**plan, "passed": False, "status": "running", "checks": []}
    started = time.monotonic()
    try:
        assert_staged_content(root, changed)
        if selected == "full":
            print("[pre-commit] Full make/frontend gate required for these paths", flush=True)
            result["delegated_to_full_gate"] = True
            result["passed"] = None
            result["status"] = "requires_full_gate"
            return FULL_CHECKS
        before = candidate(root)
        errors = references(root, changed) if changed else []
        if errors:
            raise ValueError("\n".join(errors))
        if changed:
            result["checks"].append({"name": "documentation and expert references", "passed": True})
        run_commands(root, commands(selected), result["checks"])
        assert_staged_content(root, changed)
        if candidate(root) != before:
            raise ValueError("The staged candidate changed during validation; rerun the hook")
        result["passed"] = True
        result["status"] = "passed"
        result["candidate_digest"] = before
        print(f"[pre-commit] {selected} checks passed", flush=True)
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        result["error"] = str(error)
        result["status"] = "failed"
        print(f"[pre-commit] REFUSED: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        result["elapsed_seconds"] = time.monotonic() - started
        receipt.write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
