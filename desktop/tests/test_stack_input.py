import hashlib
import json

import pytest
from desktop import stack_input


@pytest.fixture
def pin(tmp_path):
    archive = tmp_path / "stack.tar.gz"
    archive.write_bytes(b"stack bundle")
    manifest = {"platform": "macos-arm64", "qualification": "engineering-candidate"}
    manifest["sources"] = {"stack": "a" * 40}
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "native-bundle.json").write_text(json.dumps(manifest))
    value = {
        "platform": "macos-arm64",
        "release_tag": "bootstrap-input-macos-arm64-20261118",
        "asset_id": 7,
        "asset_name": "stack.tar.gz",
        "size": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256((bundle / "native-bundle.json").read_bytes()).hexdigest(),
        "qualification": "engineering-candidate",
        "sources": {"stack": "a" * 40},
    }
    path = tmp_path / "stack-input.json"
    path.write_text(json.dumps(value))
    return path, archive, bundle


def test_repository_pin_is_valid():
    assert stack_input.load()["platform"] == "macos-arm64"


def test_archive_and_asset_must_match_the_pin(pin):
    path, archive, _ = pin
    value = stack_input.load(path)
    metadata = {
        "id": 7,
        "name": "stack.tar.gz",
        "size": value["size"],
        "state": "uploaded",
        "digest": "sha256:" + value["sha256"],
    }
    stack_input.verify_archive(value, archive, metadata)
    with pytest.raises(ValueError, match="id"):
        stack_input.verify_archive(value, archive, {**metadata, "id": 8})
    archive.write_bytes(b"stack bundlf")
    with pytest.raises(ValueError, match="checksum"):
        stack_input.verify_archive(value, archive)


def test_bundle_manifest_must_match_the_pin(pin):
    path, _, bundle = pin
    value = stack_input.load(path)
    assert stack_input.verify_bundle(value, bundle)["sources"] == value["sources"]
    with pytest.raises(ValueError, match="sources"):
        stack_input.verify_bundle({**value, "sources": {"stack": "b" * 40}}, bundle)


@pytest.mark.parametrize(
    "change",
    [
        {"sha256": "0" * 63},
        {"asset_id": "7"},
        {"asset_name": "../stack.tar.gz"},
        {"sources": {"stack": "main"}},
        {"extra": 1},
    ],
)
def test_malformed_pin_is_rejected(pin, change):
    path, _, _ = pin
    path.write_text(json.dumps({**json.loads(path.read_text()), **change}))
    with pytest.raises(ValueError):
        stack_input.load(path)


def test_summary_marks_changed_fields(pin):
    path, _, _ = pin
    value = stack_input.load(path)
    text = stack_input.summary(value, {**value, "asset_id": 6})
    assert "| asset_id **(changed)** | `6` | `7` |" in text
    assert "sha256 **(changed)**" not in text
