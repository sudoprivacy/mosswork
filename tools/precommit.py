"""Run scoped documentation/tool/frontend checks, or request the application gate.

Exit 3 means the caller must execute its existing application checks.
Validation failures always exit 1 and must never select that fallback.
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
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict

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
    "tools/frontend_gate.py",
    "tools/test_gate.py",
    "tools/check_public_source.py",
    "tools/export_public_source.py",
    "tools/accept_sudograph_worker.py",
    "tools/ci_identity_reuse.py",
    "tests/unit/deployment/test_precommit.py",
    "tests/unit/deployment/test_test_gate.py",
    "tests/unit/deployment/test_ci_action_recipe.py",
    "tests/unit/deployment/test_hook_staging.py",
    "tests/unit/deployment/test_frontend_gate.py",
    "tests/unit/deployment/test_export_public_source.py",
    "tests/unit/deployment/test_ci_identity_reuse.py",
    "tests/unit/deployment/test_make_format_gate.py",
    "tests/unit/deployment/test_make_testmon.py",
)
TOOL_SCOPE = frozenset(TOOL_FILES) | {
    ".githooks/pre-commit",
    ".github/workflows/mosswork-ci.yml",
    ".github/workflows/mosswork-dashboard-ci.yml",
    "AGENTS.md",
    "Makefile",
}
GIT_CONTEXT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX")
RELEASE_ONLY_INPUTS = ROOT_DOCS | {
    "AGENTS.md",
    "Makefile",
    ".githooks/pre-commit",
    "tests/unit/deployment/test_make_format_gate.py",
    "tests/unit/deployment/test_make_testmon.py",
}
WINDOWS_VALIDATION_INPUTS = frozenset(
    {
        "tools/precommit.py",
        "tools/frontend_gate.py",
        "tests/unit/deployment/test_frontend_gate.py",
        "tools/check_public_source.py",
        "tools/export_public_source.py",
        "tests/unit/deployment/test_precommit.py",
        "tests/unit/deployment/test_export_public_source.py",
        "tools/ci_identity_reuse.py",
        "tests/unit/deployment/test_ci_identity_reuse.py",
    }
)
WINDOWS_VALIDATION_RECIPE_SHA256 = (
    "824241afb0a9ca0b5cd27361b7c5b25b2d8b522daeb3beb141b55d5d1b86fb29"
)
WINDOWS_VALIDATION_RECIPE_ACTIONS = (".github/actions/canonical-product-checks/action.yml",)


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.check_output(["git", *arguments], cwd=root)


def paths(data: bytes) -> list[str]:
    return [item.decode("utf-8", errors="surrogateescape") for item in data.split(b"\0") if item]


class CiPlan(TypedDict):
    runtime_matrix: bool
    windows_validation: bool
    validator_regressions: bool
    checked_head: str
    paths: list[str]
    reason: str


def validator_regressions_required(
    root: Path, head: str, changed: list[str], environment: dict[str, str]
) -> bool:
    """Pure product Python payloads do not change the private validator fixtures."""
    if not changed:
        return True
    for name in changed:
        path = PurePosixPath(name)
        product = name.startswith("src/octop/") or (
            len(path.parts) >= 4 and path.parts[0] == "packages" and path.parts[2] == "src"
        )
        product_test = name.startswith(("tests/unit/", "tests/integration/")) and (
            not name.startswith("tests/unit/deployment/") and path.name.startswith("test_")
        )
        if path.suffix != ".py" or path.name == "conftest.py" or not (product or product_test):
            return True
    entries = subprocess.check_output(
        ["git", "ls-tree", "-rz", head, "--", *changed], cwd=root, env=environment
    ).split(b"\0")
    present = set()
    for entry in entries:
        if not entry:
            continue
        if not entry.startswith((b"100644 ", b"100755 ")):
            return True
        present.add(entry.split(b"\t", 1)[1].decode("utf-8", errors="surrogateescape"))
    return present != set(changed)


def windows_validation_recipe(root: Path, head: str, environment: dict[str, str]) -> bool:
    """Old or changed workflows keep full runtime coverage until their route is qualified."""
    workflow = ".github/workflows/mosswork-ci.yml"
    sources = {}
    for name in (workflow, *WINDOWS_VALIDATION_RECIPE_ACTIONS):
        entry = subprocess.run(
            ["git", "ls-tree", "-z", head, "--", name],
            cwd=root,
            env=environment,
            capture_output=True,
            check=False,
        )
        if entry.returncode or entry.stdout.count(b"\0") != 1:
            return False
        metadata, separator, path = entry.stdout.removesuffix(b"\0").partition(b"\t")
        if (
            not separator
            or path != name.encode("utf-8")
            or not metadata.startswith((b"100644 blob ", b"100755 blob "))
        ):
            return False
        result = subprocess.run(
            ["git", "show", head + ":" + name],
            cwd=root,
            env=environment,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            return False
        try:
            sources[name] = result.stdout.decode("utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError:
            return False
    source = sources[workflow]
    blocks = []
    for job in ("ci-scope", "release-checks"):
        if len(re.findall(r"(?m)^  " + job + r":", source)) != 1:
            return False
        block = re.search(r"(?ms)^  " + job + r":\n.*?(?=^  [A-Za-z0-9_-]+:|\Z)", source)
        if block is None:
            return False
        blocks.append((block.group(0).rstrip() + "\n").encode("utf-8"))
    # Bind the complete shared matrix and the local actions it executes together.
    blocks.extend(
        (name + "\0" + sources[name]).encode("utf-8") for name in WINDOWS_VALIDATION_RECIPE_ACTIONS
    )
    return hashlib.sha256(b"\0".join(blocks)).hexdigest() == WINDOWS_VALIDATION_RECIPE_SHA256


def ci_check_matrix(plan: CiPlan) -> str:
    """Keep required checks in one fail-fast matrix with their original budgets."""
    checks = [
        {
            "suite": "release",
            "os": "ubuntu-24.04",
            "check_name": "release-checks",
            "timeout_minutes": 360,
        }
    ]
    if plan["runtime_matrix"]:
        checks.extend(
            {
                "suite": "acp",
                "os": platform,
                "check_name": f"ACP runtime observations ({platform})",
                "timeout_minutes": 20,
            }
            for platform in ("ubuntu-24.04", "windows-2022", "macos-15-intel")
        )
        checks.extend(
            {
                "suite": "interop",
                "os": platform,
                "check_name": f"Packaged Sudograph connector interoperability ({platform})",
                "timeout_minutes": 360,
            }
            for platform in ("ubuntu-24.04", "windows-2022")
        )
    if plan["windows_validation"]:
        checks.append(
            {
                "suite": "tools",
                "os": "windows-2022",
                "check_name": "Windows validation tools",
                "timeout_minutes": 10,
            }
        )
    return json.dumps({"include": checks}, separators=(",", ":"))


def ci_plan(root: Path, base: str, head: str) -> CiPlan:
    """Select from the checked composition after proving both supplied commits."""
    plan: CiPlan = {
        "runtime_matrix": True,
        "windows_validation": False,
        "validator_regressions": True,
        "checked_head": "",
        "paths": [],
        "reason": "No PR diff; run both runtime matrices",
    }
    if not base and not head:
        return plan
    if not all(re.fullmatch(r"[a-f0-9]{40}", value) for value in (base, head)):
        raise ValueError("CI scope requires two full commit SHAs, or two empty dispatch inputs")
    environment = {key: value for key, value in os.environ.items() if key not in GIT_CONTEXT}
    checked = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=root,
        env=environment,
        capture_output=True,
        check=False,
    )
    if checked.returncode:
        plan["reason"] = "Checked source unavailable; run both runtime matrices"
        return plan
    checkout = checked.stdout.decode().strip()
    plan["checked_head"] = checkout
    for revision in (base, head):
        result = subprocess.run(
            ["git", "cat-file", "-e", revision + "^{commit}"],
            cwd=root,
            env=environment,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            plan["reason"] = "Git endpoint unavailable; run both runtime matrices"
            return plan
    if base == head:
        return plan
    for revision in (base, head):
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", revision, checkout],
            cwd=root,
            env=environment,
            capture_output=True,
            check=False,
        )
        if ancestor.returncode:
            plan["reason"] = (
                "Checked source does not prove both CI input commits; run both runtime matrices"
            )
            return plan
    changed = paths(
        subprocess.check_output(
            ["git", "diff", "--no-renames", "--name-only", "-z", base, checkout, "--"],
            cwd=root,
            env=environment,
        )
    )
    plan["paths"] = changed
    plan["validator_regressions"] = validator_regressions_required(
        root, checkout, changed, environment
    )
    if not changed or any(
        path not in RELEASE_ONLY_INPUTS | WINDOWS_VALIDATION_INPUTS for path in changed
    ):
        plan["reason"] = (
            "Runtime, dependency, workflow or unclassified input; run both runtime matrices"
        )
        return plan
    entries = subprocess.check_output(
        ["git", "ls-tree", "-rz", checkout, "--", *changed], cwd=root, env=environment
    )
    if any(
        entry and not entry.startswith((b"100644 ", b"100755 ")) for entry in entries.split(b"\0")
    ):
        plan["reason"] = "Nonregular Git input; run both runtime matrices"
        return plan
    present = {
        entry.split(b"\t", 1)[1].decode("utf-8", errors="surrogateescape")
        for entry in entries.split(b"\0")
        if entry
    }
    if any(path in WINDOWS_VALIDATION_INPUTS and path not in present for path in changed):
        plan["reason"] = "Validation tool removed; run both runtime matrices"
        return plan
    needs_windows = any(path in WINDOWS_VALIDATION_INPUTS for path in changed)
    if needs_windows and not windows_validation_recipe(root, checkout, environment):
        plan["reason"] = (
            "Windows tool workflow recipe unavailable or changed; run both runtime matrices"
        )
        return plan
    plan["runtime_matrix"] = False
    plan["windows_validation"] = needs_windows
    plan["reason"] = (
        "Independent validation tools; run existing Linux and Windows validators and complete-source exports"
        if plan["windows_validation"]
        else "Validation-only inputs; release checks remain required"
    )
    return plan


def staged_paths(root: Path) -> list[str]:
    # Treat renames as deletion + addition, so moving runtime code into docs
    # cannot hide the original path. Include deletions and NUL-delimited names.
    return paths(git(root, "diff", "--cached", "--name-only", "--no-renames", "-z"))


def worktree_paths(root: Path) -> tuple[list[str], str | None, str]:
    """Include every pending commit, staged/unstaged edit and untracked source."""
    result = subprocess.run(
        ["git", "merge-base", "HEAD", "refs/remotes/origin/main"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        return [], None, "No accepted-main ancestor is available; retain full application checks"
    base = result.stdout.decode().strip()
    # Disable diff's silent index refresh only for this read. Numstat compares
    # contents; name-only can otherwise report unchanged, stat-dirty files.
    changed_files = git(
        root,
        "-c",
        "diff.autoRefreshIndex=false",
        "diff",
        "--numstat",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "-z",
        base,
        "--",
    )
    changed = sorted(
        {
            item.split(b"\t", 2)[2].decode("utf-8", errors="surrogateescape")
            for item in changed_files.split(b"\0")
            if item
        }
        | set(paths(git(root, "ls-files", "--others", "--exclude-standard", "-z")))
    )
    return changed, base, "Complete branch and working-tree diff against the accepted-main ancestor"


def worktree_helper() -> Any:
    """Load the existing stdlib-only input and graph helpers."""
    source = Path(__file__).with_name("test_gate.py")
    spec = importlib.util.spec_from_file_location("mosswork_worktree_inputs", source)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def worktree_inputs(root: Path) -> dict[str, str]:
    """Use the existing gate's complete raw-source snapshot, including untracked files."""
    return dict(worktree_helper().inputs(root))


