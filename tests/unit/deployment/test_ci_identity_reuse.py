"""Original job/source counterexamples keep duplicate-run reuse fail closed."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from typing import Any

import pytest
from tools import ci_identity_reuse as reuse

REPOSITORY = "fixture/project"
MAIN, HEAD, CHECKOUT, TREE = (c * 40 for c in "abcd")
PREFIX = "repos/" + REPOSITORY + "/"


@pytest.fixture
def original() -> dict[str, Any]:
    jobs = []
    for index, name in enumerate(reuse.PLATFORMS, 1):
        jobs.append(
            {
                "id": index,
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "head_sha": HEAD,
                "run_id": 10,
                "run_attempt": 1,
                "steps": [
                    {
                        "name": "Run actions/checkout@v4",
                        "number": 2,
                        "status": "completed",
                        "conclusion": "success",
                    },
                ]
                + [
                    {"name": step, "status": "completed", "conclusion": "success"}
                    for step in reuse.TEST_STEPS
                ],
            }
        )
    return {
        "prs": [
            {
                "number": 42,
                "merged_at": "2026-11-12T17:00:00Z",
                "merge_commit_sha": MAIN,
                "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
                "head": {"sha": HEAD},
            }
        ],
        "runs": [
            {
                "id": 10,
                "run_attempt": 1,
                "head_sha": HEAD,
                "event": "pull_request",
                "repository": {"full_name": REPOSITORY},
                "path": ".github/workflows/" + reuse.WORKFLOW,
                "created_at": "2026-11-12T16:00:00Z",
                "status": "completed",
                "conclusion": "success",
            }
        ],
        "jobs": jobs,
        "checkouts": {CHECKOUT: TREE},
        "logs": {
            index: (
                "2026-11-12T16:00:00.000Z [command]"
                + ("/usr/bin/git" if index == 1 else '"C:\\Program Files\\Git\\bin\\git.exe"')
                + " log -1 --format=%H\n2026-11-12T16:00:00.001Z "
                + CHECKOUT
                + "\n"
            ).encode()
            for index in (1, 2)
        },
    }


def api_for(source: dict[str, Any]):
    def api(endpoint: str) -> bytes:
        assert endpoint.startswith(PREFIX), endpoint
        path = endpoint.removeprefix(PREFIX)
        if path.startswith("commits/") and "/pulls?" in path:
            value = source["prs"]
        elif path.startswith("actions/workflows/"):
            value = {"workflow_runs": source["runs"]}
        elif path.startswith("actions/runs/"):
            value = {"jobs": source["jobs"]}
        elif path.startswith("actions/jobs/"):
            job_id = int(path.split("/")[2])
            job = next(j for j in source["jobs"] if j["id"] == job_id)
            step = next(s for s in job["steps"] if s["name"] == "Run actions/checkout@v4")
            assert path == f"actions/jobs/{job_id}/steps/{step['number']}/logs", endpoint
            return source["logs"][job_id]
        elif path.startswith("git/commits/"):
            value = {"tree": {"sha": source["checkouts"][path.rsplit("/", 1)[-1]]}}
        else:
            raise AssertionError(endpoint)
        return json.dumps(value).encode()

    return api


def decision(source: dict[str, Any]) -> dict[str, Any]:
    return reuse.decide(REPOSITORY, MAIN, TREE, api_for(source))


def test_both_actual_platform_checkouts_may_differ_from_main_commit_but_match_its_tree(original):
    result = decision(original)
    assert result["reuse"] is True
    assert CHECKOUT != MAIN
    assert {j["platform"] for j in result["proof"]["jobs"]} == {"Linux", "Windows"}
    assert all(j["checkout"] == CHECKOUT and j["tree"] == TREE for j in result["proof"]["jobs"])


@pytest.mark.parametrize("platform", [0, 1])
def test_one_different_actual_tree_refuses_even_with_successful_jobs(original, platform):
    other = "e" * 40
    original["logs"][platform + 1] = original["logs"][platform + 1].replace(
        CHECKOUT.encode(), other.encode()
    )
    original["checkouts"][other] = "f" * 40
    assert decision(original)["reuse"] is False


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", None])
def test_newest_failure_or_incomplete_run_is_not_replaced_by_an_older_green(original, conclusion):
    newer = copy.deepcopy(original["runs"][0])
    newer.update(id=11, created_at="2026-11-12T16:01:00Z", conclusion=conclusion)
    original["runs"].append(newer)
    assert decision(original)["reuse"] is False


def test_missing_platform_partial_rerun_and_duplicate_job_all_refuse(original):
    for jobs in (original["jobs"][:1], original["jobs"] + [original["jobs"][0]]):
        changed = copy.deepcopy(original)
        changed["jobs"] = jobs
        assert decision(changed)["reuse"] is False
    original["runs"][0]["run_attempt"] = 2
    assert decision(original)["reuse"] is False


@pytest.mark.parametrize("mutation", ["new-platform", "failed-step", "wrong-head", "failed-job"])
def test_job_matrix_and_original_command_proof_are_required(original, mutation):
    if mutation == "new-platform":
        original["jobs"].append({"name": "Canonical identity (macos-15)"})
    elif mutation == "failed-step":
        next(step for step in original["jobs"][0]["steps"] if step["name"] in reuse.TEST_STEPS)[
            "conclusion"
        ] = "failure"
    elif mutation == "wrong-head":
        original["jobs"][0]["head_sha"] = "e" * 40
    else:
        original["jobs"][0]["conclusion"] = "failure"
    assert decision(original)["reuse"] is False


def test_direct_main_push_unrelated_pr_and_missing_checkout_all_keep_full_matrix(original):
    for key, value in (
        ("prs", []),
        ("prs", [{**original["prs"][0], "merge_commit_sha": HEAD}]),
        ("logs", {1: b"not an original checkout", 2: b""}),
    ):
        changed = copy.deepcopy(original)
        changed[key] = value
        assert decision(changed)["reuse"] is False


def test_actual_checkout_precedes_any_test_owned_git_log(original):
    original["logs"][1] += (
        b"2026-11-12T16:00:02Z [command]git log -1 --format=%H\n2026-11-12T16:00:03Z "
        + b"e" * 40
        + b"\n"
    )
    assert decision(original)["reuse"] is True


def test_skipped_proof_job_does_not_replace_the_two_original_pr_jobs(original):
    original["jobs"].append({"name": reuse.PROOF_JOB, "conclusion": "skipped"})
    assert decision(original)["reuse"] is True
    original["jobs"][-1]["conclusion"] = "success"
    assert decision(original)["reuse"] is False


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "failed", "unfinished", "zero", "boolean"]
)
def test_only_one_successful_numbered_checkout_step_is_eligible(original, mutation):
    steps = original["jobs"][0]["steps"]
    checkout = steps[0]
    if mutation == "missing":
        steps.pop(0)
    elif mutation == "duplicate":
        steps.append(copy.deepcopy(checkout))
    elif mutation == "failed":
        checkout["conclusion"] = "failure"
    elif mutation == "unfinished":
        checkout["status"] = "in_progress"
    else:
        checkout["number"] = 0 if mutation == "zero" else True
    assert decision(original)["reuse"] is False


@pytest.mark.parametrize("control", [b"\x1b[31m", b"\x07", "\u009b".encode()])
def test_checkout_terminal_controls_stay_refused_even_with_an_older_cli(original, control):
    original["logs"][1] += control
    assert decision(original)["reuse"] is False


@pytest.mark.parametrize("response", [b"[null]", b"{}", b"not json", b"null"])
def test_unreadable_external_proof_keeps_the_full_matrix(response):
    assert reuse.decide(REPOSITORY, MAIN, TREE, lambda endpoint: response)["reuse"] is False


def test_normal_cli_binds_a_real_git_checkout_and_keeps_api_failure_full(
    tmp_path, monkeypatch, capsys, original
):
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init", "-q"),
        (
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "-c",
            "core.hooksPath=empty-hooks",
            "commit",
            "--allow-empty",
            "-qm",
            "source",
        ),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=root).decode().strip()
    original["prs"][0]["merge_commit_sha"] = commit
    original["checkouts"][CHECKOUT] = tree
    monkeypatch.chdir(root)
    output, github = tmp_path / "receipt.json", tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github))
    monkeypatch.setenv("GH_TOKEN", "synthetic-token-must-not-be-logged")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ci_identity_reuse.py",
            "--repository",
            REPOSITORY,
            "--commit",
            commit,
            "--output",
            str(output),
        ],
    )
    actual_run = subprocess.run

    def run(argv, **kwargs):
        if argv[:2] == ["gh", "api"]:
            assert "--allow-escape-sequences" not in argv
            if argv[2].endswith(("/jobs/1/logs", "/jobs/2/logs")):
                return subprocess.CompletedProcess(
                    argv, 1, b"", b"the response contains terminal escape sequences"
                )
            return subprocess.CompletedProcess(argv, 0, api_for(original)(argv[2]), b"")
        return actual_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert reuse.main() == 0
    assert json.loads(output.read_text())["reuse"] is True
    assert github.read_text() == "reuse=true\n"

    def failed(argv, **kwargs):
        if argv[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(
                argv, 1, b"", b"HTTP 403 synthetic-token-must-not-be-logged"
            )
        return actual_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", failed)
    assert reuse.main() == 0
    assert json.loads(output.read_text())["reuse"] is False
    assert github.read_text().endswith("reuse=false\n")
    text = capsys.readouterr().out
    assert "synthetic-token-must-not-be-logged" not in text
    assert "No manual retry is required" in text
    assert "HTTP 403" in text and "commits/" in text
    (root / "uncommitted.py").write_text("raise RuntimeError('source drift')\n")

    def no_api_for_drift(argv, **kwargs):
        assert argv[:2] != ["gh", "api"], "Refuse source drift before reading a prior success"
        return actual_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", no_api_for_drift)
    assert reuse.main() == 0
    assert json.loads(output.read_text())["reuse"] is False
    assert "Source changed after checkout" in capsys.readouterr().out
