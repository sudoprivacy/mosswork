"""A checked merge must bind the actual local action, including base advances."""

from pathlib import Path

import pytest
from tests.unit.deployment.test_precommit import (
    ci_commit,
    gate,
    git,
    seed_windows_validation_workflow,
    stage,
)
from tests.unit.deployment.test_precommit import (
    repo as repo,
)


@pytest.mark.parametrize(
    "action_state", ["complete", "missing", "modified", "nonregular", "feature-modified"]
)
def test_tool_scope_binds_the_checked_local_action_revision(repo: Path, action_state: str) -> None:
    seed_windows_validation_workflow(repo)
    common = git(repo, "rev-parse", "HEAD")
    action = gate.WINDOWS_VALIDATION_RECIPE_ACTIONS[0]
    original = (repo / action).read_text(encoding="utf-8")
    git(repo, "checkout", "-qb", "accepted-base")
    if action_state == "missing":
        git(repo, "rm", action)
    elif action_state == "modified":
        stage(repo, action, original + "\n# Different accepted-base implementation.\n")
    elif action_state == "nonregular":
        # Test the committed mode on every platform without requiring OS symlink privileges.
        git(repo, "config", "core.symlinks", "false")
        blob = git(repo, "rev-parse", f"HEAD:{action}")
        git(repo, "update-index", "--cacheinfo", f"120000,{blob},{action}")
    else:
        stage(repo, "src/octop/accepted.py", "ACCEPTED = True\n")
    ci_commit(repo, "accepted base advancement")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "tool-feature", common)
    stage(repo, "tools/check_public_source.py", "# Independent tool payload.\n")
    if action_state == "feature-modified":
        stage(repo, action, original + "\n# Action changed by the feature itself.\n")
    ci_commit(repo, "tool feature")
    head = git(repo, "rev-parse", "HEAD")
    git(
        repo,
        "-c",
        "core.hooksPath=" + str(repo / "fixture-hooks"),
        "merge",
        "--no-edit",
        "accepted-base",
    )
    checked = git(repo, "rev-parse", "HEAD")
    plan = gate.ci_plan(repo, base, head)
    assert plan["checked_head"] == checked
    assert plan["runtime_matrix"] is (action_state != "complete")
    assert plan["windows_validation"] is (action_state == "complete")
    if action_state in {"missing", "modified", "nonregular"}:
        assert plan["paths"] == ["tools/check_public_source.py"]
        assert "recipe" in plan["reason"]
    elif action_state == "feature-modified":
        assert action in plan["paths"]
