"""Real Make/Git/backend processes stop before expensive validation on style errors.

The private npm process injects the formatter's check/write outcomes. Actual
Ruff, mypy and pytest execute through the original Make recipes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("status", [0, 1, 2, 3])
def test_make_all_only_delegates_the_explicit_full_gate_status(tmp_path: Path, status: int) -> None:
    make = shutil.which("make") or shutil.which("mingw32-make")
    if make is None and os.name == "nt":
        pytest.skip("GNU Make is required; the actual Windows publication gate supplies it")
    assert make is not None
    (tmp_path / "tools").mkdir()
    shutil.copyfile(SOURCE / "Makefile", tmp_path / "Makefile")
    (tmp_path / "tools/precommit.py").write_text(f"raise SystemExit({status})\n", encoding="utf-8")
    interpreter = shlex.quote(Path(sys.executable).as_posix())
    (tmp_path / "GNUmakefile").write_text(
        "include Makefile\nvalidate-application:\n\t@"
        + interpreter
        + " -c \"from pathlib import Path; Path('application-ran').touch()\"\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [make, "all", "TEST_PYTHON=" + interpreter],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert (result.returncode == 0) is (status in {0, 3}), result.stdout + result.stderr
    assert (tmp_path / "application-ran").exists() is (status == 3)


@pytest.mark.parametrize("target", ["all", "precommit"])
def test_format_checks_preserve_inputs_and_precede_typecheck_and_tests(
    tmp_path: Path, target: str
) -> None:
    make = shutil.which("make") or shutil.which("mingw32-make")
    if make is None and os.name == "nt":
        pytest.skip("GNU Make is required; the actual Windows publication gate supplies it")
    assert make is not None, "Install GNU Make to validate the original publication recipes"
    root = tmp_path / "format gate"
    for path in ("bin", "src/octop", "tests", "dashboard", "empty-hooks", "tools"):
        (root / path).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE / "Makefile", root / "Makefile")
    shutil.copyfile(SOURCE / "tools/test_gate.py", root / "tools/test_gate.py")
    shutil.copyfile(SOURCE / "tools/precommit.py", root / "tools/precommit.py")
    shutil.copyfile(SOURCE / "tests/support/testmon_staged_changes.py", root / "staged_changes.py")
    (root / "conftest.py").write_text(
        "import staged_changes\nimport os, sys, json\nfrom pathlib import Path\n"
        "pytest_plugins = ['tools.test_gate']\n"
        "def pytest_sessionstart(session):\n"
        "    if hasattr(session.config, 'workerinput'): return\n"
        "    trace = Path(os.environ['FORMAT_TRACE'])\n"
        "    rows = [json.loads(x) for x in trace.read_text().splitlines()] if trace.exists() else []\n"
        "    if not any(r['args'][:1] == ['pytest'] for r in rows):\n"
        "        with trace.open('a') as f: f.write(json.dumps({'kind':'backend','args':['pytest',*sys.argv[1:]]})+'\\n')\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n[tool.ruff]\ntarget-version = "py312"\n',
        encoding="utf-8",
    )
    (root / "src/octop/__init__.py").touch()
    sample = root / "src/octop/sample.py"
    sample.write_text("def value() -> int:\n    return 7\n", encoding="utf-8")
    test = root / "tests/test_sample.py"
    test.write_text(
        "import os\nfrom pathlib import Path\n\nfrom octop.sample import value\n\n\n"
        'def test_value():\n    Path(os.environ["FORMAT_MARKER"]).write_text("executed")\n'
        "    assert value() == 7\n",
        encoding="utf-8",
    )
    package = root / "dashboard/package.json"
    package.write_bytes(b'{\n  "name": "format-control"\n}\n')
    (root / ".gitignore").write_text(
        ".mypy_cache/\n.ruff_cache/\n.pytest_cache/\n__pycache__/\n.testmondata*\n"
        "*.jsonl\n*.stdout\n*.stderr\n*.executed\nlegacy.mk\n",
        encoding="utf-8",
    )
    dispatcher = root / "bin/control.py"
    dispatcher.write_text(
        """import json, os, subprocess, sys
from pathlib import Path
root = Path(__file__).resolve().parents[1]
kind, *args = sys.argv[1:]
with open(os.environ["FORMAT_TRACE"], "a", encoding="utf-8") as output:
    output.write(json.dumps({"kind": kind, "args": args}) + "\\n")
if kind == "backend":
    sys.exit(subprocess.run([sys.executable, "-m", *args]).returncode)
assert Path.cwd() == root / "dashboard"
if kind == "typescript":
    assert args == ["tsc", "-b", "--force"], args
    sys.exit(19 if os.environ.get("CONTROL_TYPES_FAIL") == "1" else 0)
assert args[:1] == ["run"]
path = root / "dashboard/package.json"
if args[1] == "format":
    path.write_bytes(path.read_bytes().replace(b"\\r\\n", b"\\n"))
elif args[1] == "format:check":
    if b"\\r\\n" in path.read_bytes():
        sys.exit("controlled frontend formatting mismatch")
else:
    raise AssertionError(args)
