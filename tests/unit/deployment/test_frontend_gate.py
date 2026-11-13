"""Actual Git and npm processes exercise frontend selection, refusal and reuse."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3]


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root).decode().strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Frontend Gate Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "core.hooksPath", ".git/fixture-hooks")
    (root / "tools").mkdir()
    shutil.copyfile(SOURCE / "tools/precommit.py", root / "tools/precommit.py")
    shutil.copyfile(SOURCE / "tools/frontend_gate.py", root / "tools/frontend_gate.py")
    shutil.copyfile(SOURCE / "tools/test_gate.py", root / "tools/test_gate.py")
    shutil.copyfile(SOURCE / "tools/check_public_source.py", root / "tools/check_public_source.py")
    (root / ".githooks").mkdir()
    shutil.copyfile(SOURCE / ".githooks/test-gate", root / ".githooks/test-gate")
    (root / ".githooks/test-gate").chmod(0o755)
    for name, text in [
        ("dashboard/src/Example.tsx", "export const value = 1;\n"),
        ("src/octop/config.py", "VALUE = 1\n"),
    ]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "accepted fixture")
    git(root, "update-ref", "refs/remotes/origin/main", git(root, "rev-parse", "HEAD"))
    return root


def change(root, name, staged=True):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed\n", encoding="utf-8")
    if staged:
        git(root, "add", "--", name)


def invoke(root, *arguments):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(root / "tools/precommit.py"), *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


@pytest.mark.parametrize("worktree", [False, True])
def test_frontend_only_complete_candidate_selects_frontend(repository, worktree):
    change(repository, "dashboard/src/Example.tsx")
    result = invoke(repository, *(["--worktree"] if worktree else []), "--plan")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["profile"] == "frontend"
    assert plan["scope"] == "complete-branch-and-worktree"
    assert plan["commands"] == [["uv", "run", "--frozen", "python", "tools/frontend_gate.py"]]


@pytest.mark.parametrize(
    "kind",
    [
        "unstaged-backend",
        "untracked-backend",
        "committed-backend",
        "dependency",
        "frontend-config",
        "missing-main",
        "deleted-frontend",
    ],
)
def test_non_frontend_or_incomplete_input_retains_full_gate(repository, kind):
    change(repository, "dashboard/src/Example.tsx")
    if kind == "unstaged-backend":
        change(repository, "src/octop/config.py", staged=False)
    elif kind == "untracked-backend":
        change(repository, "src/octop/new.py", staged=False)
    elif kind == "committed-backend":
        change(repository, "src/octop/config.py")
        git(repository, "commit", "-qm", "unmerged backend and frontend")
        (repository / "dashboard/src/Example.tsx").write_text(
            "new frontend after backend commit\n", encoding="utf-8"
        )
        git(repository, "add", "dashboard/src/Example.tsx")
    elif kind == "dependency":
        change(repository, "dashboard/package-lock.json")
    elif kind == "frontend-config":
        change(repository, "dashboard/vite.config.ts")
    elif kind == "missing-main":
        git(repository, "update-ref", "-d", "refs/remotes/origin/main")
    elif kind == "deleted-frontend":
        (repository / "dashboard/src/Example.tsx").unlink()
        git(repository, "add", "dashboard/src/Example.tsx")
    result = invoke(repository, "--plan")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["profile"] == "full"


@pytest.mark.parametrize("kind", ["partial", "untracked-frontend"])
def test_incomplete_staging_refuses_before_commands_without_index_edit(repository, kind):
    change(repository, "dashboard/src/Example.tsx")
    if kind == "partial":
        (repository / "dashboard/src/Example.tsx").write_text(
            "different unstaged\n", encoding="utf-8"
        )
    else:
        change(repository, "dashboard/src/Untracked.tsx", staged=False)
    before = git(repository, "ls-files", "--stage")
    result = invoke(repository)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert ("unstaged" in result.stderr) if kind == "partial" else ("untracked" in result.stderr)
    assert git(repository, "ls-files", "--stage") == before
    receipt = json.loads((repository / ".git/mosswork-precommit.json").read_text(encoding="utf-8"))
    assert receipt["checks"] == []
    assert receipt["passed"] is False


@pytest.fixture
def npm_repository(repository):
    root = repository
    (root / ".gitignore").write_text(
        "dashboard/node_modules/\nsrc/octop/dashboard/\n", encoding="utf-8"
    )
    dashboard = root / "dashboard"
    (dashboard / "package.json").write_text(
        json.dumps(
            {
                "name": "private-gate-control",
                "private": True,
                "scripts": {"test": "node runner.cjs test", "build": "node runner.cjs build"},
            }
        ),
        encoding="utf-8",
    )
    (dashboard / "runner.cjs").write_text(
        """const fs=require('fs'),cp=require('child_process');
