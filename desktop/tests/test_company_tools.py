"""Verify native tool archives are checked before any payload is installed."""

import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "company_tools", Path(__file__).resolve().parents[1] / "prepare-company-tools.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


def test_verified_archive_extracts_only_expected_tools_and_notices(tmp_path):
    archive = tmp_path / "ffmpeg.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name, data in (
            ("build/bin/ffmpeg.exe", b"binary"),
            ("build/LICENSE.txt", b"upstream license"),
            ("../../unrelated", b"must not be extracted"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    record = {
        "url": "fixture",
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "files": ["ffmpeg.exe"],
    }
    payload = tmp_path / "payload"
    payload.mkdir()
    module.install_archive(archive, record, payload)
    assert (payload / "tools/ffmpeg.exe").read_bytes() == b"binary"
    assert (payload / "third-party/ffmpeg/LICENSE.txt").read_text() == "upstream license"
    assert not (tmp_path / "unrelated").exists()


def test_checksum_failure_leaves_payload_untouched(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(b"wrong download")
    with pytest.raises(ValueError, match="checksum"):
        module.install_archive(archive, {"url": "fixture", "sha256": "0" * 64}, tmp_path)
    assert not (tmp_path / "tools").exists()
