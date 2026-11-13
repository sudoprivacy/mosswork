"""Stage a digest-selected published compiler fixture for browser acceptance.

The parent/event/payload checks reuse SW3's reviewed read-only fixture intake.
No worker, compiler, renderer, model or credential provider is started here.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

INPUTS = Path(__file__).with_suffix(".json")
PAYLOADS = {
    "review.html",
    "model.yaml",
    "model.json",
    "head.json",
    "context.json",
    "catalog.json",
    "record-page.request.json",
    "record-page.response.json",
}


def require(condition: Any, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def verify_fixture(fixture: Path, parent_path: Path, pins: dict[str, Any]) -> dict[str, Any]:
    """Check original parent success and its linkage to unchanged emitted bytes."""
    require(
        sha256(fixture / "manifest.json") == pins["manifest_sha256"], "Manifest digest mismatch"
    )
    require(sha256(parent_path) == pins["parent_sha256"], "Parent digest mismatch")
    manifest, parent = read(fixture / "manifest.json"), read(parent_path)
    require(manifest.get("format") == "sudograph-browser-fixture/v1", "Unsupported fixture")
    require(parent.get("format") == "mosswork-sudograph-interop/v1", "Unsupported parent")
    require(parent.get("passed") is True, "Parent did not pass")
    require(manifest.get("credentials_or_databases_included") is False, "Private material included")
    require(
        manifest.get("acceptance_script_sha256")
        == parent.get("acceptance_script_sha256")
        == pins["acceptance_script_sha256"],
        "Acceptance source mismatch",
    )
    require(manifest.get("worker_sha256") == parent.get("worker_sha256"), "Worker mismatch")
    checks = parent.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("Parent checks missing")
    require(
        all(isinstance(row, dict) and row.get("passed") is True for row in checks),
        "Parent check failed or incomplete",
    )
    names = [row.get("name") for row in checks]
    require(
        all(isinstance(name, str) for name in names) and len(names) == len(set(names)),
        "Ambiguous parent checks",
    )
    events = {row["name"]: row for row in checks}
    require(
        {
            "current_browser_fixture_emitted",
            "review_html_uses_same_revision_and_source_identity",
            "raw_source_bytes_unchanged",
        }
        <= events.keys(),
        "Fixture/source events missing",
    )
    pointer = {key: manifest.get(key) for key in ("revision", "context_digest")}
    require(
        all(isinstance(value, str) and len(value) == 64 for value in pointer.values()),
        "Invalid pointer",
    )
    emitted = events["current_browser_fixture_emitted"]
    require(emitted.get("manifest_sha256") == pins["manifest_sha256"], "Unlinked manifest")
    require(parent.get("revision") == pointer["revision"], "Parent revision mismatch")
    require(all(emitted.get(key) == value for key, value in pointer.items()), "Unlinked pointer")
    require(
        events["review_html_uses_same_revision_and_source_identity"].get("revision")
        == pointer["revision"],
        "Review event revision mismatch",
    )
    members = manifest.get("files")
    if not isinstance(members, list) or len(members) != len(PAYLOADS):
        raise ValueError("Incomplete payload list")
    seen: set[str] = set()
    for row in members:
        name = row["path"]
        require(name in PAYLOADS and name not in seen, "Unexpected or duplicate payload")
        path = fixture / name
        require(not path.is_symlink(), "Symlink payload")
        require(
            sha256(path) == row["sha256"] and path.stat().st_size == row["bytes"],
            f"Payload digest/size mismatch: {name}",
        )
        seen.add(name)
    require(seen == PAYLOADS, "Missing payload")
    require(read(fixture / "head.json") == pointer, "Head mismatch")
    context = read(fixture / "context.json")
    require(all(context.get(key) == value for key, value in pointer.items()), "Context mismatch")
    request = read(fixture / "record-page.request.json")
    page = read(fixture / "record-page.response.json")["result"]
    require(
        request.get("operation") == "page" and request.get("view") == manifest["resource_ref"],
        "Page resource mismatch",
    )
    require(
        all(request["params"].get(key) == value for key, value in pointer.items()),
        "Request pointer mismatch",
    )
    require(all(page.get(key) == value for key, value in pointer.items()), "Page pointer mismatch")
    require(page.get("source_object") == request["params"].get("id"), "Page object mismatch")
    ids = [target.get("id") for target in manifest.get("stable_targets", [])]
    require(
        ids and all(isinstance(value, str) for value in ids) and len(ids) == len(set(ids)),
        "Invalid stable targets",
    )
    model, html = (
        (fixture / "model.json").read_text(encoding="utf-8"),
        (fixture / "review.html").read_text(encoding="utf-8"),
    )
    require(all(target in model and target in html for target in ids), "Missing stable target")
    require(
        events["raw_source_bytes_unchanged"].get("sha256")
        == manifest["source_and_model_store_unchanged"]["source.db"],
        "Raw source event mismatch",
    )
    return {
        "manifestSha256": pins["manifest_sha256"],
        "parentSha256": pins["parent_sha256"],
        "acceptanceScriptSha256": pins["acceptance_script_sha256"],
        "workerSha256": manifest["worker_sha256"],
        "parentChecksPassed": len(checks),
        "payloadsVerified": len(seen),
        "stableTargetsVerified": len(ids),
        **pointer,
        "servingContract": manifest["serving_contract"],
    }


def stage_archive(archive: Path, platform: str, output: Path) -> dict[str, Any]:
    inputs = read(INPUTS)
    pins = inputs["platforms"][platform]
    require(not output.exists(), "Require a new output directory")
    data = archive.read_bytes()
    require(
        len(data) == pins["archive_bytes"]
        and hashlib.sha256(data).hexdigest() == pins["archive_sha256"],
        "Published archive digest/size mismatch",
    )
    prefix = "sudograph-acceptance/"
    names = {prefix + "receipt.json"} | {
        prefix + "browser-fixture/" + name for name in PAYLOADS | {"manifest.json"}
    }
    with tempfile.TemporaryDirectory(prefix="browser-fixture-") as temporary:
        candidate = Path(temporary)
        with zipfile.ZipFile(io.BytesIO(data)) as packed:
            require(len(packed.namelist()) == len(set(packed.namelist())), "Duplicate ZIP members")
            require(names <= set(packed.namelist()), "Missing parent or fixture in archive")
            for name in sorted(names):
                member = packed.getinfo(name)
                require(member.external_attr >> 16 & 0o170000 != 0o120000, "Symlink ZIP member")
                target = candidate / name.removeprefix(prefix)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(packed.read(member))
        details = verify_fixture(candidate / "browser-fixture", candidate / "receipt.json", pins)
        # The existing renderer consumer also checks the original inline bundle
        # and endpoint; this reads bytes without starting a browser or renderer.
        from accept_browser_review import ReviewFixture  # noqa: PLC0415

        ReviewFixture(candidate / "browser-fixture", pins["manifest_sha256"])
        output.mkdir(mode=0o700)
        for path in candidate.rglob("*"):
            target = output / path.relative_to(candidate)
            if path.is_dir():
                target.mkdir()
            else:
                shutil.copyfile(path, target)
    receipt = {
        "format": "mosswork-published-browser-fixture-intake/v1",
        "passed": True,
        "platform": platform,
        "sourceHead": inputs["source_head"],
        "sourceTree": inputs["source_tree"],
        "runId": inputs["run_id"],
        "artifactId": pins["artifact_id"],
        "archiveSha256": pins["archive_sha256"],
        "inputLockSha256": sha256(INPUTS),
        **details,
        "scope": "Published synthetic fixture integrity only; browser effects, current grants, comments, native and production acceptance remain separate.",
    }
    (output / "intake.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def verify_staged(fixture: Path, intake: Path) -> dict[str, Any]:
    """Recheck the selected original parent/payloads before and after browser I/O."""
    receipt, inputs = read(intake), read(INPUTS)
    pins = inputs["platforms"][receipt["platform"]]
    require(receipt.get("passed") is True, "Intake did not pass")
    for key, expected in {
        "inputLockSha256": sha256(INPUTS),
        "sourceHead": inputs["source_head"],
        "sourceTree": inputs["source_tree"],
        "runId": inputs["run_id"],
        "artifactId": pins["artifact_id"],
        "archiveSha256": pins["archive_sha256"],
    }.items():
        require(receipt.get(key) == expected, f"Intake binding mismatch: {key}")
    details = verify_fixture(fixture, intake.parent / "receipt.json", pins)
    require(
        all(receipt.get(key) == value for key, value in details.items()), "Intake metadata changed"
    )
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--platform", choices=sorted(read(INPUTS)["platforms"]), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    receipt = stage_archive(args.archive, args.platform, output)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"fixture={(output / 'browser-fixture').as_posix()}\n")
            stream.write(f"manifest_sha256={receipt['manifestSha256']}\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