""",
        encoding="utf-8",
    )
    interpreter = Path(sys.executable).as_posix()
    control = dispatcher.as_posix()
    for executable, kind in (("npm", "frontend"), ("npx", "typescript")):
        (root / "bin" / executable).write_text(
            "#!/bin/sh\nexec " + shlex.join([interpreter, control, kind]) + ' "$@"\n',
            encoding="utf-8",
        )
        (root / "bin" / executable).chmod(0o755)
        (root / "bin" / (executable + ".cmd")).write_text(
            f'@echo off\r\n"{interpreter}" "{control}" {kind} %*\r\n', encoding="utf-8"
        )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST", "MAKEFLAGS", "MFLAGS"}
        and not key.startswith("GIT_")
    }
    environment.update(
        PATH=str(root / "bin") + os.pathsep + environment["PATH"],
        PYTHONPATH=os.pathsep.join([str(root), str(root / "src")]),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTEST_ADDOPTS="-p testmon.pytest_testmon -p xdist.plugin -n 1 --color=no",
    )

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=root, env=environment)

    git("init", "-q")
    git("config", "user.name", "Format Gate Test")
    git("config", "user.email", "test@example.com")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", str(root / "empty-hooks"))
    git("config", "core.autocrlf", "true")
    git("add", ".")
    git("commit", "-qm", "fixture")
    git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD").decode().strip())
    inputs = [root / name.decode() for name in git("ls-files", "-z").split(b"\0") if name]

    def snapshot() -> dict[str, str]:
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}

    def run(
        goal: str, label: str, *extra: str, types_fail: bool = False
    ) -> tuple[subprocess.CompletedProcess, list[dict]]:
        trace = root / f"{label}.jsonl"
        result = subprocess.run(
            [
                make,
                "-j2",
                *extra,
                goal,
                "RUN=" + shlex.join([interpreter, control, "backend"]),
                "TEST_PYTHON=" + shlex.quote(interpreter),
                "PYTEST_JOBS=1",
            ],
            cwd=root,
            env={
                **environment,
                "FORMAT_TRACE": str(trace),
                "FORMAT_MARKER": str(root / f"{label}.executed"),
                "CONTROL_TYPES_FAIL": "1" if types_fail else "0",
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        (root / f"{label}.stdout").write_text(result.stdout, encoding="utf-8")
        (root / f"{label}.stderr").write_text(result.stderr, encoding="utf-8")
        calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        return result, calls

    for problem in ("frontend", "backend"):
        if problem == "frontend":
            package.write_bytes(package.read_bytes().replace(b"\n", b"\r\n"))
        else:
            sample.write_text("def value( )->int:\n return 7\n", encoding="utf-8")
        git("add", ".")
        before, index = snapshot(), git("ls-files", "--stage", "-z")
        result, calls = run(target, problem + "-refused")
        assert result.returncode != 0, result.stdout + result.stderr
        assert "make format-all" in result.stdout
        assert snapshot() == before and git("ls-files", "--stage", "-z") == index
        assert not any(row["args"][:1] in (["mypy"], ["pytest"]) for row in calls), calls
        assert not any(row["kind"] == "typescript" for row in calls), calls
        repaired, _ = run("format-all", problem + "-repair")
        assert repaired.returncode == 0, repaired.stdout + repaired.stderr
        git("add", ".")
        before, index = snapshot(), git("ls-files", "--stage", "-z")
        positive, calls = run(target, problem + "-qualified")
        assert positive.returncode == 0, positive.stdout + positive.stderr
        assert "available fixes" not in positive.stdout + positive.stderr
        assert snapshot() == before and git("ls-files", "--stage", "-z") == index
        assert ["mypy", "src/octop"] in [row["args"] for row in calls]
        kinds = [row["kind"] for row in calls]
        assert kinds.count("typescript") == 1
        assert kinds.index("typescript") < next(
            index for index, row in enumerate(calls) if row["args"][:1] == ["mypy"]
        )
        pytest_args = [row["args"] for row in calls if row["args"][:1] == ["pytest"]]
        assert len(pytest_args) == 1
        # Both entries require a complete run without matching source-bound history.
        assert "--testmon-noselect" in pytest_args[0]
        assert ("-m" in pytest_args[0]) is (target == "all")

    before, index = snapshot(), git("ls-files", "--stage", "-z")
    refused, calls = run(target, "type-error-refused", types_fail=True)
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert any(row["args"] == ["tsc", "-b", "--force"] for row in calls)
    assert not any(row["args"][:1] in (["mypy"], ["pytest"]) for row in calls), calls
    assert not (root / "type-error-refused.executed").exists()
    assert snapshot() == before and git("ls-files", "--stage", "-z") == index

    # Restore automatic formatting as a prerequisite: the old behavior runs
    # validators successfully while changing the frozen input's raw bytes.
    package.write_bytes(package.read_bytes().replace(b"\n", b"\r\n"))
    git("add", ".")
    before = snapshot()
    (root / "legacy.mk").write_text(f"{target}: format-all\n", encoding="utf-8")
    old, calls = run(target, "legacy-input-drift", "-f", "Makefile", "-f", "legacy.mk")
    assert old.returncode == 0, old.stdout + old.stderr
    assert snapshot() != before
    assert any(row["args"][:1] == ["pytest"] for row in calls)
    assert git("diff", "--name-only", "--", "dashboard/package.json") == b""

    test.write_text(test.read_text(encoding="utf-8").replace("== 7", "== 999"), encoding="utf-8")
    git("add", ".")
    failure, calls = run(target, "original-test-failure")
    assert failure.returncode != 0, failure.stdout + failure.stderr
    assert "available fixes" not in failure.stdout + failure.stderr
    assert any(row["args"][:1] == ["pytest"] for row in calls)
    assert "assert 7 == 999" in failure.stdout + failure.stderr
