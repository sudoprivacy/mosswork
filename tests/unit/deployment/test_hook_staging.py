"""Real Git checks for literal restaging and Windows argument length."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3]


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


def install_dispatcher(root: Path) -> None:
    for name in ("tools/precommit.py", "tools/check_public_source.py", ".githooks/pre-commit"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / name, target)
    (root / ".githooks/pre-commit").chmod(0o755)


@pytest.mark.parametrize("path_count", [1, 600])
def test_full_hook_restages_literal_paths_without_argv_limit(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_count: int
) -> None:
    install_dispatcher(repo)
    (repo / "dashboard").mkdir()
    literal = "src/kept[1].txt"
    stage(repo, literal, "before validation\n")
    neighbor = repo / "src/kept1.txt"
    neighbor.write_text("not staged\n", encoding="utf-8")
    names = [literal]
    for index in range(path_count):
        name = f"src/payloads/{index:04d}-{'p' * 55}.txt"
        file = repo / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("payload\n", encoding="utf-8")
        names.append(name)
    git(repo, "add", "--", "src/payloads")
    before = git(repo, "rev-parse", "HEAD")
    shims = tmp_path / "restage-validators"
    shims.mkdir()
    log = tmp_path / "restage-commands.jsonl"
    for name in ("make", "npm"):
        shim = shims / name
        shim.write_text(
            "#!/usr/bin/env python\nimport json, os, sys\nfrom pathlib import Path\n"
            "tool = Path(sys.argv[0]).name\n"
            "assert 'GIT_INDEX_FILE' not in os.environ\n"
            "with open(os.environ['GATE_TEST_LOG'], 'a', encoding='utf-8') as log:\n"
            "    log.write(json.dumps({'tool':tool,'argv':sys.argv[1:]})+'\\n')\n"
            "if tool == 'make':\n"
            "    Path('src/kept[1].txt').write_text('after validation\\n',encoding='utf-8')\n",
            encoding="utf-8",
            newline="\n",
        )
        shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(shims) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("UV_PYTHON", sys.executable)
    monkeypatch.setenv("GATE_TEST_LOG", str(log))
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=.githooks", "commit", "-m", "literal restage"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(repo, "rev-parse", "HEAD") != before
    assert [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] == [
        {"tool": "make", "argv": ["precommit"]},
        {"tool": "npm", "argv": ["run", "build"]},
    ]
    assert git(repo, "show", f"HEAD:{literal}") == "after validation"
    assert set(git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()) == {
        "README.md",
        *names,
    }
    assert neighbor.read_text(encoding="utf-8") == "not staged\n"
