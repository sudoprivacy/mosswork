"""Private assembly stays owned by Stack; the desktop carries only pinned bytes."""

import hashlib
import json

import pytest
from desktop.private_payload import copy_private_bundle


def bundle(root):
    files = {}
    for name in [
        "Start.command",
        "private_cloud_config.py",
        "private_macos_entry.py",
        "bin/scode",
        "bin/nexusd-cluster",
        "bin/nexus-agent-enroll",
        "bin/grpcurl",
        "python-runtime.tar.gz",
    ]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / "native-bundle.json").write_text(
        json.dumps(
            {"platform": "macos-arm64", "qualification": "engineering-candidate", "files": files}
        )
    )
    checksums = {
        **files,
        "native-bundle.json": hashlib.sha256(
            (root / "native-bundle.json").read_bytes()
        ).hexdigest(),
    }
    (root / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items()))
    )


def test_carries_original_manifest_and_payload_without_reassembly(tmp_path):
    source, target = tmp_path / "source", tmp_path / "app/Contents/Resources/private-bootstrap"
    bundle(source)
    receipt = copy_private_bundle(source, target, "arm64")
    assert receipt["qualification"] == "engineering-candidate"
    assert (
        receipt["manifest_sha256"]
        == hashlib.sha256((target / "native-bundle.json").read_bytes()).hexdigest()
    )
    assert (
        receipt["checksums_sha256"]
        == hashlib.sha256((target / "SHA256SUMS").read_bytes()).hexdigest()
    )
    for path in source.rglob("*"):
        if path.is_file():
            assert (target / path.relative_to(source)).read_bytes() == path.read_bytes()


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong-arch",
        "modified-script",
        "missing-cloud",
        "outside-root",
        "extra-python",
        "extra-directory",
        "checksums",
        "manifest-whitespace",
        "duplicate-checksum",
    ],
)
def test_refuses_unusable_payload_before_copy(tmp_path, mutation):
    source, target = tmp_path / "source", tmp_path / "app/private-bootstrap"
    bundle(source)
    manifest = json.loads((source / "native-bundle.json").read_text())
    if mutation == "wrong-arch":
        manifest["platform"] = "macos-x64"
    elif mutation == "modified-script":
        (source / "Start.command").write_text("different")
    elif mutation == "missing-cloud":
        del manifest["files"]["private_cloud_config.py"]
    elif mutation == "outside-root":
        manifest["files"]["../outside"] = "0" * 64
    elif mutation == "extra-python":
        (source / "sitecustomize.py").write_text("raise SystemExit('injected')")
    elif mutation == "extra-directory":
        (source / "unlisted").mkdir()
    elif mutation == "checksums":
        (source / "SHA256SUMS").write_text("replacement")
    elif mutation == "manifest-whitespace":
        with (source / "native-bundle.json").open("ab") as stream:
            stream.write(b"\n")
    elif mutation == "duplicate-checksum":
        checksums = (source / "SHA256SUMS").read_text()
        (source / "SHA256SUMS").write_text(checksums + checksums.splitlines()[0] + "\n")
    if mutation in {"wrong-arch", "missing-cloud", "outside-root"}:
        (source / "native-bundle.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        copy_private_bundle(source, target, "arm64")
    assert not target.exists()
