"""Qualify the stable native feed against complete versioned release files."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from tools.desktop_update_manifest import FEED, manifest, verify


def release(tmp_path: Path) -> Path:
    for platform, arch, extension in (("macos", "arm64", "zip"), ("windows", "amd64", "exe")):
        (tmp_path / f"mosswork-company-{platform}-{arch}-0.0.8-update.{extension}").write_bytes(
            b"whole application and matching runtime"
        )
    return tmp_path


def test_feed_selects_exact_native_artifacts_and_hashes_the_actual_bytes(tmp_path):
    directory = release(tmp_path)
    feed = manifest(directory, "v0.0.8")
    assert feed["version"] == "0.0.8" and feed["channel"] == "stable"
    assert {(a["platform"], a["arch"]) for a in feed["artifacts"]} == {
        ("darwin", "arm64"),
        ("windows", "amd64"),
    }
    for artifact in feed["artifacts"]:
        actual = (directory / artifact["filename"]).read_bytes()
        assert base64.b64decode(artifact["digest"]) == hashlib.sha256(actual).digest()
        assert artifact["size"] == len(actual)
        assert artifact["url"] == f"../releases/v0.0.8/{artifact['filename']}"
    (directory / FEED).write_text(json.dumps(feed))
    verify(directory, "v0.0.8")


@pytest.mark.parametrize("tag", ["0.0.8", "v0.0.8rc1", "v0.0.8-rc.1", "vdev", "v00.0.8"])
def test_unqualified_or_prerelease_version_cannot_enter_stable_feed(tmp_path, tag):
    with pytest.raises(ValueError, match="stable"):
        manifest(release(tmp_path), tag)


def test_missing_or_modified_whole_runtime_artifact_fails_closed(tmp_path):
    directory = release(tmp_path)
    feed = manifest(directory, "v0.0.8")
    (directory / FEED).write_text(json.dumps(feed))
    artifact = directory / feed["artifacts"][1]["filename"]
    artifact.write_bytes(b"old runtime with a new shell")
    with pytest.raises(ValueError, match="actual complete"):
        verify(directory, "v0.0.8")
    artifact.unlink()
    with pytest.raises(FileNotFoundError):
        manifest(directory, "v0.0.8")
