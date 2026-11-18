import hashlib
import io
import tarfile

import pytest
from desktop.extract_bootstrap_release import extract


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "stack.tar.gz"
    with tarfile.open(path, "w:gz") as package:
        for name, content in (("native-bundle.json", b"{}"), ("SHA256SUMS", b"pins")):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            package.addfile(member, io.BytesIO(content))
    return path


def test_signed_release_input_is_pinned(archive, tmp_path):
    destination = tmp_path / "bootstrap"
    with pytest.raises(ValueError, match="checksum"):
        extract(archive, "0" * 64, destination)
    assert not destination.exists()
    pin = hashlib.sha256(archive.read_bytes()).hexdigest()
    extract(archive, pin, destination)
    assert (destination / "SHA256SUMS").read_bytes() == b"pins"
    with pytest.raises(FileExistsError):
        extract(archive, pin, destination)


def test_incomplete_bundle_leaves_no_destination(tmp_path):
    path = tmp_path / "incomplete.tar.gz"
    with tarfile.open(path, "w:gz") as package:
        package.addfile(tarfile.TarInfo("SHA256SUMS"))
    dest = tmp_path / "bundle"
    with pytest.raises(ValueError, match="root"):
        extract(path, hashlib.sha256(path.read_bytes()).hexdigest(), dest)
    assert not dest.exists()
