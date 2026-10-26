"""Run the pipe-friendly installer with fake downloads and no host mutations."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Installer requires a POSIX shell")


@pytest.mark.parametrize(("machine", "arch"), [("x86_64", "amd64"), ("aarch64", "arm64")])
@pytest.mark.parametrize(
    "failure", [None, "deployment", "images", "missing-checksum", "load", "unsupported-arch"]
)
def test_installer_verifies_architecture_and_payloads_before_loading(
    tmp_path, machine, arch, failure
):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    archive = downloads / "moss-server-deploy-v1.0.1b2.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        data = b"# Deployment entry point\n"
        member = tarfile.TarInfo("moss-server/deploy.py")
        member.size = len(data)
        bundle.addfile(member, io.BytesIO(data))
    images = downloads / f"moss-server-images-v1.0.1b2-linux-{arch}.tar.gz"
    images.write_bytes(b"docker image archive")
    checksums = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in (archive, images)
    )
    if failure == "deployment":
        archive.write_bytes(archive.read_bytes() + b"tampered")
    elif failure == "images":
        images.write_bytes(images.read_bytes() + b"tampered")
    elif failure == "missing-checksum":
        checksums = checksums.splitlines()[0] + "\n"
    (downloads / "SHA256SUMS.txt").write_text(checksums)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    dispatcher = (
        f"#!{sys.executable}\n"
        + r"""
import json, os, pathlib, shutil, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ['INSTALLER_TEST_ROOT'])
if name == 'uname':
    print(os.environ['INSTALLER_TEST_MACHINE'] if args == ['-m'] else 'Linux')
elif name == 'id':
    print('0')
elif name == 'docker':
    if args[:2] == ['image', 'load']:
        if os.environ.get('INSTALLER_TEST_FAIL_LOAD') == '1':
            raise SystemExit('Image import failed')
        payload = pathlib.Path(args[args.index('--input') + 1])
        assert payload.read_bytes() == b'docker image archive'
        (root / 'loaded.txt').write_text(payload.name)
    elif args not in (['info'], ['compose', 'version']):
        raise SystemExit(f'Unexpected Docker operation: {args}')
elif name == 'curl':
    url = next(arg for arg in args if arg.startswith('https://'))
    with (root / 'requests.txt').open('a') as stream:
        stream.write(url + '\n')
    if '/releases/latest' in url:
        raise SystemExit('Pinned installer must never resolve latest')
    shutil.copyfile(root / 'downloads' / url.rsplit('/', 1)[1], args[args.index('-o') + 1])
elif name == 'python3':
    if args and pathlib.Path(args[0]).name == 'deploy.py':
        assert (root / 'loaded.txt').exists(), 'Images must be loaded before deployment'
        (root / 'deployed.json').write_text(json.dumps(args))
    else:
        os.execv(sys.executable, [sys.executable, *args])
"""
    )
    for name in ("uname", "id", "docker", "curl", "python3"):
        binary = binaries / name
        binary.write_text(dispatcher)
        binary.chmod(0o755)
    template = Path(__file__).resolve().parents[3] / "scripts/install-moss-server.sh"
    installer = tmp_path / "install.sh"
    installer.write_text(template.read_text().replace("@@MOSSWORK_RELEASE_TAG@@", "v1.0.1b2"))
    result = subprocess.run(
        ["bash", str(installer), "--port", "18088"],
        env={
            **os.environ,
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "INSTALLER_TEST_ROOT": str(tmp_path),
            "INSTALLER_TEST_MACHINE": "ppc64le" if failure == "unsupported-arch" else machine,
            "INSTALLER_TEST_FAIL_LOAD": "1" if failure == "load" else "0",
        },
        capture_output=True,
        text=True,
    )
    if failure:
        assert result.returncode != 0
        expected = {
            "load": "Image import failed",
            "unsupported-arch": "Only Linux amd64 and arm64 are supported",
        }.get(failure, "checksum mismatch")
        assert expected in result.stderr
        assert not (tmp_path / "deployed.json").exists()
        assert not (tmp_path / "loaded.txt").exists()
    else:
        assert result.returncode == 0, result.stderr
        arguments = json.loads((tmp_path / "deployed.json").read_text())
        assert arguments[1:] == ["--version", "v1.0.1b2", "--port", "18088"]
        assert (tmp_path / "loaded.txt").read_text() == images.name
    if failure == "unsupported-arch":
        assert not (tmp_path / "requests.txt").exists()
        return
    requests = (tmp_path / "requests.txt").read_text().splitlines()
    assert len(requests) == 3
    assert {url.rsplit("/", 1)[1] for url in requests} == {
        archive.name,
        images.name,
        "SHA256SUMS.txt",
    }
    assert all("/releases/download/v1.0.1b2/" in url for url in requests)