def worker_pool_mismatch(root: Path) -> tuple[str, str, str, bool] | None:
    """Diagnose a pool-only reset when existing Python history remains usable.

    This only refuses duplicate work. It never selects tests or accepts a graph;
    the original test gate still performs every reuse and execution check.
    """
    try:
        requested = os.environ.get("PYTEST_JOBS", "2")
        if not requested.isdecimal() or int(requested) < 1:
            return None
        makefile = root / "Makefile"
        if makefile.is_symlink() or not re.search(
            r"(?m)^PYTEST_JOBS \?= 2$", makefile.read_text(encoding="utf-8")
        ):
            return None
        storage = Path(git(root, "rev-parse", "--absolute-git-dir").decode().strip()) / (
            "mosswork-test-gate"
        )
        history = storage / "history.json"
        if storage.is_symlink() or history.is_symlink() or (storage / "incomplete.json").exists():
            return None
        previous = json.loads(history.read_text(encoding="utf-8"))
        if (
            not isinstance(previous, dict)
            or previous.get("schema") != 2
            or previous.get("passed") is not True
            or previous.get("root") != str(root.resolve())
        ):
            return None
        argv = previous["argv"]
        if not isinstance(argv, list) or argv.count("-n") != 1:
            return None
        recorded = argv[argv.index("-n") + 1]
        if (
            not isinstance(recorded, str)
            or not recorded.isdecimal()
            or int(recorded) < 1
            or recorded == requested
        ):
            return None
        original = Path(previous["attempt"])
        if original.is_symlink() or original.parent.resolve() != storage.resolve():
            return None
        proof = json.loads(original.read_text(encoding="utf-8"))
        if not isinstance(proof, dict):
            return None
        # The producer adds elapsed seconds after writing its history pointer.
        pointer = dict(previous)
        proof.pop("seconds", None)
        pointer.pop("seconds", None)
        if proof != pointer:
            return None
        helper = worktree_helper()
        graph = storage / previous["graph"]
        if graph.parent.resolve() != storage.resolve():
            return None
        actual_cache = helper.cache_digest(graph)
        if actual_cache is None or actual_cache != previous.get("cache_digest"):
            return None
        previous_inputs = previous.get("inputs")
        if not isinstance(previous_inputs, dict):
            return None
        current_inputs = helper.inputs(root)
        same_source = current_inputs == previous_inputs
        python_only = getattr(helper, "python_only", None)
        if not same_source and (
            not callable(python_only) or not python_only(previous_inputs, current_inputs)
        ):
            return None
        environment = {key: value for key, value in os.environ.items() if key not in GIT_CONTEXT}
        inner = """import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('mosswork_pool_probe', Path('tools/test_gate.py'))
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
helper.normalize_launch_environment()
print('MOSSWORK_POOL_ENVIRONMENT: ' + json.dumps(dict(environment=helper.environment(sys.argv[1]), variables=helper.environment_variables())))
"""
        # Metadata must cross the same Git shell as the actual test gate. Merely
        # normalizing PATH in a native Python process misses MSYS variables.
        probe = (
            "import json,os,subprocess,sys\n"
            "from pathlib import Path\n"
            f"code={inner!r}\n"
            "if not Path('.githooks/test-gate').is_file(): raise SystemExit(1)\n"
            "for name in ('GIT_AUTHOR_DATE','GIT_AUTHOR_EMAIL','GIT_AUTHOR_NAME','GIT_EDITOR'):\n"
            "    os.environ.pop(name,None)\n"
            "help_result=subprocess.run(['git','hook','run','-h'],capture_output=True)\n"
            "argv=['git','-c','core.hooksPath=.githooks','hook','run']\n"
            "if b'--allow-unknown-hook-name' in help_result.stdout+help_result.stderr:\n"
            "    argv.append('--allow-unknown-hook-name')\n"
            "argv+=['test-gate','--',sys.executable,'-c',code,sys.argv[1]]\n"
            "result=subprocess.run(argv,capture_output=True,text=True,encoding='utf-8')\n"
            "prefix='MOSSWORK_POOL_ENVIRONMENT: '\n"
            "records=[line[len(prefix):] for line in (result.stdout+result.stderr).splitlines() "
            "if line.startswith(prefix)]\n"
            "if result.returncode or len(records)!=1: raise SystemExit(1)\n"
            "print(records[0])\n"
        )
        result = subprocess.run(
            ["uv", "run", "--frozen", "python", "-c", probe, recorded],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        if result.returncode:
            return None
        current = json.loads(result.stdout)
        if current["environment"] == previous.get("environment"):
            return recorded, requested, str(original), same_source
        previous_variables = previous.get("environment_variables", {})
        changed = sorted(
            name
            for name in current["variables"].keys() | previous_variables.keys()
            if current["variables"].get(name) != previous_variables.get(name)
        )
        print(
            "[pre-commit] Pool history also has a different environment: "
            + (", ".join(changed) if changed else "Python or installed packages")
            + "; original validation remains required.",
            flush=True,
        )
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError):
        pass  # Unproven metadata retains the original full validation route.
    return None


