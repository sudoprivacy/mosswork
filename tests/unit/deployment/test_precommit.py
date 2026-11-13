"""Real staged Git candidates and processes exercise the publication dispatcher."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("mosswork_precommit", SOURCE / "tools/precommit.py")
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def git(root: Path, *argv: str) -> str:
    return subprocess.check_output(["git", *argv], cwd=root, text=True, encoding="utf-8").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Gate Test")
    git(root, "config", "user.email", "test@example.com")
    (root / "README.md").write_text("# Guide\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-qm", "fixture")
    return root


def stage(root: Path, path: str, text: str = "# Changed\n") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git(root, "add", "--", path)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("README.md", "docs"),
        ("docs/design/new.md", "docs"),
        ("docs/涓氬姟璇存槑.md", "docs"),
        pytest.param(
            "docs/涓氬姟\n璇存槑.md",
            "docs",
            marks=pytest.mark.skipif(
                os.name == "nt", reason="Windows filenames cannot contain newlines"
            ),
        ),
        ("src/octop/infra/agents/experts/library/example/AGENTS.md", "full"),
        ("packages/moss-harness/src/moss_harness/builtin/md_files/en/AGENTS.md", "full"),
        ("src/octop/config.py", "full"),
        ("dashboard/src/App.tsx", "full"),
        ("pyproject.toml", "full"),
        ("uv.lock", "full"),
        ("tools/new_unknown.py", "full"),
        (".github/workflows/mosswork-release.yml", "full"),
    ],
)
def test_index_selection_does_not_mistake_runtime_content_for_docs(repo: Path, path, expected):
    stage(repo, path)
    assert gate.profile(repo, gate.staged_paths(repo)) == expected


def test_runtime_rename_and_deletion_cannot_enter_docs_shortcut(repo: Path) -> None:
    stage(repo, "src/runtime.md")
    git(repo, "commit", "-qm", "runtime fixture")
    (repo / "docs").mkdir()
    git(repo, "mv", "src/runtime.md", "docs/moved.md")
    assert set(gate.staged_paths(repo)) == {"src/runtime.md", "docs/moved.md"}
    assert gate.profile(repo, gate.staged_paths(repo)) == "full"
    git(repo, "commit", "-qm", "move fixture")
    stage(repo, "src/app.py")
    git(repo, "commit", "-qm", "app fixture")
    git(repo, "rm", "src/app.py")
    assert gate.profile(repo, gate.staged_paths(repo)) == "full"


def test_partial_staging_is_refused_without_modifying_index(repo: Path) -> None:
    stage(repo, "README.md", "# Staged\n")
    before = gate.candidate(repo)
    (repo / "README.md").write_text("# Unstaged\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unstaged changes"):
        gate.assert_staged_content(repo, gate.staged_paths(repo))
    assert gate.candidate(repo) == before
    assert git(repo, "show", ":README.md") == "# Staged"


@pytest.fixture
def pool_hint_repo(tmp_path):
    from tests.unit.deployment.test_test_gate import gate_repo

    def execute(root, environment, label, *args):
        # Match the production uv interpreter selection before its Git launch.
        program = (
            "import json,os,sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0,{str(SOURCE)!r})\n"
            "from tests.unit.deployment.test_test_gate import execute\n"
            "result,_,observed=execute(Path.cwd(),dict(os.environ),sys.argv[1],*sys.argv[2:])\n"
            "print(json.dumps(dict(returncode=result.returncode,stdout=result.stdout,"
            "stderr=result.stderr,observed=observed)))\n"
        )
        child = subprocess.run(
            ["uv", "run", "--frozen", "python", "-c", program, label, *args],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        actual = json.loads(child.stdout)
        result = subprocess.CompletedProcess(
            child.args, actual["returncode"], actual["stdout"], actual["stderr"]
        )
        history = root / ".git/mosswork-test-gate/history.json"
        # Git routes hook output to stderr; use the actual completed receipt.
        plan = json.loads(history.read_text(encoding="utf-8")) if result.returncode == 0 else {}
        return result, plan, actual["observed"]

    root, environment = gate_repo.__wrapped__(tmp_path)
    install_dispatcher(root)
    shutil.copyfile(SOURCE / ".githooks/test-gate", root / ".githooks/test-gate")
    (root / ".githooks/test-gate").chmod(0o755)
    environment.update(
        UV_NO_SYNC="1",
        UV_PROJECT_ENVIRONMENT=sys.prefix,
        UV_PYTHON=sys.executable,
        VIRTUAL_ENV=sys.prefix,
        PYTHONDONTWRITEBYTECODE="1",
    )
    environment.pop("PYTEST_JOBS", None)
    (root / "tests/test_live.py").write_text(
        "import pytest\nfrom pathlib import Path\n\n@pytest.mark.live\ndef test_live():\n"
        "    with Path('output/observed').open('a') as f: f.write('live\\n')\n"
        "    raise AssertionError('live remains required')\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=root, env=environment, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=" + str(root / "empty-hooks"),
            "commit",
            "-qm",
            "hint fixture",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        check=True,
    )
    source = root / "src/first.py"
    source.write_text(source.read_text() + "\n# Pending candidate\n", encoding="utf-8")
    result, plan, observed = execute(root, environment, "actual-baseline")
    assert result.returncode == 0 and plan.get("mode") == "full", result.stdout + result.stderr
    assert sorted(observed) == ["first", "second"]
    subprocess.run(["git", "add", "src/first.py"], cwd=root, env=environment, check=True)
    return root, environment, execute


def test_pool_mismatch_refuses_before_duplicate_tests_and_keeps_original_graph(pool_hint_repo):
    root, environment, execute = pool_hint_repo
    storage = root / ".git/mosswork-test-gate"
    original = {path.name: path.read_bytes() for path in storage.iterdir() if path.is_file()}
    marker = (root / "output/observed").read_bytes()
    index = git(root, "ls-files", "--stage")
    result = subprocess.run(
        [sys.executable, "tools/precommit.py"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "No tests ran" in result.stderr and "PYTEST_JOBS=1" in result.stderr
    assert "make test-current PYTEST_JOBS=2" in result.stderr
    assert (root / "output/observed").read_bytes() == marker
    assert {
        path.name: path.read_bytes() for path in storage.iterdir() if path.is_file()
    } == original
    assert git(root, "ls-files", "--stage") == index
    receipt = json.loads((root / ".git/mosswork-precommit.json").read_text())
    assert receipt["passed"] is False and receipt["checks"] == []
    same, same_plan, observed = execute(root, environment, "recorded-pool-retry")
    assert same.returncode == 0 and same_plan["mode"] == "affected" and observed == []
    live, _, observed = execute(root, environment, "live-still-required", "--include-live")
    assert live.returncode != 0 and observed == ["live"]
    assert "live remains required" in live.stdout + live.stderr
    changed, changed_plan, _ = execute(
        root, environment, "intentional-new-pool", "--workers", "2", "--full"
    )
    assert changed.returncode == 0 and changed_plan["mode"] == "full"
    # Concurrent Windows appenders can overwrite the private trace; require
    # both original test bodies to pass in the actual worker-produced report.
    cases = list(ET.parse(root / "output/intentional-new-pool.xml").iter("testcase"))
    assert sorted(case.get("name") for case in cases) == ["test_first", "test_second"]
    assert all(len(case) == 0 for case in cases)


@pytest.mark.parametrize("kind", ["source", "environment", "incomplete", "proof", "graph"])
def test_unproven_pool_history_retains_original_full_route(pool_hint_repo, kind):
    root, environment, _ = pool_hint_repo
    storage = root / ".git/mosswork-test-gate"
    if kind == "source":
        source = root / "src/first.py"
        source.write_text(source.read_text() + "# Changed after validation\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/first.py"], cwd=root, env=environment, check=True)
    elif kind == "environment":
        environment["MOSSWORK_REAL_TEST_INPUT"] = "changed"
    elif kind == "incomplete":
        (storage / "incomplete.json").write_text("{}", encoding="utf-8")
    elif kind == "proof":
        previous = json.loads((storage / "history.json").read_text())
        Path(previous["attempt"]).write_text("{}", encoding="utf-8")
    else:
        previous = json.loads((storage / "history.json").read_text())
        (storage / previous["graph"]).write_bytes(b"broken private negative fixture")
    marker = (root / "output/observed").read_bytes()
    result = subprocess.run(
        [sys.executable, "tools/precommit.py"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == gate.FULL_CHECKS, result.stdout + result.stderr
    assert "Application gate: make precommit" in result.stdout
    assert (root / "output/observed").read_bytes() == marker


def install_dispatcher(root: Path) -> None:
    for name in ("tools/precommit.py", "tools/check_public_source.py", ".githooks/pre-commit"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / name, target)
    (root / ".githooks/pre-commit").chmod(0o755)


def invoke_dispatcher(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(root / "tools/precommit.py")],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_actual_docs_dispatcher_checks_nested_links_and_preserves_candidate(repo: Path) -> None:
    install_dispatcher(repo)
    stage(repo, "docs/design/review.md", "[Guide](../../README.md#guide)\n")
    before = gate.candidate(repo)
    result = invoke_dispatcher(repo)
    assert result.returncode == 0, result.stderr
    assert gate.candidate(repo) == before
    receipt = json.loads((repo / ".git/mosswork-precommit.json").read_text(encoding="utf-8"))
    assert receipt["passed"] is True and receipt["profile"] == "docs"
    assert len(receipt["checks"]) == 1 and receipt["commands"] == []
    stage(repo, "docs/design/review.md", "[Missing](missing.md)\n")
    refused = invoke_dispatcher(repo)
    assert refused.returncode == 1 and "broken local link" in refused.stderr
    receipt = json.loads((repo / ".git/mosswork-precommit.json").read_text(encoding="utf-8"))
    assert receipt["passed"] is False


def test_partial_docs_and_unknown_source_have_distinct_failure_codes(repo: Path) -> None:
    install_dispatcher(repo)
    stage(repo, "README.md", "# New\n")
    (repo / "README.md").write_text("# Unstaged\n", encoding="utf-8")
    assert invoke_dispatcher(repo).returncode == 1
    git(repo, "add", "README.md")
    stage(repo, "src/config.py", "value = 1\n")
    result = invoke_dispatcher(repo)
    assert result.returncode == gate.FULL_CHECKS
    assert "make precommit, then (cd dashboard && npm run build)" in result.stdout
    assert "missing or changed data may select the full suite" in result.stdout
    assert "recordings from make test/make all are reused automatically" in result.stdout
    receipt = json.loads((repo / ".git/mosswork-precommit.json").read_text(encoding="utf-8"))
    assert receipt["passed"] is None and receipt["delegated_to_full_gate"] is True
    assert receipt["delegated_commands"] == [
        {"cwd": ".", "argv": ["make", "precommit"]},
        {"cwd": "dashboard", "argv": ["npm", "run", "build"]},
    ]
    assert receipt["test_selection"]["mode"] == "pytest-testmon"
    assert receipt["test_selection"]["recorded_by"] == ["make test", "make all"]
    planned = subprocess.run(
        [sys.executable, str(repo / "tools/precommit.py"), "--plan"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    plan = json.loads(planned.stdout)
    assert plan["delegated_commands"] == receipt["delegated_commands"]
    assert plan["test_selection"] == receipt["test_selection"]
    assert not plan.get("passed")


def test_failed_real_validator_stops_later_processes_and_keeps_parent_index(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage(repo, "README.md", "# Candidate\n")
    before = gate.candidate(repo)
    marker = tmp_path / "must-not-exist"
    monkeypatch.setenv("GIT_INDEX_FILE", str(repo / ".git/index"))
    # The child initializes a different repository and then fails. It must not
    # read or mutate the parent index through the inherited hook environment.
    child = tmp_path / "child.py"
    child.write_text(
        "import os, subprocess, sys\n"
        "assert 'GIT_INDEX_FILE' not in os.environ\n"
        "subprocess.run(['git', 'init', '-q', 'nested'], check=True)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    results: list[dict] = []
    with pytest.raises(ValueError, match="exited 7"):
        gate.run_commands(
            repo,
            [
                [sys.executable, str(child)],
                [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
            ],
            results,
        )
    assert len(results) == 1 and results[0]["returncode"] == 7
    assert not marker.exists() and gate.candidate(repo) == before


def test_tools_require_complete_known_validation_scope(repo: Path) -> None:
    for path in gate.TOOL_FILES:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# fixture\n", encoding="utf-8")
    git(repo, "add", *gate.TOOL_FILES)
    assert gate.profile(repo, gate.staged_paths(repo)) == "tools"
    git(repo, "commit", "-qm", "tool fixture")
    git(repo, "rm", "tools/check_public_source.py")
    assert gate.profile(repo, gate.staged_paths(repo)) == "full"


def test_only_hook_line_ending_attributes_are_scoped(repo: Path) -> None:
    for path in gate.TOOL_FILES:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# fixture\n", encoding="utf-8")
    stage(repo, ".gitattributes", ".githooks/* text eol=lf\n")
    assert gate.profile(repo, gate.staged_paths(repo)) == "tools"
    stage(repo, ".gitattributes", "* text eol=lf\n")
    assert gate.profile(repo, gate.staged_paths(repo)) == "full"


def test_candidate_change_during_real_validation_is_refused(repo, tmp_path, monkeypatch):
    install_dispatcher(repo)
    stage(repo, "README.md", "# Candidate\n")
    child = tmp_path / "change-index.py"
    child.write_text(
        "from pathlib import Path\nimport subprocess\n"
        "Path('README.md').write_text('# Changed during check\\n')\n"
        "subprocess.run(['git', 'add', 'README.md'], check=True)\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["precommit.py"])
    monkeypatch.setattr(
        gate, "commands", lambda _selected, _changed=None: [[sys.executable, str(child)]]
    )
    assert gate.main() == 1
    receipt = json.loads((repo / ".git/mosswork-precommit.json").read_text(encoding="utf-8"))
    assert receipt["passed"] is False and "candidate changed" in receipt["error"]


def test_receipt_belongs_to_linked_worktree_not_common_git_directory(repo, tmp_path) -> None:
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "linked", str(linked))
    install_dispatcher(linked)
    stage(linked, "README.md", "# Linked worktree\n")
    result = invoke_dispatcher(linked)
    assert result.returncode == 0, result.stderr
    admin = Path(git(linked, "rev-parse", "--absolute-git-dir"))
    assert (admin / "mosswork-precommit.json").is_file()
    assert not (repo / ".git/mosswork-precommit.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_docs_symlink_uses_full_gate(repo: Path) -> None:
    (repo / "docs").mkdir()
    (repo / "docs/alias.md").symlink_to("../README.md")
    git(repo, "add", "docs/alias.md")
    assert gate.profile(repo, gate.staged_paths(repo)) == "full"


def test_real_git_commit_runs_docs_hook_with_no_make_or_npm(repo: Path, monkeypatch) -> None:
    install_dispatcher(repo)
    stage(repo, "README.md", "# Reviewed\n")
    monkeypatch.setenv("UV_PYTHON", sys.executable)
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=.githooks", "commit", "-m", "checked docs"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "docs checks passed" in result.stdout + result.stderr
    assert "make precommit" not in result.stdout + result.stderr
    assert git(repo, "show", "HEAD:README.md") == "# Reviewed"


@pytest.mark.parametrize("failed_tool", ["none", "make", "npm"])
def test_full_hook_preserves_required_commands_and_refuses_real_failures(
    repo: Path,
    tmp_path: Path,
    monkeypatch,
    failed_tool: str,
) -> None:
    install_dispatcher(repo)
    (repo / "dashboard").mkdir()
    stage(repo, "src/config.py", "value = 1\n")
    before = git(repo, "rev-parse", "HEAD")
    shims = tmp_path / "validators"
    shims.mkdir()
    log = tmp_path / "commands.jsonl"
    for name in ("make", "npm"):
        shim = shims / name
        shim.write_text(
            "#!/usr/bin/env python\nimport json, os, sys\nfrom pathlib import Path\n"
            "assert 'GIT_INDEX_FILE' not in os.environ\n"
            "tool = Path(sys.argv[0]).name\n"
            "with open(os.environ['GATE_TEST_LOG'], 'a', encoding='utf-8') as log:\n"
            "    log.write(json.dumps({'tool':tool, 'argv':sys.argv[1:]})+'\\n')\n"
            "raise SystemExit(7 if tool == os.environ['GATE_TEST_FAIL'] else 0)\n",
            encoding="utf-8",
            newline="\n",
        )
        shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(shims) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("UV_PYTHON", sys.executable)
    monkeypatch.setenv("GATE_TEST_LOG", str(log))
    monkeypatch.setenv("GATE_TEST_FAIL", failed_tool)
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=.githooks", "commit", "-m", "full gate"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    expected = [{"tool": "make", "argv": ["precommit"]}]
    if failed_tool != "make":
        expected.append({"tool": "npm", "argv": ["run", "build"]})
    assert calls == expected, result.stdout + result.stderr
    if failed_tool != "none":
        assert result.returncode != 0
        assert git(repo, "rev-parse", "HEAD") == before
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert git(repo, "rev-parse", "HEAD") != before


def ci_commit(repo: Path, message: str) -> None:
    """Prepare private Git data without inheriting the outer commit's hook."""
    empty_hooks = repo / "fixture-hooks"
    empty_hooks.mkdir(exist_ok=True)
    git(repo, "-c", "core.hooksPath=" + str(empty_hooks), "commit", "-qm", message)


