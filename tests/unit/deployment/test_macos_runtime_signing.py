import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
from desktop import macos_runtime_signing as signing


def test_signs_thin_and_universal_native_code_including_extensionless_files(tmp_path, monkeypatch):
    descriptions = {
        "python": "Mach-O 64-bit executable arm64",
        "module.so": "Mach-O universal binary with 2 architectures: [x86_64:Mach-O 64-bit bundle x86_64] [arm64:Mach-O 64-bit bundle arm64]",
        "library": "Mach-O 64-bit dynamically linked shared library arm64",
        "static.o": "Mach-O 64-bit object arm64",
        "Other.class": "compiled Java class data",
    }
    for name in descriptions:
        magic = "cafebabe" if name in {"Other.class", "module.so"} else "cffaedfe"
        (tmp_path / name).write_bytes(bytes.fromhex(magic))
    (tmp_path / "plain.py").write_text("pass")
    monkeypatch.setattr(
        subprocess, "check_output", lambda args, **kw: descriptions[Path(args[-1]).name]
    )
    signed = []
    verified = []

    def run(args, **kwargs):
        if "--sign" in args:
            assert kwargs["check"] and "--timestamp" in args and "runtime" in args
            signed.append(Path(args[-1]).name)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(signing, "verify_developer_binary", lambda p, t: verified.append(p.name))
    signing.sign_runtime_tree(tmp_path, "identity", "ABCDEFGHIJ")
    assert sorted(signed) == sorted(verified) == ["library", "module.so", "python"]


def test_bootstrap_pins_signed_archive_and_preserves_source_digest(tmp_path, monkeypatch):
    archive = tmp_path / "python-runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for name in ("python/bin/python3", "python/lib/native.so"):
            item = tarfile.TarInfo(name)
            item.size = 5
            output.addfile(item, io.BytesIO(b"input"))
    original = signing.digest(archive)
    metadata = tmp_path / "native-bundle.json"
    manifest = {"python": {"sha256": original}, "files": {archive.name: original}}
    metadata.write_text(json.dumps(manifest))

    def sign(root, identity, team):
        (root / "lib/native.so").write_bytes(b"signed library")

    monkeypatch.setattr(signing, "sign_runtime_tree", sign)
    result = signing.sign_bootstrap_python(tmp_path, "identity", "ABCDEFGHIJ", native=False)
    delivered = signing.digest(archive)
    assert delivered != original
    with tarfile.open(archive) as bundle:
        assert bundle.extractfile("python/lib/native.so").read() == b"signed library"
    updated = json.loads(metadata.read_bytes())
    assert updated["python"] == {"sourceSha256": original, "sha256": delivered}
    assert updated["files"][archive.name] == delivered
    checksums = tmp_path / "SHA256SUMS"
    assert checksums.read_text().splitlines() == [
        f"{signing.digest(metadata)}  native-bundle.json",
        f"{delivered}  python-runtime.tar.gz",
    ]
    assert result == {
        "manifest_sha256": signing.digest(metadata),
        "checksums_sha256": signing.digest(checksums),
    }
    archive.write_bytes(b"changed")
    with pytest.raises(ValueError, match="declared digest"):
        signing.sign_bootstrap_python(tmp_path, "identity", "ABCDEFGHIJ", native=False)