def documentation(path: str) -> bool:
    p = PurePosixPath(path)
    return path in ROOT_DOCS or (
        p.parts[0] == "docs" and p.suffix.lower() in {".md", ".png", ".jpg", ".jpeg", ".svg"}
    )


def frontend_only(root: Path, changed: list[str]) -> bool:
    """Only regular authored frontend files can select the frontend gate."""
    source = root / "tools/frontend_gate.py"
    if not source.is_file() or source.is_symlink() or not changed:
        return False
    spec = importlib.util.spec_from_file_location("mosswork_frontend_paths", source)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    accepts_path = getattr(helper, "frontend_path", None)
    if not callable(accepts_path):
        return False
    for name in changed:
        path = root / name
        if not accepts_path(name) or path.is_symlink() or not path.is_file():
            return False
        if not path.resolve().is_relative_to(root.resolve()):
            return False
    return True


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
    if frontend_only(root, changed):
        return "frontend"
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


def commands(selected: str, changed: list[str] | None = None) -> list[list[str]]:
    if selected == "frontend":
        return [["uv", "run", "--frozen", "python", "tools/frontend_gate.py"]]
    if selected != "tools":
        return []
    tests = [
        "tests/unit/deployment/test_precommit.py",
        "tests/unit/deployment/test_ci_action_recipe.py",
        "tests/unit/deployment/test_hook_staging.py",
        "tests/unit/deployment/test_export_public_source.py",
        "tests/unit/deployment/test_ci_identity_reuse.py",
    ]
    if changed is None or set(changed) & {
        "tools/test_gate.py",
        "tests/unit/deployment/test_test_gate.py",
    }:
        tests.append("tests/unit/deployment/test_test_gate.py")
    if changed is None or set(changed) & {
        "tools/frontend_gate.py",
        "tools/precommit.py",
        "tests/unit/deployment/test_frontend_gate.py",
        ".github/workflows/mosswork-dashboard-ci.yml",
        "Makefile",
    }:
        tests.append("tests/unit/deployment/test_frontend_gate.py")
    if changed is None or set(changed) & {
        "Makefile",
        "tools/precommit.py",
        ".githooks/pre-commit",
        ".github/workflows/mosswork-ci.yml",
        "tests/unit/deployment/test_make_format_gate.py",
        "tests/unit/deployment/test_make_testmon.py",
    }:
        tests += [
            "tests/unit/deployment/test_make_format_gate.py",
            "tests/unit/deployment/test_make_testmon.py",
        ]
    return [
        ["uv", "run", "--frozen", "ruff", "check", *TOOL_FILES],
        ["uv", "run", "--frozen", "ruff", "format", "--check", *TOOL_FILES],
        [
            "uv",
            "run",
            "--frozen",
            "mypy",
            "--strict",
            "tools/ci_identity_reuse.py",
            "tools/test_gate.py",
        ],
        [
            "uv",
            "run",
            "--frozen",
            "pytest",
            *tests,
            "-q",
        ],
    ]


