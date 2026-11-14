"""Actual private Git/pytest histories qualify selection without copying an application cache."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3]


@pytest.fixture
def gate_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "real gate"
    for folder in ("src", "tests", "tools", "output", "empty-hooks"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE / "tools/test_gate.py", root / "tools/test_gate.py")
    shutil.copyfile(SOURCE / "Makefile", root / "Makefile")
    shutil.copyfile(SOURCE / "tests/support/testmon_staged_changes.py", root / "staged_changes.py")
    (root / "conftest.py").write_text(
        'import staged_changes\npytest_plugins = ["tools.test_gate"]\n', encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths=["tests"]\npythonpath=["src"]\nmarkers=["live: explicit paid-model tests"]\naddopts="--maxfail=1"\n',
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(
        ".testmon*\n.pytest_cache/\n__pycache__/\noutput/\n", encoding="utf-8"
    )
    for name, value in (("first", 1), ("second", 2)):
        (root / f"src/{name}.py").write_text(
            f"def value():\n    return {value}\n", encoding="utf-8"
        )
        (root / f"tests/test_{name}.py").write_text(
            f"from {name} import value\nfrom pathlib import Path\n\ndef test_{name}():\n"
            f"    with Path('output/observed').open('a') as f: f.write('{name}\\n')\n"
            f"    assert value() == {value}\n",
            encoding="utf-8",
        )
    (root / "tests/test_live.py").write_text(
        "import pytest\nfrom pathlib import Path\n\n@pytest.mark.live\ndef test_live():\n"
        "    Path('output/LIVE-RAN').touch()\n    raise AssertionError('live body must not execute')\n",
        encoding="utf-8",
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
            "GIT_PREFIX",
            "PYTEST_CURRENT_TEST",
            "PYTEST_ADDOPTS",
        }
    }
    env.update(
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTEST_ADDOPTS="-p testmon.pytest_testmon -p xdist.plugin",
        PYTHONIOENCODING="utf-8",
        OPENAI_API_KEY="synthetic-present-credential",
    )
    for args in (
        ("init", "-q"),
        ("config", "user.name", "Gate Test"),
        ("config", "user.email", "test@example.com"),
        ("config", "core.hooksPath", str(root / "empty-hooks")),
        ("add", "."),
        ("commit", "-qm", "fixture"),
    ):
        subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)
    return root, env


def execute(
    root: Path, env: dict[str, str], label: str, *args: str
) -> tuple[subprocess.CompletedProcess[str], dict, list[str]]:
    marker = root / "output/observed"
    prior = marker.read_text().splitlines() if marker.exists() else []
    result = subprocess.run(
        [sys.executable, str(root / "tools/test_gate.py"), "--workers", "1", *args],
        cwd=root,
        env={
            **env,
            "PYTEST_ADDOPTS": env["PYTEST_ADDOPTS"] + " --junitxml=output/" + label + ".xml",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    (root / f"output/{label}.stdout").write_text(result.stdout, encoding="utf-8")
    (root / f"output/{label}.stderr").write_text(result.stderr, encoding="utf-8")
    plans = [line for line in result.stdout.splitlines() if line.startswith("TEST_GATE: {")]
    plan = json.loads(plans[0].removeprefix("TEST_GATE: ")) if plans else {}
    after = marker.read_text().splitlines() if marker.exists() else []
    assert not (root / "output/LIVE-RAN").exists(), result.stdout + result.stderr
    return result, plan, after[len(prior) :]


def test_actual_graph_reuses_python_changes_and_keeps_nonpython_and_missing_cache_full(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    root, env = gate_repo
    first, plan, calls = execute(root, env, "first")
    assert first.returncode == 0, first.stdout + first.stderr
    assert plan["mode"] == "full" and sorted(calls) == ["first", "second"]
    assert Path(plan["graph"]).is_file()
    unchanged, plan, calls = execute(root, env, "unchanged")
    assert unchanged.returncode == 0, unchanged.stdout + unchanged.stderr
    assert plan["mode"] == "affected" and calls == []
    (root / "src/first.py").write_text("def value():\n    return 1 + 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/first.py"], cwd=root, env=env, check=True)
    changed, plan, calls = execute(root, env, "changed")
    assert changed.returncode == 0, changed.stdout + changed.stderr
    assert plan["mode"] == "affected" and calls == ["first"]
    (root / "settings.json").write_text('{"new": true}\n', encoding="utf-8")
    nonpython, plan, calls = execute(root, env, "nonpython")
    assert nonpython.returncode == 0, nonpython.stdout + nonpython.stderr
    assert plan["mode"] == "full" and sorted(calls) == ["first", "second"]
    Path(plan["graph"]).rename(root / "output/preserved-graph")
    missing, plan, calls = execute(root, env, "missing")
    assert missing.returncode == 0, missing.stdout + missing.stderr
    assert plan["mode"] == "full" and sorted(calls) == ["first", "second"]
    explicit, plan, calls = execute(root, env, "explicit", "--full")
    assert explicit.returncode == 0, explicit.stdout + explicit.stderr
    assert plan["mode"] == "full" and sorted(calls) == ["first", "second"]
    environment_changed, plan, calls = execute(
        root, {**env, "MOSSWORK_SYNTHETIC_VARIANT": "changed"}, "environment"
    )
    assert environment_changed.returncode == 0, (
        environment_changed.stdout + environment_changed.stderr
    )
    assert plan["mode"] == "full" and sorted(calls) == ["first", "second"]


def test_real_failure_and_collection_only_cannot_replace_a_passed_baseline(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    root, env = gate_repo
    initial, plan, _ = execute(root, env, "initial")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    baseline = root / ".git/mosswork-test-gate/baseline.json"
    before = baseline.read_bytes()
    (root / "src/first.py").write_text("def value():\n    return 99\n", encoding="utf-8")
    failed, plan, calls = execute(root, env, "failed")
    assert failed.returncode != 0 and "assert 99 == 1" in failed.stdout
    assert plan["mode"] == "affected" and calls == ["first"]
    assert baseline.read_bytes() == before
    attempted, plan, calls = execute(
        root, {**env, "PYTEST_ADDOPTS": env["PYTEST_ADDOPTS"] + " --collect-only"}, "selection"
    )
    assert attempted.returncode != 0 and "TEST_GATE_REFUSED" in attempted.stderr
    assert not plan and not calls and baseline.read_bytes() == before


def test_source_drift_rejects_an_otherwise_successful_real_pytest_run(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    root, env = gate_repo
    initial, _, _ = execute(root, env, "initial")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    baseline = root / ".git/mosswork-test-gate/baseline.json"
    before = baseline.read_bytes()
    (root / "tests/test_write.py").write_text(
        "from pathlib import Path\n\ndef test_write():\n"
        "    Path('src/first.py').write_text('def value():\\n    return 1 + 0\\n')\n",
        encoding="utf-8",
    )
    result, plan, _ = execute(root, env, "drift")
    assert result.returncode != 0 and "Source/environment changed" in result.stderr
    receipt = json.loads(Path(plan["receipt"]).read_text(encoding="utf-8"))
    assert receipt["exit_code"] == 0 and not receipt["passed"]
    assert receipt["changed_during_validation"] == ["src/first.py"]
    assert baseline.read_bytes() == before


@pytest.mark.parametrize("with_git_shell", [False, True])
def test_source_archive_without_git_keeps_the_original_full_nonlive_run(
    gate_repo: tuple[Path, dict[str, str]],
    with_git_shell: bool,
) -> None:
    root, env = gate_repo
    if with_git_shell:
        (root / ".githooks").mkdir()
        shutil.copyfile(SOURCE / ".githooks/test-gate", root / ".githooks/test-gate")
    (root / ".git").rename(root / "output/retained-git")
    result, plan, calls = execute(root, env, "archive")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no reusable baseline" in result.stdout and not plan
    assert sorted(calls) == ["first", "second"]


def test_git_shell_zero_without_actual_gate_cannot_accept_validation(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    root, env = gate_repo
    hooks = root / ".githooks"
    hooks.mkdir()
    shutil.copyfile(SOURCE / ".githooks/test-gate", hooks / "test-gate")
    (hooks / "test-gate").chmod(0o755)
    original, _, calls = execute(root, env, "original-actual-gate")
    assert original.returncode == 0 and sorted(calls) == ["first", "second"]
    storage = root / ".git/mosswork-test-gate"
    baseline = (storage / "baseline.json").read_bytes()
    (hooks / "test-gate").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    refused, _, calls = execute(root, env, "zero-shell")
    assert refused.returncode != 0 and "zero shell exit cannot replace validation" in refused.stderr
    assert calls == [] and (storage / "baseline.json").read_bytes() == baseline
    assert not list(storage.glob("launch-*.json"))


def test_real_make_uses_the_gate_and_explicit_full_target_keeps_every_case(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    make = shutil.which("make") or shutil.which("mingw32-make")
    if make is None and os.name == "nt":
        pytest.skip("GNU Make is required; the Windows publication route supplies it")
    assert make is not None
    root, env = gate_repo
    modes: list[str] = []
    for label, target in (("cold", "test-current"), ("reuse", "test-current"), ("full", "test")):
        result = subprocess.run(
            [
                make,
                target,
                "PYTEST_JOBS=1",
                "TEST_PYTHON=" + shlex.quote(Path(sys.executable).as_posix()),
                "RUN=" + shlex.quote(Path(sys.executable).as_posix()) + " -m",
            ],
            cwd=root,
            env={
                **env,
                "PYTEST_ADDOPTS": env["PYTEST_ADDOPTS"]
                + " --junitxml=output/make-"
                + label
                + ".xml",
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        plans = [line for line in result.stdout.splitlines() if line.startswith("TEST_GATE: {")]
        modes.append(
            json.loads(plans[0].removeprefix("TEST_GATE: "))["mode"] if plans else "explicit-full"
        )
    assert modes == ["full", "affected", "explicit-full"]
    assert (root / "output/observed").read_text().splitlines().count("first") == 2
    assert (root / "output/observed").read_text().splitlines().count("second") == 2
    assert not (root / "output/LIVE-RAN").exists()


@pytest.mark.parametrize("inherited_hook_route", [False, True])
def test_fresh_make_and_real_commit_share_history_without_losing_live_or_policy_checks(
    gate_repo: tuple[Path, dict[str, str]],
    inherited_hook_route: bool,
) -> None:
    make = shutil.which("make") or shutil.which("mingw32-make")
    if make is None and os.name == "nt":
        pytest.skip("GNU Make is required; the Windows publication route supplies it")
    assert make is not None
    root, environment = gate_repo
    if inherited_hook_route:
        environment["GIT_CONFIG_PARAMETERS"] = "'core.hooksPath'='.githooks'"
    hooks = root / ".githooks"
    hooks.mkdir()
    shutil.copyfile(SOURCE / ".githooks/test-gate", hooks / "test-gate")
    (hooks / "test-gate").chmod(0o755)
    (root / "tests/test_live.py").write_text(
        "import pytest\nfrom pathlib import Path\n\n@pytest.mark.live\ndef test_live():\n"
        "    with Path('output/observed').open('a') as f: f.write('live\\n')\n",
        encoding="utf-8",
    )
    arguments = [
        make,
        "PYTEST_JOBS=1",
        "TEST_PYTHON=" + shlex.quote(Path(sys.executable).as_posix()),
        "RUN=" + shlex.quote(Path(sys.executable).as_posix()) + " -m",
    ]
    # This private hook isolates the actual Make test boundary; the application's
    # original lint/typecheck/build hook still qualifies publication separately.
    (hooks / "pre-commit").write_text(
        "#!/bin/sh\nexec " + shlex.join([*arguments, "test-affected"]) + "\n",
        encoding="utf-8",
    )
    (hooks / "pre-commit").chmod(0o755)
    subprocess.run(["git", "add", "."], cwd=root, env=environment, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=" + str(root / "empty-hooks"),
            "commit",
            "-qm",
            "shared entry fixture",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        check=True,
    )
    marker = root / "output/observed"

    def invoke(*argv: str) -> tuple[dict, list[str]]:
        before = marker.read_text().splitlines() if marker.exists() else []
        result = subprocess.run(
            list(argv),
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        receipt = json.loads((root / ".git/mosswork-test-gate/history.json").read_text())
        assert receipt["passed"]
        return receipt, marker.read_text().splitlines()[len(before) :]

    full, observed = invoke(*arguments, "test-current")
    assert full["mode"] == "full" and sorted(observed) == ["first", "second"]
    graph = full["graph"]
    hook, observed = invoke(
        "git", "-c", "core.hooksPath=.githooks", "commit", "--allow-empty", "-qm", "checked"
    )
    assert hook["mode"] == "affected" and observed == ["live"], hook["reason"]
    assert hook["graph"] == graph and hook["scope"] == "including-live"
    assert not (root / ".testmondata").exists()
    unchanged, observed = invoke(
        "git", "-c", "core.hooksPath=.githooks", "commit", "--allow-empty", "-qm", "unchanged"
    )
    assert unchanged["mode"] == "affected" and observed == []
    assert unchanged["pytest_session"]["collected"] == 0
    assert unchanged["pytest_session"]["exit_code"] == 0
    nonlive, observed = invoke(*arguments, "test-current")
    assert nonlive["mode"] == "affected" and observed == []
    unchanged, observed = invoke(
        "git", "-c", "core.hooksPath=.githooks", "commit", "--allow-empty", "-qm", "same scopes"
    )
    assert unchanged["mode"] == "affected" and observed == []

    live = root / "tests/test_live.py"
    live.write_text(
        live.read_text(encoding="utf-8")
        + "\n@pytest.mark.live\ndef test_live_added():\n"
        + "    with Path('output/observed').open('a') as f: f.write('new-live\\n')\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "tests/test_live.py"], cwd=root, env=environment, check=True)
    nonlive, observed = invoke(*arguments, "test-current")
    assert nonlive["mode"] == "affected" and observed == []
    eligible, observed = invoke(
        "git", "-c", "core.hooksPath=.githooks", "commit", "-qm", "required new live case"
    )
    assert eligible["mode"] == "affected" and observed.count("new-live") == 1
    assert not {"first", "second"}.intersection(observed)

    (root / "src/first.py").write_text("def value():\n    return 1 + 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/first.py"], cwd=root, env=environment, check=True)
    staged, observed = invoke(
        "git", "-c", "core.hooksPath=.githooks", "commit", "-qm", "staged source"
    )
    assert staged["mode"] == "affected" and observed == ["first"]

    policy, observed = invoke(
        "git",
        "-c",
        "core.hooksPath=.githooks",
        "-c",
        "mosswork.fixture-policy=changed",
        "commit",
        "--allow-empty",
        "-qm",
        "different actual Git policy",
    )
    assert policy["mode"] == "full" and sorted(observed) == ["first", "live", "new-live", "second"]
    assert "GIT_CONFIG_PARAMETERS" in policy["environment_variables"]
    baseline = root / ".git/mosswork-test-gate/baseline.json"
    passed_baseline = baseline.read_bytes()
    (root / "tests/test_live.py").write_text(
        "import pytest\n\n@pytest.mark.live\ndef test_live():\n"
        "    raise AssertionError('controlled live failure must stop the commit')\n",
        encoding="utf-8",
    )
    failed = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=.githooks",
            "commit",
            "--allow-empty",
            "-qm",
            "refused live failure",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert failed.returncode != 0 and "controlled live failure" in failed.stdout + failed.stderr
    assert baseline.read_bytes() == passed_baseline


def test_zero_exit_without_an_actual_pytest_session_cannot_create_a_baseline(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    root, env = gate_repo
    env = {k: v for k, v in env.items() if k != "PYTHONSAFEPATH"}
    initial, _, _ = execute(root, env, "initial")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    baseline = root / ".git/mosswork-test-gate/baseline.json"
    before = baseline.read_bytes()
    (root / "pytest.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    fake, _, calls = execute(root, env, "no-session")
    assert fake.returncode != 0 and "Actual pytest completion receipt" in fake.stderr
    assert not calls and baseline.read_bytes() == before


def test_changed_or_interrupted_graph_is_never_reused_and_legacy_cache_is_independent(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    root, env = gate_repo
    first, plan, _ = execute(root, env, "first")
    assert first.returncode == 0, first.stdout + first.stderr
    original_graph = Path(plan["graph"])
    with original_graph.open("ab") as stream:
        stream.write(b"changed outside the gate")
    changed, plan, calls = execute(root, env, "changed-graph")
    assert changed.returncode == 0, changed.stdout + changed.stderr
    assert plan["mode"] == "full" and sorted(calls) == ["first", "second"]
    assert Path(plan["graph"]) != original_graph and original_graph.is_file()
    storage = root / ".git/mosswork-test-gate"
    (storage / "incomplete.json").write_text('{"interrupted": true}\n', encoding="utf-8")
    previous_graph = Path(plan["graph"])
    interrupted, plan, calls = execute(root, env, "interrupted")
    assert interrupted.returncode == 0, interrupted.stdout + interrupted.stderr
    assert plan["mode"] == "full" and sorted(calls) == ["first", "second"]
    assert Path(plan["graph"]) != previous_graph and previous_graph.is_file()
    # Ordinary pytest/hook output cannot mutate the gate's private graph.
    (root / ".testmondata").write_bytes(b"independent ordinary pytest cache")
    reused, plan, calls = execute(root, env, "independent")
    assert reused.returncode == 0, reused.stdout + reused.stderr
    assert plan["mode"] == "affected" and calls == []


def test_simultaneous_gate_refuses_before_launching_pytest(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    from tools.test_gate import gate_lock

    root, env = gate_repo
    storage = root / ".git/mosswork-test-gate"
    storage.mkdir()
    with gate_lock(storage):
        result, plan, calls = execute(root, env, "contended")
    assert result.returncode != 0 and "already running" in result.stderr
    assert not plan and calls == []


def test_nested_commands_do_not_inherit_the_private_testmon_graph(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    root, env = gate_repo
    (root / "tests/test_nested.py").write_text(
        "import subprocess, sys\n\ndef test_nested():\n"
        "    value = subprocess.check_output([sys.executable, '-c', "
        "\"import os;print(os.environ.get('TESTMON_DATAFILE', 'unset'))\"], text=True)\n"
        "    assert value.strip() == 'unset'\n",
        encoding="utf-8",
    )
    result, _, _ = execute(root, env, "nested")
    assert result.returncode == 0, result.stdout + result.stderr


def test_linked_worktree_workers_keep_their_own_root_and_private_graph(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    root, env = gate_repo
    linked = root.parent / "linked gate"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(linked), "HEAD"],
        cwd=root,
        env=env,
        capture_output=True,
        check=True,
    )
    (linked / "output").mkdir()
    result, plan, calls = execute(linked, env, "linked")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(calls) == ["first", "second"]
    assert not (root / "output/observed").exists()
    receipt = json.loads(Path(plan["receipt"]).read_text(encoding="utf-8"))
    assert receipt["pytest_session"]["root"] == str(linked.resolve())
    assert "/worktrees/" in Path(plan["graph"]).as_posix()


def test_failed_first_full_retains_passed_failed_and_unexecuted_cases_distinctly(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    root, env = gate_repo
    (root / "tests/test_third.py").write_text(
        "from pathlib import Path\n\ndef test_third():\n"
        "    with Path('output/observed').open('a') as f: f.write('third\\n')\n"
        "\n",
        encoding="utf-8",
    )
    (root / "src/second.py").write_text("def value():\n    return 99\n", encoding="utf-8")
    failed, plan, calls = execute(root, env, "first-full-failed")
    assert failed.returncode != 0 and "assert 99 == 2" in failed.stdout
    assert plan["mode"] == "full" and calls == ["first", "second"]
    storage = root / ".git/mosswork-test-gate"
    assert not (storage / "baseline.json").exists()
    history = json.loads((storage / "history.json").read_text(encoding="utf-8"))
    assert not history["passed"] and history["trusted_graph"]
    assert history["pytest_session"]["passed"] == 1
    assert history["pytest_session"]["failed"] == 1
    assert not (storage / "incomplete.json").exists()
    (root / "src/second.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    fixed, plan, calls = execute(root, env, "fixed")
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert plan["mode"] == "affected" and plan["prior_passed"] is False
    # Testmon has not checkpointed this short coverage batch. It safely reruns
    # the unrecorded pass too; no saved work is claimed for that case.
    assert sorted(calls) == ["first", "second", "third"]
    baseline = json.loads((storage / "baseline.json").read_text(encoding="utf-8"))
    assert baseline["passed"] and baseline["mode"] == "affected"
    # The first actual field remains failed, never relabeled as whole-source pass.
    assert json.loads(Path(history["attempt"]).read_text(encoding="utf-8"))["passed"] is False


def test_checkpointed_passes_are_reused_after_a_real_first_full_failure(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    from testmon.testmon_core import TEST_BATCH_SIZE

    root, env = gate_repo
    first = root / "tests/test_first.py"
    first.write_text(
        "import pytest\n"
        + first.read_text(encoding="utf-8").replace(
            "def test_first():",
            f"@pytest.mark.parametrize('index', range({TEST_BATCH_SIZE}))\ndef test_first(index):",
        ),
        encoding="utf-8",
    )
    (root / "tests/test_third.py").write_text(
        "from pathlib import Path\n\ndef test_third():\n"
        "    with Path('output/observed').open('a') as f: f.write('third\\n')\n",
        encoding="utf-8",
    )
    (root / "src/second.py").write_text("def value():\n    return 99\n", encoding="utf-8")
    failed, plan, calls = execute(root, env, "checkpointed-first-full-failed")
    assert failed.returncode != 0 and plan["mode"] == "full"
    assert calls == ["first"] * TEST_BATCH_SIZE + ["second"]
    storage = root / ".git/mosswork-test-gate"
    history = json.loads((storage / "history.json").read_text(encoding="utf-8"))
    assert not history["passed"] and history["pytest_session"]["passed"] == TEST_BATCH_SIZE
    assert not (storage / "baseline.json").exists()
    (root / "src/second.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    repaired, plan, calls = execute(root, env, "checkpointed-repaired")
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert plan["mode"] == "affected" and plan["prior_passed"] is False
    assert sorted(calls) == ["second", "third"]
    assert json.loads(Path(history["attempt"]).read_text(encoding="utf-8"))["passed"] is False


def test_crashed_worker_does_not_make_its_partial_graph_trusted(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    root, env = gate_repo
    initial, _, _ = execute(root, env, "initial")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    storage = root / ".git/mosswork-test-gate"
    previous = (storage / "history.json").read_bytes()
    config = root / "pyproject.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            '--maxfail=1"', '--maxfail=1 --max-worker-restart=0"'
        ),
        encoding="utf-8",
    )
    (root / "tests/test_crash.py").write_text(
        "import os\n\ndef test_crash():\n    os._exit(12)\n", encoding="utf-8"
    )
    result, plan, _ = execute(root, env, "crash")
    assert result.returncode != 0
    receipt = json.loads(Path(plan["receipt"]).read_text(encoding="utf-8"))
    assert receipt["passed"] is False and receipt["pytest_session"]["worker_crashed"]
    assert (storage / "history.json").read_bytes() == previous
    assert (storage / "incomplete.json").exists()


def test_actual_zero_status_early_pytest_exit_cannot_become_a_full_baseline(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    root, env = gate_repo
    conftest = root / "conftest.py"
    conftest.write_text(
        conftest.read_text(encoding="utf-8")
        + "\nimport pytest\n@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
        "def pytest_collection(session):\n    yield\n"
        "    pytest.exit('synthetic early collection stop', returncode=0)\n",
        encoding="utf-8",
    )
    result, plan, calls = execute(root, env, "early-zero-status")
    assert result.returncode != 0 and "Actual pytest completion receipt" in result.stderr
    assert calls == []
    receipt = json.loads(Path(plan["receipt"]).read_text(encoding="utf-8"))
    assert receipt["exit_code"] == 0 and receipt["passed"] is False
    assert receipt["pytest_session"]["interrupted"]
    storage = root / ".git/mosswork-test-gate"
    assert not (storage / "baseline.json").exists()
    assert not (storage / "history.json").exists()


def test_unexecuted_existing_dependent_is_required_after_an_affected_failure(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    root, env = gate_repo
    second = root / "tests/test_second.py"
    second.write_text(
        second.read_text(encoding="utf-8")
        .replace("from second import value", "from first import value")
        .replace("value() == 2", "value() == 1"),
        encoding="utf-8",
    )
    initial, _, _ = execute(root, env, "initial")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    (root / "src/first.py").write_text("def value():\n    return 99\n", encoding="utf-8")
    failed, plan, calls = execute(root, env, "shared-dependency-failed")
    assert failed.returncode != 0 and plan["mode"] == "affected"
    assert len(calls) == 1 and calls[0] in {"first", "second"}
    failed_name = calls[0]
    unexecuted_name = "second" if failed_name == "first" else "first"
    repaired_test = root / f"tests/test_{failed_name}.py"
    repaired_test.write_text(
        repaired_test.read_text(encoding="utf-8").replace("value() == 1", "value() == 99"),
        encoding="utf-8",
    )
    still_failed, plan, calls = execute(root, env, "second-still-required")
    assert still_failed.returncode != 0 and "assert 99 == 1" in still_failed.stdout
    assert plan["mode"] == "affected" and unexecuted_name in calls
    checked = set(calls)
    other_test = root / f"tests/test_{unexecuted_name}.py"
    other_test.write_text(
        other_test.read_text(encoding="utf-8").replace("value() == 1", "value() == 99"),
        encoding="utf-8",
    )
    repaired, plan, calls = execute(root, env, "both-repaired")
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert plan["mode"] == "affected" and unexecuted_name in calls
    if failed_name not in checked:
        assert failed_name in calls


def test_tool_call_tracking_changes_do_not_invalidate_real_history(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    root, env = gate_repo
    first, _, _ = execute(root, {**env, "CODEX_TOOL_CALL_ID": "synthetic-call-one"}, "first")
    assert first.returncode == 0, first.stdout + first.stderr
    reused, plan, calls = execute(
        root,
        {**env, "CODEX_TOOL_CALL_ID": "synthetic-call-two", "MOSSWORK_TEST_REQUIRE_REUSE": "1"},
        "new-tracking-id",
    )
    assert reused.returncode == 0, reused.stdout + reused.stderr
    assert plan["mode"] == "affected" and calls == []
    refused, plan, calls = execute(
        root,
        {
            **env,
            "MOSSWORK_TEST_REQUIRE_REUSE": "1",
            "MOSSWORK_SYNTHETIC_VARIANT": "private-control-value",
        },
        "real-environment-change",
    )
    assert refused.returncode != 0 and not plan and calls == []
    assert "MOSSWORK_SYNTHETIC_VARIANT" in refused.stderr
    assert "private-control-value" not in refused.stdout + refused.stderr


def test_reuse_measurement_refuses_before_an_unplanned_full_run(
    gate_repo: tuple[Path, dict[str, str]],
) -> None:
    root, env = gate_repo
    refused, plan, calls = execute(root, {**env, "MOSSWORK_TEST_REQUIRE_REUSE": "1"}, "no-baseline")
    assert refused.returncode != 0 and not plan and calls == []
    assert "No tests ran" in refused.stderr and "make all" in refused.stderr
    assert not (root / ".git/mosswork-test-gate/incomplete.json").exists()


@pytest.mark.parametrize("repetitions", [1, 2, 3])
def test_posix_git_launch_preserves_other_path_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repetitions: int
) -> None:
    from tools import test_gate

    git_exec = str(tmp_path / "git-exec")
    Path(git_exec).mkdir()
    original = [git_exec] * repetitions + ["/custom/tools", git_exec, "/usr/bin", "/custom/tools"]
    monkeypatch.setattr(test_gate.sys, "platform", "linux")
    monkeypatch.setenv("GIT_EXEC_PATH", git_exec)
    monkeypatch.setenv("PATH", os.pathsep.join(original))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath=.githooks' 'fixture.policy=keep'")
    test_gate.normalize_launch_environment()
    assert os.environ["PATH"].split(os.pathsep) == [
        git_exec,
        "/custom/tools",
        git_exec,
        "/usr/bin",
        "/custom/tools",
    ]
    assert os.environ["GIT_CONFIG_PARAMETERS"] == "'core.hooksPath=.githooks' 'fixture.policy=keep'"


def test_posix_git_launch_keeps_interpreter_and_custom_path_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import test_gate

    git_exec = str(tmp_path / "git-exec")
    Path(git_exec).mkdir()
    interpreter = str(Path(sys.executable).parent)
    custom = ["/custom/tools", git_exec, "/usr/bin", "/custom/tools"]
    monkeypatch.setattr(test_gate.sys, "platform", "linux")
    monkeypatch.setenv("GIT_EXEC_PATH", git_exec)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([interpreter, git_exec, interpreter, git_exec, *custom])
    )
    test_gate.normalize_launch_environment()
    assert os.environ["PATH"].split(os.pathsep) == [interpreter, git_exec, interpreter, *custom]


@pytest.mark.parametrize("runtime", ["mingw64", "ucrt64"])
def test_windows_git_launch_uses_the_actual_runtime_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    from tools import test_gate

    root = tmp_path / "installed Git"
    directories = [
        root / runtime / "libexec/git-core",
        root / runtime / "bin",
        root / "usr/bin",
        root / "bin",
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    (root / "bin/git.exe").touch()
    monkeypatch.setattr(test_gate.sys, "platform", "win32")
    monkeypatch.setenv("GIT_EXEC_PATH", str(directories[0]))
    monkeypatch.setenv(
        "PATH", os.pathsep.join(str(p) for p in [*directories, *directories, root / "custom"])
    )
    test_gate.normalize_launch_environment()
    assert os.environ["PATH"].split(os.pathsep) == [
        os.path.normcase(os.path.normpath(str(p))) for p in [*directories, root / "custom"]
    ]