const mode=process.argv[2], control=JSON.parse(fs.readFileSync('../.git/control.json','utf8'));
fs.appendFileSync('../.git/command-trace',mode+'\\n');
if(mode==='test'&&control.source)fs.writeFileSync('public/marker.svg','mutation');
if(mode==='test'&&control.index)cp.execFileSync('git',['add','--','dashboard/public/marker.svg'],{cwd:'..'});
if(mode==='test'&&control.fail)process.exit(17);
if(mode==='build'){fs.mkdirSync('../src/octop/dashboard',{recursive:true});fs.writeFileSync('../src/octop/dashboard/index.html','built fixture');}
""",
        encoding="utf-8",
    )
    (dashboard / "node_modules").mkdir()
    (dashboard / "node_modules/.package-lock.json").write_text("{}", encoding="utf-8")
    (dashboard / "public").mkdir()
    (dashboard / "public/marker.svg").write_text("accepted", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "accepted npm fixture")
    git(root, "update-ref", "refs/remotes/origin/main", git(root, "rev-parse", "HEAD"))
    (dashboard / "public/marker.svg").write_text("candidate", encoding="utf-8")
    (root / ".git/control.json").write_text("{}", encoding="utf-8")
    return root


def frontend(root):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(root / "tools/frontend_gate.py")],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def trace(root):
    return (root / ".git/command-trace").read_text(encoding="utf-8").splitlines()


def test_actual_npm_build_output_and_same_source_reuse(npm_repository):
    root = npm_repository
    first = frontend(root)
    assert first.returncode == 0, (first.stdout, first.stderr)
    assert trace(root) == ["test", "build"]
    second = frontend(root)
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert trace(root) == ["test", "build"]
    assert "reusing" in second.stdout + second.stderr
    (root / "dashboard/public/marker.svg").write_text("changed candidate", encoding="utf-8")
    third = frontend(root)
    assert third.returncode == 0, (third.stdout, third.stderr)
    assert trace(root) == ["test", "build", "test", "build"]


def test_actual_failed_npm_stops_build_and_prevents_old_success_reuse(npm_repository):
    root = npm_repository
    assert frontend(root).returncode == 0
    (root / "dashboard/public/marker.svg").write_text("new candidate", encoding="utf-8")
    (root / ".git/control.json").write_text('{"fail":true}', encoding="utf-8")
    failed = frontend(root)
    assert failed.returncode == 1
    assert "later checks were not run" in failed.stderr
    assert trace(root) == ["test", "build", "test"]
    (root / ".git/control.json").write_text("{}", encoding="utf-8")
    repaired = frontend(root)
    assert repaired.returncode == 0, (repaired.stdout, repaired.stderr)
    assert trace(root) == ["test", "build", "test", "test", "build"]


@pytest.mark.parametrize("kind", ["source", "index"])
def test_actual_source_or_index_drift_stops_before_build(npm_repository, kind):
    root = npm_repository
    (root / ".git/control.json").write_text(json.dumps({kind: True}), encoding="utf-8")
    result = frontend(root)
    assert result.returncode == 1
    assert "changed during frontend validation" in result.stderr
    assert trace(root) == ["test"]


def test_actual_make_then_git_hook_reuses_one_frontend_result(npm_repository):
    root = npm_repository
    executable = shutil.which("make") or shutil.which("mingw32-make")
    if executable is None and os.name == "nt":
        pytest.skip("GNU Make is required; the Windows publication route supplies it")
    assert executable is not None
    make = Path(executable)
    python = shlex.quote(Path(sys.executable).as_posix())
    original_make = (SOURCE / "Makefile").read_text(encoding="utf-8")
    windows_context = original_make[
        original_make.index("# Git's Windows shell launchers") : original_make.index("REPO_ROOT")
    ]
    (root / "Makefile").write_text(
        "SHELL := /bin/bash\n"
        + windows_context
        + "\nall:\n\t"
        + python
        + " tools/frontend_gate.py\n",
        encoding="utf-8",
    )
    git(root, "add", "Makefile")
    git(root, "commit", "-qm", "accepted make fixture")
    git(root, "update-ref", "refs/remotes/origin/main", git(root, "rev-parse", "HEAD"))
    hook = root / ".git/fixture-hooks/pre-commit"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\nexec " + python + " tools/frontend_gate.py\n", encoding="utf-8")
    hook.chmod(0o755)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    environment["PATH"] = str(make.parent) + os.pathsep + environment["PATH"]
    first = subprocess.run(
        [str(make), "all"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert first.returncode == 0, (first.stdout, first.stderr)
    assert trace(root) == ["test", "build"]
    git(root, "add", "dashboard/public/marker.svg")
    second = subprocess.run(
        ["git", "commit", "-m", "actual frontend hook"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert "FRONTEND_GATE_RECEIPT:" in second.stdout + second.stderr
    assert trace(root) == ["test", "build"], (second.stdout, second.stderr)


def test_successful_empty_git_shell_cannot_replace_frontend_validation(npm_repository):
    root = npm_repository
    (root / ".githooks/test-gate").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    git(root, "add", ".githooks/test-gate")
    git(root, "commit", "-qm", "negative shell fixture")
    git(root, "update-ref", "refs/remotes/origin/main", git(root, "rev-parse", "HEAD"))
    result = frontend(root)
    assert result.returncode == 1
    assert "did not complete" in result.stderr
    assert not (root / ".git/command-trace").exists()


def test_changed_installed_module_executes_fresh_tests(npm_repository):
    root = npm_repository
    runner = root / "dashboard/runner.cjs"
    runner.write_text(
        runner.read_text(encoding="utf-8")
        + "\nif(mode==='test')fs.appendFileSync('../.git/installed-module-trace',require('./node_modules/runtime.cjs')+'\\n');\n",
        encoding="utf-8",
    )
    git(root, "add", "dashboard/runner.cjs")
    git(root, "commit", "-qm", "accepted module fixture")
    git(root, "update-ref", "refs/remotes/origin/main", git(root, "rev-parse", "HEAD"))
    module = root / "dashboard/node_modules/runtime.cjs"
    module.write_text("module.exports='original';\n", encoding="utf-8")
    first = frontend(root)
    assert first.returncode == 0, (first.stdout, first.stderr)
    module.write_text("module.exports='changed';\n", encoding="utf-8")
    second = frontend(root)
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert (root / ".git/installed-module-trace").read_text(encoding="utf-8").splitlines() == [
        "original",
        "changed",
    ]
    assert trace(root) == ["test", "build", "test", "build"]


@pytest.mark.parametrize("kind", ["missing-original", "modified-original", "invalid-history"])
def test_missing_or_corrupt_actual_proof_runs_fresh_checks(npm_repository, kind):
    root = npm_repository
    first = frontend(root)
    assert first.returncode == 0, (first.stdout, first.stderr)
    storage = root / ".git/mosswork-frontend-gate"
    original = json.loads((storage / "history.json").read_text(encoding="utf-8"))["attempt"]
    # Exercise reuse of a reused result too: it must retain the original checks.
    second = frontend(root)
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert trace(root) == ["test", "build"]
    if kind == "missing-original":
        Path(original).unlink()
    elif kind == "modified-original":
        Path(original).write_text("{}", encoding="utf-8")
    else:
        (storage / "history.json").write_text("[]", encoding="utf-8")
    third = frontend(root)
    assert third.returncode == 0, (third.stdout, third.stderr)
    assert trace(root) == ["test", "build", "test", "build"]