def references(root: Path, changed: list[str]) -> list[str]:
    source = Path(__file__).with_name("check_public_source.py")
    spec = importlib.util.spec_from_file_location("mosswork_source_checks", source)
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    errors: list[str] = checker.check_references(
        root, tuple(root / path for path in changed if path.endswith(".md"))
    )
    return errors


def run_commands(root: Path, checks: list[list[str]], results: list[dict[str, Any]]) -> None:
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


def default_git_index(root: Path) -> bool:
    """Keep alternate-index commits on the original fresh-check path."""
    try:
        argv = ["git", "rev-parse", "--path-format=absolute", "--git-path", "index"]
        clean = {key: value for key, value in os.environ.items() if key not in GIT_CONTEXT}
        current = subprocess.check_output(argv, cwd=root).decode().strip()
        default = subprocess.check_output(argv, cwd=root, env=clean).decode().strip()
        return Path(current).resolve() == Path(default).resolve()
    except (OSError, ValueError, subprocess.CalledProcessError):
        return False


def independent_tool_checks(
    root: Path,
    changed: list[str],
    args: argparse.Namespace,
    result: dict[str, Any],
    receipt: Path,
) -> bool:
    """Use the existing exact-input proof engine in a frozen Git launch context."""
    helper_path = root / "tools/test_gate.py"
    engine_path = root / "tools/frontend_gate.py"
    if not helper_path.is_file() or not engine_path.is_file() or not default_git_index(root):
        print(
            "TOOLS_GATE: unproven launch/index; original fresh tool checks remain required",
            flush=True,
        )
        return False
    helper = worktree_helper()
    options = {"profile": "tools", "worktree": args.worktree, "commands": result["commands"]}
    if not args.git_context:
        status = helper.git_launch_context(root, sys.argv[1:], options, entry="tools/precommit.py")
        if status is None:
            return False
        if status:
            raise ValueError(
                f"Original tools validation refused ({status}); retain its receipt above"
            )
        child_result = json.loads(receipt.read_text(encoding="utf-8"))
        if (
            not isinstance(child_result, dict)
            or child_result.get("profile") != "tools"
            or child_result.get("passed") is not True
            or child_result.get("commands") != result["commands"]
            or child_result.get("candidate_digest") != candidate(root)
        ):
            raise ValueError(
                "The original tools launch did not publish this candidate's actual result"
            )
        result.update(child_result)
        return True
    spec = importlib.util.spec_from_file_location("mosswork_exact_tool_validation", engine_path)
    assert spec is not None and spec.loader is not None
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    try:
        engine.tool_modules()
    except (OSError, ValueError) as error:
        print("TOOLS_GATE: " + str(error) + "; execute original fresh checks", flush=True)
        return False
    complete, base, _ = worktree_paths(root)
    indexed_source = args.worktree or (
        not helper.git(root, "diff", "--name-only", "--no-ext-diff", "--no-textconv", "-z")
        and not helper.git(root, "ls-files", "--others", "--exclude-standard", "-z")
    )
    eligible = base is not None and profile(root, complete) == "tools" and indexed_source
    actual: dict[str, Any] = {}
    status = engine.validate(
        root,
        changed,
        helper,
        tool_checks=result["commands"],
        reuse_allowed=eligible,
        outcome=actual,
    )
    original = Path(actual["attempt"])
    original_bytes = original.read_bytes()
    if json.loads(original_bytes) != actual:
        raise ValueError("The actual tools receipt changed; retain it and validate again")
    result["tool_validation"] = {
        "receipt": str(original),
        "sha256": hashlib.sha256(original_bytes).hexdigest(),
        "reused_from": actual.get("reused"),
        "checks_executed": len(actual["checks"]),
    }
    result["checks"].extend(
        {"argv": item["argv"], "returncode": item["exitCode"]}
        for item in actual["checks"]
        if "exitCode" in item
    )
    if status:
        raise ValueError("Original tool checks failed; retain the actual TOOLS_GATE_RECEIPT")
    return True