def seed_windows_validation_workflow(repo: Path) -> None:
    path = ".github/workflows/mosswork-ci.yml"
    stage(repo, path, (SOURCE / path).read_text(encoding="utf-8"))
    ci_commit(repo, "qualified Windows workflow fixture")


@pytest.mark.parametrize(
    "path,run_runtime",
    [
        ("Makefile", False),
        ("AGENTS.md", False),
        ("CONTRIBUTING.md", False),
        ("CONTRIBUTING_EN.md", False),
        (".githooks/pre-commit", False),
        ("tests/unit/deployment/test_make_format_gate.py", False),
        ("tests/unit/deployment/test_make_testmon.py", False),
        ("src/octop/config.py", True),
        ("packages/moss-harness/src/moss_harness/config.py", True),
        ("pyproject.toml", True),
        ("uv.lock", True),
        ("tools/precommit.py", False),
        ("tools/frontend_gate.py", False),
        ("tests/unit/deployment/test_frontend_gate.py", False),
        (".github/workflows/mosswork-ci.yml", True),
        (".github/workflows/mosswork-identity-ci.yml", True),
        ("tests/conftest.py", True),
        ("tests/unit/deployment/test_precommit.py", False),
        ("tests/unit/deployment/new_test.py", True),
        ("docs/new.md", True),
        ("unknown-input.json", True),
    ],
)
def test_real_committed_ci_scope_defaults_to_both_runtime_matrices(
    repo: Path, path: str, run_runtime: bool
) -> None:
    if path in gate.WINDOWS_VALIDATION_INPUTS:
        seed_windows_validation_workflow(repo)
    base = git(repo, "rev-parse", "HEAD")
    stage(repo, path)
    ci_commit(repo, "candidate")
    plan = gate.ci_plan(repo, base, git(repo, "rev-parse", "HEAD"))
    assert plan["runtime_matrix"] is run_runtime
    assert plan["paths"] == [path]


