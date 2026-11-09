"""Real staged Git candidates and processes exercise the publication dispatcher."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
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
        ("docs/业务说明.md", "docs"),
        pytest.param(
            "docs/业务\n说明.md",
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
    receipt = json.loads((repo / ".git/mosswork-precommit.json").read_text(encoding="utf-8"))
    assert receipt["passed"] is None and receipt["delegated_to_full_gate"] is True


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
    monkeypatch.setattr(gate, "commands", lambda _selected: [[sys.executable, str(child)]])
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