def ci_checkout(root: Path) -> str:
    """Require a clean CI checkout without refreshing or borrowing a Git index."""
    environment = {key: value for key, value in os.environ.items() if key not in GIT_CONTEXT}
    status = subprocess.check_output(
        ["git", "--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
        env=environment,
    )
    if status:
        raise ValueError(
            "CI checkout changed: "
            + ascii(paths(status))
            + ". Retain the logs and run the workflow on a clean commit."
        )
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, env=environment)
    return hashlib.sha256(
        head
        + b"\0"
        + subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=root, env=environment)
    ).hexdigest()


def ci_tools(root: Path, base: str, head: str) -> None:
    """Run the existing tool validators and a complete export on the selected CI platform."""
    selection = ci_plan(root, base, head)
    print("CI_SCOPE: " + json.dumps(selection, ensure_ascii=True), flush=True)
    if selection["runtime_matrix"] or not selection["windows_validation"]:
        raise ValueError(
            "This diff does not select independent Windows tool validation. "
            "Run the normal workflow; CI_SCOPE above explains its required checks."
        )
    if not all((root / path).is_file() for path in TOOL_FILES):
        raise ValueError(
            "Required tool validators are missing; restore the complete committed source"
        )
    before = ci_checkout(root)
    parent = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())).resolve()
    if parent.is_relative_to(root.resolve()):
        raise ValueError("Set RUNNER_TEMP outside the checkout to retain validation outputs")
    output = Path(tempfile.mkdtemp(prefix="mosswork-ci-tools-", dir=parent))
    result: dict[str, Any] = {
        **selection,
        "profile": (
            "windows-validation-tools"
            if sys.platform == "win32"
            else sys.platform + "-validation-tools"
        ),
        "platform": sys.platform,
        "passed": False,
        "checks": [],
    }
    started = time.monotonic()
    try:
        public = output / "public-source"
        checks = commands("tools", selection["paths"]) + [
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "tools/export_public_source.py",
                "--output",
                str(public),
            ],
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "tools/check_public_source.py",
                "--root",
                str(public),
            ],
        ]
        for check in checks:
            if check[:4] == ["uv", "run", "--frozen", "pytest"]:
                check = [*check, "-n", "2"]
            run_commands(root, [check], result["checks"])
            if ci_checkout(root) != before:
                raise ValueError(
                    "The CI source index changed during validation; retain the failed receipt"
                )
        result.update(passed=True, candidate_digest=before)
        print(
            "CI_TOOLS: existing tool checks and complete current-source export passed on "
            + sys.platform,
            flush=True,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        result["error"] = str(error)
        raise
    finally:
        result["elapsed_seconds"] = time.monotonic() - started
        receipt = output / "receipt.json"
        receipt.write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        print("CI_TOOLS_RECEIPT: " + str(receipt), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-context", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--launch-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--worktree",
        action="store_true",
        help="Validate the complete branch and working tree for make all",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="Show selection without running checks")
    mode.add_argument(
        "--ci-plan",
        nargs=2,
        metavar=("BASE", "HEAD"),
        help="Plan hosted runtime matrices from committed Git inputs",
    )
    mode.add_argument(
        "--ci-tools",
        nargs=2,
        metavar=("BASE", "HEAD"),
        help="Run existing Windows tool validators for an eligible committed diff",
    )
    args = parser.parse_args()
    if args.git_context != (args.launch_id is not None) or (
        args.launch_id is not None
        and (
            re.fullmatch(r"[a-f0-9]{32}", args.launch_id) is None
            or args.plan
            or args.ci_plan is not None
            or args.ci_tools is not None
        )
    ):
        parser.error("Invalid internal tools launch identity")
    if args.worktree and (args.ci_plan is not None or args.ci_tools is not None):
        parser.error(
            "--worktree is local validation; use --ci-plan/--ci-tools for committed CI inputs"
        )
    environment = dict(os.environ)
    if args.worktree or args.ci_plan is not None or args.ci_tools is not None:
        for name in GIT_CONTEXT:
            environment.pop(name, None)
    root = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True, encoding="utf-8", env=environment
        ).strip()
    )
    if args.ci_tools is not None:
        try:
            ci_tools(root, *args.ci_tools)
            return 0
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            print(f"CI_TOOLS_REFUSED: {error}", file=sys.stderr, flush=True)
            return 1
    if args.ci_plan is not None:
        try:
            ci_selection = ci_plan(root, *args.ci_plan)
            print("CI_SCOPE: " + json.dumps(ci_selection, ensure_ascii=True), flush=True)
            if output := os.environ.get("GITHUB_OUTPUT"):
                with open(output, "a", encoding="utf-8") as stream:
                    stream.write(
                        "runtime_matrix="
                        + str(ci_selection["runtime_matrix"]).lower()
                        + "\n"
                        + "windows_validation="
                        + str(ci_selection["windows_validation"]).lower()
                        + "\nvalidator_regressions="
                        + str(ci_selection["validator_regressions"]).lower()
                        + "\ncheck_matrix="
                        + ci_check_matrix(ci_selection)
                        + "\n"
                    )
            return 0
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            print(f"CI_SCOPE_REFUSED: {error}", file=sys.stderr, flush=True)
            return 1
    baseline = None
    reason = "Staged commit candidate"
    snapshot = None
    if args.worktree:
        # This validator is a child process. Its complete worktree must never
        # borrow a caller's temporary Git index; keep Git policy/auth settings.
        for name in GIT_CONTEXT:
            os.environ.pop(name, None)
        changed, baseline, reason = worktree_paths(root)
    else:
        changed = staged_paths(root)
    selected = profile(root, changed)
    if args.git_context and selected != "tools":
        parser.error("The internal tools launch requires an actual independent tools candidate")
    if selected == "frontend":
        complete, frontend_base, frontend_reason = worktree_paths(root)
        if frontend_base is None or not frontend_only(root, complete):
            selected = "full"
        else:
            changed = complete
            baseline, reason = frontend_base, frontend_reason
            snapshot = worktree_inputs(root)
    if args.worktree:
        if baseline is None or not changed:
            selected = "full"
        elif selected != "full":
            try:
                snapshot = worktree_inputs(root)
            except ValueError as error:
                selected = "full"
                reason = str(error) + "; retain full application checks"
    plan: dict[str, Any] = {
        "profile": selected,
        "paths": changed,
        "commands": commands(selected, changed),
    }
    if args.worktree:
        plan.update(baseline=baseline, scope="complete-branch-and-worktree", reason=reason)
    if selected == "full":
        plan["delegated_commands"] = [
            {"cwd": ".", "argv": ["make", "precommit"]},
            {"cwd": "dashboard", "argv": ["npm", "run", "build"]},
        ]
        plan["test_selection"] = {
            "mode": "pytest-testmon",
            "recorded_by": ["make test", "make all"],
            "scope": "This worktree's real dependency graph; missing or changed data may select the full suite",
        }
        if args.worktree:
            plan["delegated_commands"] = [{"cwd": ".", "argv": ["make", "validate-application"]}]
    if selected == "frontend":
        plan.update(
            baseline=baseline,
            scope="complete-branch-and-worktree",
            reason=reason,
            test_selection={
                "mode": "full-frontend",
                "scope": "All frontend tests and build; backend tests are outside this frontend-only profile",
            },
        )
    if args.plan:
        print(json.dumps(plan, ensure_ascii=True))
        return 0
    receipt = Path(git(root, "rev-parse", "--absolute-git-dir").decode().strip()) / (
        "mosswork-worktree-validation.json" if args.worktree else "mosswork-precommit.json"
    )
    result: dict[str, Any] = {**plan, "passed": False, "status": "running", "checks": []}
    started = time.monotonic()
    try:
        if not args.worktree:
            assert_staged_content(root, changed)
            if selected == "frontend" and paths(
                git(root, "ls-files", "--others", "--exclude-standard", "-z")
            ):
                raise ValueError(
                    "Stage every intended frontend input before committing; untracked source cannot qualify the indexed candidate"
                )
        else:
            print("WORKTREE_VALIDATION: " + json.dumps(plan, ensure_ascii=True), flush=True)
        if selected == "full":
            if mismatch := worker_pool_mismatch(root):
                recorded, requested, original, same_source = mismatch
                source_scope = "unchanged source" if same_source else "eligible Python changes"
                raise ValueError(
                    f"No tests ran: {source_scope} and verified history at {original} "
                    f"used {recorded} worker(s), but this command requests {requested}. "
                    f"Keep PYTEST_JOBS={recorded} process-locally through Make and the commit "
                    "to reuse the original checked history. If the pool change is intentional, "
                    f"run make test-current PYTEST_JOBS={requested} to execute and record the "
                    "required full suite with that pool, then retry. No graph was copied or accepted. "
                    "This refusal does not accept current source changes; new, changed, failed "
                    "and unexecuted cases remain required."
                )
            if args.worktree:
                print(
                    "[all] Full application validation is required: original lint, frontend formatting, "
                    "typecheck and non-live tests. No tool-only result substitutes for it.",
                    flush=True,
                )
                result.update(status="requires_full_gate", passed=None, delegated_to_full_gate=True)
                return FULL_CHECKS
            print(
                "[pre-commit] Application gate: make precommit, then (cd dashboard && npm run build)",
                flush=True,
            )
            print(
                "[pre-commit] pytest-testmon evaluates this worktree's real dependency graph; "
                "missing or changed data may select the full suite. Existing same-environment "
                "recordings from make test/make all are reused automatically.",
                flush=True,
            )
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
        handled = selected == "tools" and independent_tool_checks(
            root, changed, args, result, receipt
        )
        if not handled:
            run_commands(root, commands(selected, changed), result["checks"])
        if selected == "frontend":
            after = worktree_inputs(root)
            assert snapshot is not None

            def authored(values: dict[str, str]) -> dict[str, str]:
                return {
                    name: value
                    for name, value in values.items()
                    if not name.startswith("src/octop/dashboard/")
                }

            if authored(after) != authored(snapshot):
                raise ValueError("Authored source changed during frontend validation")
            snapshot = after
        if args.worktree:
            if worktree_inputs(root) != snapshot:
                raise ValueError(
                    "Working-tree source changed during validation; retain this failure and rerun"
                )
        else:
            assert_staged_content(root, changed)
        if candidate(root) != before:
            raise ValueError("The staged candidate changed during validation; rerun the hook")
        result["passed"] = True
        result["status"] = "passed"
        result["candidate_digest"] = before
        if args.worktree:
            result["worktree_source_digest"] = hashlib.sha256(
                json.dumps(snapshot, sort_keys=True).encode()
            ).hexdigest()
        if args.worktree:
            print(
                f"[all] {selected} checks passed for the complete branch and working tree",
                flush=True,
            )
        else:
            print(f"[pre-commit] {selected} checks passed", flush=True)
        if args.launch_id is not None:
            worktree_helper().complete_git_launch(
                root,
                args.launch_id,
                {"profile": "tools", "worktree": args.worktree, "commands": result["commands"]},
                entry="tools/precommit.py",
            )
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        result["error"] = str(error)
        result["status"] = "failed"
        print(f"[pre-commit] REFUSED: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        result["elapsed_seconds"] = time.monotonic() - started
        receipt.write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        if args.worktree:
            print("WORKTREE_VALIDATION_RECEIPT: " + str(receipt), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
