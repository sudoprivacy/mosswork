"""Carry the existing Stack-produced Private bundle into the company installer."""

import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath


def copy_private_bundle(source: Path, destination: Path, arch: str) -> dict:
    source = source.resolve(strict=True)
    manifest_file = source / "native-bundle.json"
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise ValueError("Invalid Private bundle manifest")
    manifest_bytes = manifest_file.read_bytes()
    manifest = json.loads(manifest_bytes)
    platform = {"arm64": "macos-arm64", "amd64": "macos-x64"}[arch]
    if manifest.get("platform") != platform:
        raise ValueError("Private bundle architecture differs from the desktop")
    files = manifest.get("files", {})
    required = {
        "Start.command",
        "private_cloud_config.py",
        "private_macos_entry.py",
        "bin/scode",
        "bin/nexusd-cluster",
        "bin/nexus-agent-enroll",
        "bin/grpcurl",
        "python-runtime.tar.gz",
    }
    if not required.issubset(files):
        raise ValueError("Private bundle lacks the maintained Cloud bootstrap entry")
    for name, expected in files.items():
        path = Path(name)
        if (
            not name
            or name == "."
            or PurePosixPath(name).as_posix() != name
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or name in {"native-bundle.json", "SHA256SUMS"}
        ):
            raise ValueError("Private bundle path leaves its root")
        item = source / path
        if item.is_symlink() or not item.is_file() or not item.resolve().is_relative_to(source):
            raise ValueError("Invalid Private bundle member")
        if hashlib.sha256(item.read_bytes()).hexdigest() != expected:
            raise ValueError("Private bundle checksum differs: " + name)
    checksums_file = source / "SHA256SUMS"
    if checksums_file.is_symlink() or not checksums_file.is_file():
        raise ValueError("Invalid Private bundle checksums")
    checksums_bytes = checksums_file.read_bytes()
    expected_files = {**files, "native-bundle.json": hashlib.sha256(manifest_bytes).hexdigest()}
    checksums = {}
    for line in checksums_bytes.decode("utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or name in checksums:
            raise ValueError("Invalid Private bundle checksum list")
        checksums[name] = digest
    if checksums != expected_files:
        raise ValueError("Private bundle checksum list differs from its manifest")
    expected_files["SHA256SUMS"] = hashlib.sha256(checksums_bytes).hexdigest()
    directories = {
        parent.as_posix() for name in expected_files for parent in PurePosixPath(name).parents
    }
    for item in source.rglob("*"):
        name = item.relative_to(source).as_posix()
        if item.is_symlink() or (
            not (item.is_dir() and name in directories)
            and not (item.is_file() and name in expected_files)
        ):
            raise ValueError("Unlisted or redirected Private bundle member: " + name)
    destination.mkdir(parents=True)
    for name, expected in expected_files.items():
        item, target = source / name, destination / name
        if item.is_symlink() or not item.is_file():
            raise ValueError("Invalid Private bundle metadata")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError("Private bundle changed during packaging")
    return {
        "manifest_sha256": expected_files["native-bundle.json"],
        "checksums_sha256": expected_files["SHA256SUMS"],
        "platform": platform,
        "qualification": manifest.get("qualification"),
        "source": "sudostack private_macos_candidate.py",
    }