@pytest.mark.parametrize("operation", ["rename", "delete", "mixed"])
def test_ci_scope_cannot_hide_runtime_changes_behind_validation_inputs(
    repo: Path, operation: str
) -> None:
    stage(repo, "src/runtime.py")
    ci_commit(repo, "runtime baseline")
    base = git(repo, "rev-parse", "HEAD")
    if operation == "rename":
        git(repo, "mv", "src/runtime.py", "Makefile")
    elif operation == "delete":
        git(repo, "rm", "src/runtime.py")
    else:
        stage(repo, "src/runtime.py", "changed = True\n")
        stage(repo, "Makefile")
    ci_commit(repo, "candidate")
    plan = gate.ci_plan(repo, base, git(repo, "rev-parse", "HEAD"))
    assert plan["runtime_matrix"] is True
    assert "src/runtime.py" in plan["paths"]


def test_ci_scope_keeps_full_matrices_for_dispatch_empty_diff_and_shallow_history(
    repo: Path, tmp_path: Path
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    assert gate.ci_plan(repo, "", "")["runtime_matrix"] is True
    assert gate.ci_plan(repo, base, base)["runtime_matrix"] is True
    stage(repo, "Makefile")
    ci_commit(repo, "candidate")
    head = git(repo, "rev-parse", "HEAD")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--quiet", "--depth=1", repo.as_uri(), str(shallow)], check=True
    )
    assert gate.ci_plan(shallow, base, head)["runtime_matrix"] is True
    assert "unavailable" in gate.ci_plan(shallow, base, head)["reason"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_ci_scope_retains_runtime_matrices_for_nonregular_inputs(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "AGENTS.md").symlink_to("README.md")
    git(repo, "add", "AGENTS.md")
    ci_commit(repo, "symlink candidate")
    assert gate.ci_plan(repo, base, git(repo, "rev-parse", "HEAD"))["runtime_matrix"] is True


def test_ci_scope_cli_uses_commits_without_inheriting_or_mutating_an_index(
    repo: Path, tmp_path: Path
) -> None:
    install_dispatcher(repo)
    base = git(repo, "rev-parse", "HEAD")
    stage(repo, "Makefile")
    ci_commit(repo, "candidate")
    head = git(repo, "rev-parse", "HEAD")
    index_before = (repo / ".git/index").read_bytes()
    output = tmp_path / "github-output"
    environment = {
        **os.environ,
        "GITHUB_OUTPUT": str(output),
        "GIT_DIR": str(tmp_path / "foreign.git"),
        "GIT_INDEX_FILE": str(tmp_path / "foreign-index"),
    }
    argv = [sys.executable, str(repo / "tools/precommit.py"), "--ci-plan", base, head]
    result = subprocess.run(argv, cwd=repo, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(result.stdout.removeprefix("CI_SCOPE: "))
    assert plan["runtime_matrix"] is False and plan["paths"] == ["Makefile"]
    assert output.read_text(encoding="utf-8") == (
        "runtime_matrix=false\nwindows_validation=false\nvalidator_regressions=true\n"
        "linux_validation=false\n"
    )
    assert (repo / ".git/index").read_bytes() == index_before
    assert git(repo, "rev-parse", "HEAD") == head

    output.unlink()
    refused = subprocess.run(
        [*argv[:-2], "HEAD;echo invalid", head],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0 and "CI_SCOPE_REFUSED" in refused.stderr
    assert not output.exists()
    assert (repo / ".git/index").read_bytes() == index_before


@pytest.mark.parametrize("path", sorted(gate.WINDOWS_VALIDATION_INPUTS))
def test_committed_validation_tools_keep_windows_checks_without_runtime_jobs(
    repo: Path, path: str
) -> None:
    seed_windows_validation_workflow(repo)
    base = git(repo, "rev-parse", "HEAD")
    stage(repo, path)
    stage(repo, "CONTRIBUTING.md")
    ci_commit(repo, "independent tool candidate")
    plan = gate.ci_plan(repo, base, git(repo, "rev-parse", "HEAD"))
    assert plan["runtime_matrix"] is False
    assert plan["windows_validation"] is True
    assert plan["linux_validation"] is (path in gate.DUAL_PLATFORM_VALIDATION_INPUTS)
    assert set(plan["paths"]) == {path, "CONTRIBUTING.md"}


@pytest.mark.parametrize(
    "workflow",
    [
        "missing",
        "changed",
        "output-commented",
        "planner-changed",
        "linux-command",
        "linux-selection",
        "linux-tool-condition",
    ],
)
def test_windows_tool_scope_keeps_runtime_jobs_for_unqualified_workflow(
    repo: Path, workflow: str
) -> None:
    if workflow != "missing":
        path = ".github/workflows/mosswork-ci.yml"
        source = (SOURCE / path).read_text(encoding="utf-8")
        old, new = {
            "changed": ("    timeout-minutes: 10\n", "    timeout-minutes: 11\n"),
            "output-commented": (
                "      windows_validation: ${{ steps.scope.outputs.windows_validation }}",
                "#       windows_validation: ${{ steps.scope.outputs.windows_validation }}",
            ),
            "planner-changed": ("id: scope", "id: other-selector"),
            "linux-command": (
                "      - name: Check independent validation tools and the complete current source",
                "      - name: Removed independent tool check",
            ),
            "linux-tool-condition": (
                " || needs.ci-scope.outputs.linux_validation == 'true'",
                "",
            ),
            "linux-selection": (
                "  release-checks:\n    needs: ci-scope",
                "  release-checks:\n    needs: other-selector",
            ),
        }[workflow]
        assert old in source
        stage(repo, path, source.replace(old, new))
        ci_commit(repo, "different Windows workflow fixture")
    base = git(repo, "rev-parse", "HEAD")
    stage(repo, "tools/check_public_source.py")
    ci_commit(repo, "tool-only candidate")
    plan = gate.ci_plan(repo, base, git(repo, "rev-parse", "HEAD"))
    assert plan["runtime_matrix"] is True
    assert plan["windows_validation"] is False
    assert "recipe" in plan["reason"]


@pytest.mark.parametrize("operation", ["delete", "rename", "runtime", "execution-helper", "worker"])
def test_windows_tool_scope_refuses_missing_or_mixed_runtime_inputs(
    repo: Path, operation: str
) -> None:
    path = "tools/export_public_source.py"
    stage(repo, path)
    ci_commit(repo, "tool baseline")
    base = git(repo, "rev-parse", "HEAD")
    if operation == "delete":
        git(repo, "rm", path)
    elif operation == "rename":
        git(repo, "mv", path, "tools/unknown.py")
    else:
        stage(repo, path, "# Changed tool\n")
        extra = {
            "runtime": "src/octop/config.py",
            "execution-helper": "tools/test_gate.py",
            "worker": "tools/accept_sudograph_worker.py",
        }[operation]
        stage(repo, extra)
    ci_commit(repo, "candidate requiring runtime checks")
    plan = gate.ci_plan(repo, base, git(repo, "rev-parse", "HEAD"))
    assert plan["runtime_matrix"] is True
    assert plan["windows_validation"] is False


def test_ci_tools_refuses_ineligible_input_before_starting_validators(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    stage(repo, "src/octop/config.py")
    ci_commit(repo, "runtime candidate")

    def must_not_run(*_args: object) -> None:
        raise AssertionError("Runtime input must not enter independent tool validation")

    monkeypatch.setattr(gate, "run_commands", must_not_run)
    output = tmp_path / "outputs"
    output.mkdir()
    monkeypatch.setenv("RUNNER_TEMP", str(output))
    with pytest.raises(ValueError, match="normal workflow"):
        gate.ci_tools(repo, base, git(repo, "rev-parse", "HEAD"))
    assert list(output.iterdir()) == []


@pytest.mark.parametrize("outcome", ["success", "validator-failure", "source-drift"])
def test_ci_tools_real_processes_preserve_parent_index_and_stop_after_failure(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    install_dispatcher(repo)
    for path in gate.TOOL_FILES:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / path, target)
    path = ".github/workflows/mosswork-ci.yml"
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE / path, target)
    git(repo, "add", ".")
    ci_commit(repo, "complete validator baseline")
    base = git(repo, "rev-parse", "HEAD")
    path = "tests/unit/deployment/test_export_public_source.py"
    stage(repo, path, (repo / path).read_text(encoding="utf-8") + "\n# Changed fixture\n")
    ci_commit(repo, "independent candidate")
    head = git(repo, "rev-parse", "HEAD")
    index_before = (repo / ".git/index").read_bytes()
    output = tmp_path / "outputs"
    output.mkdir()
    foreign = tmp_path / "foreign-index"
    foreign.write_bytes(b"unrelated index bytes")
    log = tmp_path / "validator-calls.jsonl"
    probe = tmp_path / "validator.py"
    probe.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "assert all(k not in os.environ for k in ['GIT_DIR','GIT_INDEX_FILE','GIT_WORK_TREE','GIT_COMMON_DIR','GIT_PREFIX'])\n"
        "assert os.environ.get('GIT_CONFIG_COUNT') == '1'\n"
        "with Path(os.environ['VALIDATOR_LOG']).open('a',encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "if os.environ['VALIDATOR_OUTCOME']=='source-drift':\n"
        "    Path('README.md').write_text('changed during validation',encoding='utf-8')\n"
        "raise SystemExit(7 if os.environ['VALIDATOR_OUTCOME']=='validator-failure' else 0)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RUNNER_TEMP", str(output))
    monkeypatch.setenv("VALIDATOR_LOG", str(log))
    monkeypatch.setenv("VALIDATOR_OUTCOME", outcome)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "foreign.git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(foreign))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.abbrev")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "12")
    original = gate.run_commands

    def execute(root: Path, checks: list[list[str]], results: list[dict]) -> None:
        original(root, [[sys.executable, str(probe), *argv[1:]] for argv in checks], results)

    monkeypatch.setattr(gate, "run_commands", execute)
    if outcome == "success":
        gate.ci_tools(repo, base, head)
    else:
        with pytest.raises(ValueError, match="exited 7|checkout changed"):
            gate.ci_tools(repo, base, head)
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == (6 if outcome == "success" else 1)
    receipts = list(output.glob("mosswork-ci-tools-*/receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["passed"] is (outcome == "success")
    assert receipt["profile"] == (
        "windows-validation-tools"
        if sys.platform == "win32"
        else sys.platform + "-validation-tools"
    )
    assert receipt["platform"] == sys.platform
    assert os.environ["GIT_INDEX_FILE"] == str(foreign)
    assert foreign.read_bytes() == b"unrelated index bytes"
    assert (repo / ".git/index").read_bytes() == index_before


def seed_tool_worktree(root: Path) -> str:
    for path in (*gate.TOOL_FILES, "tools/test_gate.py", "Makefile"):
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / path, destination)
    git(root, "add", ".")
    ci_commit(root, "complete tool baseline")
    base = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", base)
    return base


def test_worktree_plan_ignores_stat_only_changes_without_refreshing_index(repo: Path) -> None:
    seed_tool_worktree(repo)
    stage(repo, "src/app.py", "value = 1\n")
    ci_commit(repo, "accepted application fixture")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    stage(repo, "tools/ci_identity_reuse.py", "# Actual pending tool source\n")
    runtime = repo / "src/app.py"
    time.sleep(1.1)  # Exercise ordinary same-content writes across filesystem timestamp precision.
    runtime.write_bytes(runtime.read_bytes())
    before = (repo / ".git/index").read_bytes()
    result = subprocess.run(
        [sys.executable, str(repo / "tools/precommit.py"), "--plan", "--worktree"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan["profile"] == "tools"
    assert plan["paths"] == ["tools/ci_identity_reuse.py"]
    assert (repo / ".git/index").read_bytes() == before


def test_worktree_scope_includes_pending_commits_and_all_current_sources(repo: Path) -> None:
    base = seed_tool_worktree(repo)
    stage(repo, "tools/ci_identity_reuse.py", "# Pending committed tool change\n")
    ci_commit(repo, "unmerged tool commit")
    stage(repo, "README.md", "# Staged documentation\n")
    stage(repo, "src/app.py", "value = 1\n")
    ci_commit(repo, "unmerged application commit")
    (repo / "src/app.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "untracked.py").write_text("value = 3\n", encoding="utf-8")
    changed, original, _ = gate.worktree_paths(repo)
    assert original == base
    assert set(changed) == {"tools/ci_identity_reuse.py", "README.md", "src/app.py", "untracked.py"}


@pytest.mark.skipif(os.name == "nt", reason="Windows symlinks need additional OS privileges")
def test_unstaged_tool_symlink_cannot_reuse_regular_index_mode(repo: Path) -> None:
    seed_tool_worktree(repo)
    path = repo / "tools/ci_identity_reuse.py"
    path.unlink()
    path.symlink_to(repo / "README.md")
    result = subprocess.run(
        [sys.executable, str(repo / "tools/precommit.py"), "--plan", "--worktree"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan["profile"] == "full"
    assert "links" in plan["reason"]


@pytest.mark.parametrize(
    "case,expected",
    [
        ("tool", "tools"),
        ("make", "tools"),
        ("docs", "docs"),
        ("missing-base", "full"),
        ("clean", "full"),
        ("unstaged-runtime", "full"),
        ("untracked-runtime", "full"),
        ("committed-runtime", "full"),
        ("deleted-tool", "full"),
    ],
)
def test_actual_worktree_cli_plans_complete_sources_without_borrowing_index(
    repo: Path, tmp_path: Path, case: str, expected: str
) -> None:
    seed_tool_worktree(repo)
    if case == "missing-base":
        git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    if case not in {"clean", "docs", "make"}:
        path = "tools/ci_identity_reuse.py"
        stage(repo, path, (repo / path).read_text(encoding="utf-8") + "\n# tool change\n")
    if case == "docs":
        stage(repo, "docs/new.md", "# Documentation\n")
    if case == "make":
        path = "Makefile"
        stage(repo, path, (repo / path).read_text(encoding="utf-8") + "\n# Changed Make entry\n")
    if case == "unstaged-runtime":
        stage(repo, "src/app.py")
        ci_commit(repo, "runtime baseline in branch")
        (repo / "src/app.py").write_text("# Unstaged runtime\n", encoding="utf-8")
    if case == "untracked-runtime":
        (repo / "unknown.py").write_text("# Unknown source\n", encoding="utf-8")
    if case == "committed-runtime":
        stage(repo, "src/app.py")
        ci_commit(repo, "pending application source")
    if case == "deleted-tool":
        git(repo, "rm", "tools/check_public_source.py")
    index = (repo / ".git/index").read_bytes()
    foreign = tmp_path / "foreign-index"
    foreign.write_bytes(b"foreign original bytes")
    result = subprocess.run(
        [sys.executable, str(repo / "tools/precommit.py"), "--plan", "--worktree"],
        cwd=repo,
        env={
            **os.environ,
            "GIT_INDEX_FILE": str(foreign),
            "GIT_DIR": str(tmp_path / "foreign.git"),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan["profile"] == expected
    assert plan["scope"] == "complete-branch-and-worktree"
    if case in {"tool", "make"}:
        test_command = next(argv for argv in plan["commands"] if "pytest" in argv)
        assert ("tests/unit/deployment/test_make_format_gate.py" in test_command) is (
            case == "make"
        )
        assert ("tests/unit/deployment/test_make_testmon.py" in test_command) is (case == "make")
    if expected == "full":
        assert plan["delegated_commands"] == [
            {"cwd": ".", "argv": ["make", "validate-application"]}
        ]
    assert (repo / ".git/index").read_bytes() == index
    assert foreign.read_bytes() == b"foreign original bytes"


@pytest.mark.parametrize("outcome", ["success", "failed-validator", "new-source"])
def test_worktree_validation_preserves_failure_and_detects_untracked_source_drift(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    seed_tool_worktree(repo)
    stage(repo, "tools/ci_identity_reuse.py", "# Tool source\n")
    index = (repo / ".git/index").read_bytes()
    probe = tmp_path / "validator.py"
    probe.write_text(
        "import os,sys\nfrom pathlib import Path\n"
        "assert 'GIT_INDEX_FILE' not in os.environ\n"
        "assert os.environ['GIT_CONFIG_VALUE_0']=='12'\n"
        "if sys.argv[1]=='new-source': Path('unknown.py').write_text('# added during validation')\n"
        "raise SystemExit(7 if sys.argv[1]=='failed-validator' else 0)\n",
        encoding="utf-8",
    )
    original = gate.run_commands

    def execute(root: Path, checks: list[list[str]], results: list[dict]) -> None:
        original(root, [[sys.executable, str(probe), outcome]], results)

    monkeypatch.setattr(gate, "run_commands", execute)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", [str(repo / "tools/precommit.py"), "--worktree"])
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "unused-index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.abbrev")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "12")
    assert gate.main() == (0 if outcome == "success" else 1)
    receipt = json.loads((repo / ".git/mosswork-worktree-validation.json").read_text())
    assert receipt["passed"] is (outcome == "success")
    assert receipt["status"] == ("passed" if outcome == "success" else "failed")
    assert (repo / ".git/index").read_bytes() == index


@pytest.mark.parametrize(
    "path,required",
    [
        ("src/octop/domain.py", False),
        ("packages/moss-harness/src/moss_harness/domain.py", False),
        ("tests/unit/sudograph/test_domain.py", False),
        ("tests/integration/test_acp_api.py", False),
        ("src/outside.py", True),
        ("src/octop/conftest.py", True),
        ("tests/unit/deployment/test_test_gate.py", True),
        ("tests/conftest.py", True),
        ("tests/support/testmon_staged_changes.py", True),
        ("Makefile", True),
        ("pyproject.toml", True),
        ("packages/moss-harness/pyproject.toml", True),
        ("uv.lock", True),
        ("tools/precommit.py", True),
        (".github/workflows/mosswork-ci.yml", True),
        ("docs/guide.md", True),
    ],
)
def test_committed_payload_keeps_product_matrices_and_scopes_validator_regressions(
    repo: Path, path: str, required: bool
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    stage(repo, path)
    ci_commit(repo, "candidate")
    plan = gate.ci_plan(repo, base, git(repo, "rev-parse", "HEAD"))
    assert plan["validator_regressions"] is required
    if not required:
        assert plan["runtime_matrix"] is True
        assert plan["windows_validation"] is False
        assert plan["paths"] == [path]


@pytest.mark.parametrize("extra", ["uv.lock", "conftest.py", "tools/test_gate.py", "docs/guide.md"])
def test_mixed_product_changes_keep_the_complete_validator_regressions(
    repo: Path, extra: str
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    stage(repo, "src/octop/domain.py")
    stage(repo, extra)
    ci_commit(repo, "mixed candidate")
    plan = gate.ci_plan(repo, base, git(repo, "rev-parse", "HEAD"))
    assert plan["validator_regressions"] is True
    assert plan["runtime_matrix"] is True


@pytest.mark.parametrize("operation", ["delete", "rename"])
def test_missing_committed_payload_never_omits_validator_regressions(
    repo: Path, operation: str
) -> None:
    stage(repo, "src/octop/domain.py")
    ci_commit(repo, "baseline")
    base = git(repo, "rev-parse", "HEAD")
    if operation == "delete":
        git(repo, "rm", "src/octop/domain.py")
    else:
        git(repo, "mv", "src/octop/domain.py", "src/octop/new.py")
    ci_commit(repo, "candidate")
    assert gate.ci_plan(repo, base, git(repo, "rev-parse", "HEAD"))["validator_regressions"] is True


@pytest.mark.skipif(os.name == "nt", reason="Windows symlinks need additional OS privileges")
def test_nonregular_product_payload_keeps_validator_regressions(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    path = repo / "src/octop/domain.py"
    path.parent.mkdir(parents=True)
    path.symlink_to(repo / "README.md")
    git(repo, "add", ".")
    ci_commit(repo, "linked product input")
    assert gate.ci_plan(repo, base, git(repo, "rev-parse", "HEAD"))["validator_regressions"] is True
