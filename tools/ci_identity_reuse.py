"""Reuse successful identity jobs only for their actual, identical checkout tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

WORKFLOW = "mosswork-identity-ci.yml"
PROOF_JOB = "Already-tested main tree"
PLATFORMS = {
    "Canonical identity (ubuntu-24.04)": "Linux",
    "Canonical identity (windows-2022)": "Windows",
}
TEST_STEPS = {
    "Check canonical identity source",
    "Verify current membership, revocation and original IM identity",
}
SHA = re.compile(r"[a-f0-9]{40}")
Api = Callable[[str], bytes]


class Unavailable(ValueError):
    """Missing proof keeps the original complete platform matrix."""


def require(condition: Any, reason: str) -> None:
    if not condition:
        raise Unavailable(reason)


def checkout_commit(log: bytes) -> str:
    """Read checkout's original command output, before test-owned Git commands."""
    lines = log.decode("utf-8").splitlines()
    for index, line in enumerate(lines):
        if "[command]" in line and " log -1 --format=%H" in line:
            for following in lines[index + 1 : index + 4]:
                match = re.search(r"\s([a-f0-9]{40})\s*$", following)
                if match:
                    return match.group(1)
            raise Unavailable("The original checkout command has no complete commit output")
    raise Unavailable("The original job log does not identify its actual checkout")


def prove(repository: str, commit: str, tree: str, api: Api) -> dict[str, Any]:
    """Require both original jobs from the newest attempt; never substitute an old green."""
    require(re.fullmatch(r"[\w.-]+/[\w.-]+", repository), "Invalid repository identity")
    require(SHA.fullmatch(commit) and SHA.fullmatch(tree), "Invalid current source identity")
    prefix = "repos/" + repository + "/"

    def read(endpoint: str) -> Any:
        return json.loads(api(prefix + endpoint))

    associated = read("commits/" + commit + "/pulls?per_page=100")
    require(isinstance(associated, list), "The commit has no readable PR association")
    prs = [
        p
        for p in associated
        if p.get("merged_at")
        and p.get("merge_commit_sha") == commit
        and p.get("base", {}).get("ref") == "main"
        and p.get("base", {}).get("repo", {}).get("full_name") == repository
    ]
    require(len(prs) == 1, "This main commit has no single matching merged PR")
    pr = prs[0]
    head = pr["head"]["sha"]
    require(SHA.fullmatch(head), "The merged PR has no valid original source head")
    response = read(
        "actions/workflows/"
        + WORKFLOW
        + "/runs?event=pull_request&head_sha="
        + head
        + "&per_page=100"
    )
    runs = [
        r
        for r in response["workflow_runs"]
        if r.get("head_sha") == head
        and r.get("event") == "pull_request"
        and r.get("path", "").split("@", 1)[0] == ".github/workflows/" + WORKFLOW
        and r.get("repository", {}).get("full_name") == repository
    ]
    require(runs, "No original identity workflow exists for the merged source")
    run = max(runs, key=lambda r: (r["created_at"], int(r["id"])))
    require(
        run.get("status") == "completed" and run.get("conclusion") == "success",
        "The newest identity run is incomplete or failed; older successes cannot replace it",
    )
    run_id, attempt = int(run["id"]), int(run["run_attempt"])
    require(run_id > 0 and attempt > 0, "Invalid original run/attempt")
    jobs = read(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")["jobs"]
    require(len(jobs) <= 3, "The workflow matrix changed; run the current complete matrix")
    require(
        all(j.get("name") in {*PLATFORMS, PROOF_JOB} for j in jobs),
        "The workflow job inventory changed; run the current complete matrix",
    )
    for j in jobs:
        if j["name"] == PROOF_JOB:
            require(j.get("conclusion") == "skipped", "A PR must execute its original jobs")
    proofs = []
    trees: dict[str, str] = {}
    for name, platform in PLATFORMS.items():
        matches = [j for j in jobs if j.get("name") == name]
        require(len(matches) == 1, "Missing or ambiguous original " + platform + " job")
        job = matches[0]
        require(
            job.get("status") == "completed"
            and job.get("conclusion") == "success"
            and job.get("head_sha") == head
            and int(job.get("run_id", 0)) == run_id
            and int(job.get("run_attempt", 0)) == attempt,
            "The original " + platform + " job did not pass this source/attempt",
        )
        for step in TEST_STEPS:
            steps = [s for s in job.get("steps", []) if s.get("name") == step]
            require(
                len(steps) == 1
                and steps[0].get("status") == "completed"
                and steps[0].get("conclusion") == "success",
                "The original " + platform + " test command did not complete successfully",
            )
        job_id = int(job["id"])
        require(job_id > 0, "Invalid original job ID")
        log = api(prefix + f"actions/jobs/{job_id}/logs")
        require(len(log) <= 4 * 1024 * 1024, "The original job log exceeds the proof bound")
        actual = checkout_commit(log)
        if actual not in trees:
            trees[actual] = read("git/commits/" + actual)["tree"]["sha"]
        require(
            trees[actual] == tree,
            "The original " + platform + " checkout differs from the actual main tree",
        )
        proofs.append(
            {
                "platform": platform,
                "job": job_id,
                "checkout": actual,
                "tree": trees[actual],
                "originalLogSHA256": hashlib.sha256(log).hexdigest(),
            }
        )
    return {"pr": pr["number"], "run": run_id, "attempt": attempt, "jobs": proofs}


def decide(repository: str, commit: str, tree: str, api: Api) -> dict[str, Any]:
    result: dict[str, Any] = {"reuse": False, "commit": commit, "tree": tree}
    try:
        result["proof"] = prove(repository, commit, tree, api)
        result.update(reuse=True, reason="Both successful original platform checkouts match main")
    except (Unavailable, AttributeError, KeyError, TypeError, ValueError, UnicodeError) as error:
        result["reason"] = (
            str(error)
            if isinstance(error, Unavailable)
            else "Original proof is incomplete or malformed"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    deadline = time.monotonic() + 30

    def api(endpoint: str) -> bytes:
        remaining = deadline - time.monotonic()
        require(remaining > 0, "Proof lookup expired; run the original complete matrix")
        try:
            request = subprocess.run(
                ["gh", "api", endpoint],
                capture_output=True,
                timeout=min(10, remaining),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise Unavailable(
                "GitHub proof lookup unavailable; run the original complete matrix"
            ) from error
        require(
            request.returncode == 0,
            "GitHub proof lookup failed; run the original complete matrix",
        )
        return request.stdout

    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], timeout=10).decode().strip()
        tree = (
            subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], timeout=10)
            .decode()
            .strip()
        )
        require(head == args.commit, "The checked-out main commit differs from the event")
        changed = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"], timeout=10
        )
        require(not changed.strip(), "Source changed after checkout; run the complete matrix")
        result = decide(args.repository, args.commit, tree, api)
    except (Unavailable, OSError, subprocess.SubprocessError) as error:
        result = {
            "reuse": False,
            "commit": args.commit,
            "reason": str(error)
            if isinstance(error, Unavailable)
            else "Current source proof unavailable; run the complete matrix",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as target:
            target.write("reuse=" + str(result["reuse"]).lower() + "\n")
    print("CI_IDENTITY_REUSE: " + json.dumps(result), flush=True)
    if result["reuse"]:
        print(
            "The exact main tree already passed both original platform jobs. Reuse their recorded results."
        )
    else:
        print(
            "Proof unavailable: run every original Linux/Windows command. No manual retry is required."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
