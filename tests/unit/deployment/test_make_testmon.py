"""The actual full-test recipe records a graph without selecting fewer tests."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3]


def test_full_make_records_real_baseline_and_preserves_failure(tmp_path: Path) -> None:
    make = shutil.which("make") or shutil.which("mingw32-make")
    if make is None and os.name == "nt":
        pytest.skip("GNU Make is required; the actual Windows publication gate supplies it")
    assert make is not None, "Install GNU Make to validate the original test recipe"
    root = tmp_path / "real make and testmon"
    root.mkdir()
    shutil.copyfile(SOURCE / "Makefile", root / "Makefile")
    shutil.copyfile(SOURCE / "tests/support/testmon_staged_changes.py", root / "staged_changes.py")
    (root / "conftest.py").write_text("import staged_changes\n", encoding="utf-8")
    shutil.copyfile(SOURCE / "pyproject.toml", root / "pyproject.toml")
    (root / "first.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "second.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    (root / "test_first.py").write_text(
        "from first import value\n\ndef test_first():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    (root / "test_second.py").write_text(
        "from second import value\n\ndef test_second():\n    assert value() == 2\n",
        encoding="utf-8",
    )
    (root / "test_live.py").write_text(
        "import pytest\n\n@pytest.mark.live\ndef test_live():\n"
        "    pytest.skip('credential-live fixture')\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_PREFIX",
        "PYTEST_ADDOPTS",
        "PYTEST_CURRENT_TEST",
    ):
        environment.pop(name, None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTEST_ADDOPTS"] = "-p testmon.pytest_testmon -p xdist.plugin"
    # Verify changed staged files using the repository's unchanged real helper.
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "Baseline Test"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "fixture"],
    ):
        subprocess.run(argv, cwd=root, env=environment, check=True, capture_output=True)

    def run(
        target: str, label: str, *pytest_options: str
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        report = root / f"{label}.xml"
        child_environment = {
            **environment,
            "PYTEST_ADDOPTS": environment["PYTEST_ADDOPTS"]
            + f" --junitxml={label}.xml "
            + " ".join(pytest_options),
        }
        result = subprocess.run(
            [
                make,
                target,
                f"RUN={shlex.quote(Path(sys.executable).as_posix())} -m",
                "PYTEST_JOBS=1",
            ],
            cwd=root,
            env=child_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert report.is_file(), result.stdout + result.stderr
        names = [case.attrib["name"] for case in ET.parse(report).iter("testcase")]
        return result, names

    full, names = run("test", "first-full")
    assert full.returncode == 0, full.stdout + full.stderr
    assert sorted(names) == ["test_first", "test_second"]
    assert (root / ".testmondata").is_file()
    unchanged, names = run("test-affected", "unchanged")
    assert unchanged.returncode == 0, unchanged.stdout + unchanged.stderr
    assert "test_first" not in names and "test_second" not in names

    (root / "first.py").write_text("def value():\n    return 1 + 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "first.py"], cwd=root, env=environment, check=True)
    changed, names = run("test-affected", "staged-change")
    assert changed.returncode == 0, changed.stdout + changed.stderr
    assert "test_first" in names and "test_second" not in names

    full_again, names = run("test", "full-with-baseline")
    assert full_again.returncode == 0, full_again.stdout + full_again.stderr
    assert sorted(names) == ["test_first", "test_second"]
    (root / "first.py").write_text("def value():\n    return 99\n", encoding="utf-8")
    failure, names = run("test", "full-failure", "--maxfail=0")
    assert failure.returncode != 0, failure.stdout + failure.stderr
    assert sorted(names) == ["test_first", "test_second"]
    assert len(list(ET.parse(root / "full-failure.xml").iter("failure"))) == 1


@pytest.mark.parametrize("target", ["test", "test-affected", "pytest"])
def test_make_stops_failed_work_and_keeps_successful_coverage(tmp_path: Path, target: str) -> None:
    make = shutil.which("make") or shutil.which("mingw32-make")
    if make is None and os.name == "nt":
        pytest.skip("GNU Make is required; the actual Windows publication gate supplies it")
    assert make is not None, "Install GNU Make to validate the original test recipe"
    root = tmp_path / "real failure scheduling"
    root.mkdir()
    shutil.copyfile(SOURCE / "Makefile", root / "Makefile")
    shutil.copyfile(SOURCE / "pyproject.toml", root / "pyproject.toml")
    (root / "test_work.py").write_text(
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.mark.parametrize('case', range(40))\n"
        "def test_work(case):\n"
        "    assert not Path('fail').exists(), 'controlled validation failure'\n",
        encoding="utf-8",
    )
    (root / "fail").touch()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST", "MAKEFLAGS", "MFLAGS"}
        and not key.startswith("GIT_")
    }
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTEST_ADDOPTS"] = "-p testmon.pytest_testmon -p xdist.plugin"
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "Failure Scheduling Test"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "fixture"],
    ):
        subprocess.run(argv, cwd=root, env=environment, check=True, capture_output=True)

    def run(label: str, *pytest_options: str) -> tuple[int, list[ET.Element]]:
        argv = (
            [sys.executable, "-m", "pytest", "-n", "2"]
            if target == "pytest"
            else [
                make,
                target,
                f"RUN={shlex.quote(Path(sys.executable).as_posix())} -m",
                "PYTEST_JOBS=2",
            ]
        )
        result = subprocess.run(
            argv,
            cwd=root,
            env={
                **environment,
                "PYTEST_ADDOPTS": environment["PYTEST_ADDOPTS"]
                + f" --junitxml={label}.xml "
                + " ".join(pytest_options),
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        (root / f"{label}.stdout").write_text(result.stdout, encoding="utf-8")
        (root / f"{label}.stderr").write_text(result.stderr, encoding="utf-8")
        report = root / f"{label}.xml"
        assert report.is_file(), result.stdout + result.stderr
        return result.returncode, list(ET.parse(report).iter("testcase"))

    # An explicit diagnostic override still collects the complete failure set.
    code, cases = run("all-failures", "--maxfail=0")
    assert code != 0
    assert len(cases) == len([case for case in cases if case.find("failure") is not None]) == 40

    code, cases = run("first-failure")
    assert code != 0
    assert 1 <= len(cases) <= 8  # Two workers may finish already-dispatched work.
    assert all(case.find("failure") is not None for case in cases)

    (root / "fail").unlink()
    code, cases = run("all-successful")
    assert code == 0
    assert len(cases) == 40
    assert all(case.find("failure") is None and case.find("skipped") is None for case in cases)
