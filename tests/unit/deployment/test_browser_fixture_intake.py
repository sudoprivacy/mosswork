"""Published fixtures need a successful linked parent before browser effects."""

from __future__ import annotations

import hashlib
import importlib
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def intake(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "tools"))
    return importlib.import_module("accept_browser_fixture")


def write(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_files(root: Path) -> tuple[Path, dict[str, Any]]:
    fixture = root / "browser-fixture"
    pointer = {"revision": "a" * 64, "context_digest": "b" * 64}
    bundle = {
        "ui": {
            "agent_context": pointer,
            "review_transport": {"endpoint": "/review/original/v1", "resource": "fixture"},
        }
    }
    payloads = {
        "model.yaml": "model: fixture",
        "model.json": json.dumps({"id": "canonical-object"}),
        "head.json": json.dumps(pointer),
        "context.json": json.dumps(pointer),
        "catalog.json": "{}",
        "record-page.request.json": json.dumps(
            {
                "operation": "page",
                "view": "fixture",
                "params": {**pointer, "id": "canonical-object"},
            }
        ),
        "record-page.response.json": json.dumps(
            {"result": {**pointer, "source_object": "canonical-object"}}
        ),
        "review.html": "canonical-object<script>const BUNDLE = " + json.dumps(bundle),
    }
    fixture.mkdir(parents=True)
    files = []
    for name, value in payloads.items():
        data = value.encode()
        (fixture / name).write_bytes(data)
        files.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {
        "format": "sudograph-browser-fixture/v1",
        **pointer,
        "resource_ref": "fixture",
        "worker_sha256": "c" * 64,
        "acceptance_script_sha256": "d" * 64,
        "credentials_or_databases_included": False,
        "files": files,
        "stable_targets": [{"id": "canonical-object"}],
        "source_and_model_store_unchanged": {"source.db": "e" * 64},
        "serving_contract": {"record_endpoint": "/review/original/v1"},
    }
    manifest_hash = write(fixture / "manifest.json", manifest)
    parent = {
        "format": "mosswork-sudograph-interop/v1",
        "passed": True,
        "revision": pointer["revision"],
        "worker_sha256": manifest["worker_sha256"],
        "acceptance_script_sha256": manifest["acceptance_script_sha256"],
        "checks": [
            {
                "name": "current_browser_fixture_emitted",
                "passed": True,
                **pointer,
                "manifest_sha256": manifest_hash,
            },
            {
                "name": "review_html_uses_same_revision_and_source_identity",
                "passed": True,
                "revision": pointer["revision"],
            },
            {"name": "raw_source_bytes_unchanged", "passed": True, "sha256": "e" * 64},
        ],
    }
    parent_hash = write(root / "receipt.json", parent)
    return fixture, {
        "manifest_sha256": manifest_hash,
        "parent_sha256": parent_hash,
        "acceptance_script_sha256": manifest["acceptance_script_sha256"],
    }


@pytest.mark.parametrize(
    "defect",
    ["failed_parent", "failed_check", "foreign_manifest", "mixed_pointer", "duplicate_event"],
)
def test_refuses_invalid_parent_even_when_its_bytes_match_the_pin(
    tmp_path: Path, intake: Any, defect: str
) -> None:
    fixture, pins = fixture_files(tmp_path)
    parent_path = tmp_path / "receipt.json"
    parent = intake.read(parent_path)
    if defect == "failed_parent":
        parent["passed"] = False
    elif defect == "failed_check":
        parent["checks"][1]["passed"] = False
    elif defect == "foreign_manifest":
        parent["checks"][0]["manifest_sha256"] = "f" * 64
    elif defect == "mixed_pointer":
        parent["checks"][0]["context_digest"] = "f" * 64
    else:
        parent["checks"].append(parent["checks"][0])
    pins["parent_sha256"] = write(parent_path, parent)
    with pytest.raises(ValueError):
        intake.verify_fixture(fixture, parent_path, pins)


@pytest.mark.parametrize("defect", ["missing_parent", "altered_html", "missing_digest"])
def test_payload_presence_does_not_replace_parent_or_pointer(
    tmp_path: Path, intake: Any, defect: str
) -> None:
    fixture, pins = fixture_files(tmp_path)
    parent_path = tmp_path / "receipt.json"
    if defect == "missing_parent":
        parent_path.unlink()
    elif defect == "altered_html":
        (fixture / "review.html").write_text("altered", encoding="utf-8")
    else:
        manifest = intake.read(fixture / "manifest.json")
        del manifest["context_digest"]
        pins["manifest_sha256"] = write(fixture / "manifest.json", manifest)
    with pytest.raises((ValueError, FileNotFoundError)):
        intake.verify_fixture(fixture, parent_path, pins)


def archive_inputs(tmp_path: Path, intake: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    fixture, pins = fixture_files(tmp_path / "producer")
    archive = tmp_path / "artifact.zip"
    with zipfile.ZipFile(archive, "w") as packed:
        for path in fixture.parent.rglob("*"):
            if path.is_file():
                packed.write(
                    path, "sudograph-acceptance/" + path.relative_to(fixture.parent).as_posix()
                )
    pins.update(
        artifact_id=1, archive_sha256=intake.sha256(archive), archive_bytes=archive.stat().st_size
    )
    lock = tmp_path / "inputs.json"
    write(
        lock,
        {
            "source_head": "head",
            "source_tree": "tree",
            "run_id": 1,
            "platforms": {"Linux": pins, "Windows": {**pins, "archive_sha256": "f" * 64}},
        },
    )
    monkeypatch.setattr(intake, "INPUTS", lock)
    return archive


def test_stage_keeps_exact_html_and_rechecks_parent_at_browser_boundary(
    tmp_path: Path, intake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = archive_inputs(tmp_path, intake, monkeypatch)
    output = tmp_path / "staged"
    receipt = intake.stage_archive(archive, "Linux", output)
    assert receipt["payloadsVerified"] == 8
    fixture = output / "browser-fixture"
    assert (fixture / "review.html").read_bytes() == (
        tmp_path / "producer/browser-fixture/review.html"
    ).read_bytes()
    assert intake.verify_staged(fixture, output / "intake.json") == receipt
    review = importlib.import_module("accept_browser_review").ReviewFixture(
        fixture, receipt["manifestSha256"], output / "intake.json"
    )
    assert review.endpoint == "/review/original/v1"
    (output / "receipt.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Parent digest"):
        review.verify()


@pytest.mark.parametrize("defect", ["altered_archive", "other_platform", "existing_output"])
def test_staging_refusal_preserves_existing_files_and_creates_no_fixture(
    tmp_path: Path, intake: Any, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    archive = archive_inputs(tmp_path, intake, monkeypatch)
    output = tmp_path / "staged"
    platform = "Linux"
    if defect == "altered_archive":
        archive.write_bytes(archive.read_bytes() + b"altered")
    elif defect == "other_platform":
        platform = "Windows"
    else:
        output.mkdir()
        (output / "retained").write_bytes(b"unchanged")
    with pytest.raises((ValueError, KeyError)):
        intake.stage_archive(archive, platform, output)
    assert not (output / "browser-fixture").exists()
    if output.exists():
        assert (output / "retained").read_bytes() == b"unchanged"


def test_missing_parent_in_zip_is_refused_before_staging(
    tmp_path: Path, intake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = archive_inputs(tmp_path, intake, monkeypatch)
    data = (tmp_path / "producer/browser-fixture/review.html").read_bytes()
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr("sudograph-acceptance/browser-fixture/review.html", data)
    inputs = intake.read(intake.INPUTS)
    inputs["platforms"]["Linux"].update(
        archive_sha256=intake.sha256(archive), archive_bytes=archive.stat().st_size
    )
    write(intake.INPUTS, inputs)
    output = tmp_path / "no-parent"
    with pytest.raises(ValueError, match="Missing parent"):
        intake.stage_archive(archive, "Linux", output)
    assert not output.exists()
