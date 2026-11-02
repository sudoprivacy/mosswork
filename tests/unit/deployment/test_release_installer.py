"""Run the shell installer with local release files and a fake Docker CLI."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Installer requires a POSIX shell")
TAG = "v1.0.1b2"
DEPLOY = f"moss-server-deploy-{TAG}.tar.gz"
TEMPLATE = Path(__file__).resolve().parents[3] / "scripts/install-moss-server.sh"


@pytest.fixture
def installer(tmp_path):
    downloads = tmp_path / "release files"
    downloads.mkdir()
    with tarfile.open(downloads / DEPLOY, "w:gz") as bundle:
        data = b'#!/bin/sh\nprintf \'%s\\n\' "$@" > "$INSTALLER_TEST_ROOT/deployed.args"\n'
        member = tarfile.TarInfo("moss-server/deploy.sh")
        member.size = len(data)
        member.mode = 0o755
        bundle.addfile(member, io.BytesIO(data))
    for arch in ("amd64", "arm64"):
        (downloads / f"moss-server-images-{TAG}-linux-{arch}.tar.gz").write_bytes(
            b"docker image archive"
        )
    (downloads / "SHA256SUMS.txt").write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted(downloads.glob("*.tar.gz"))
        )
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # Restrict PATH so an offline run can prove curl is not required.
    for name in ("bash", "tar", "gzip", "sha256sum", "awk", "cut", "rm", "mktemp"):
        executable = shutil.which(name)
        assert executable is not None, f"Installer test requires {name}"
        (binaries / name).symlink_to(executable)
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
        (root / 'loaded.txt').write_text(str(payload))
    elif args not in (['info'], ['compose', 'version']):
        raise SystemExit(f'Unexpected Docker operation: {args}')
elif name == 'curl':
    with (root / 'requests.jsonl').open('a') as stream:
        stream.write(json.dumps(args) + '\n')
    if os.environ.get('INSTALLER_TEST_NO_NETWORK') == '1':
        raise SystemExit('Offline installation attempted a network request')
    url = next(arg for arg in args if arg.startswith('https://'))
    if '/releases/latest' in url:
        raise SystemExit('Pinned installer must never resolve latest')
    shutil.copyfile(root / 'release files' / url.rsplit('/', 1)[1], args[args.index('-o') + 1])
"""
    )
    for name in ("uname", "id", "docker", "curl"):
        binary = binaries / name
        binary.write_text(dispatcher)
        binary.chmod(0o755)
    script = tmp_path / "install.sh"
    script.write_text(TEMPLATE.read_text().replace("@@MOSSWORK_RELEASE_TAG@@", TAG))
    temporary = tmp_path / "temporary"
    temporary.mkdir()

    def run(*arguments, machine="x86_64", fail_load=False, no_network=True):
        before = {path.name: path.read_bytes() for path in downloads.iterdir()}
        env = {key: value for key, value in os.environ.items() if not key.startswith("MOSSWORK_")}
        result = subprocess.run(
            [str(binaries / "bash"), str(script), *arguments],
            cwd=downloads,
            env={
                **env,
                "PATH": str(binaries),
                "INSTALLER_TEST_ROOT": str(tmp_path),
                "TMPDIR": str(temporary),
                "INSTALLER_TEST_MACHINE": machine,
                "INSTALLER_TEST_FAIL_LOAD": "1" if fail_load else "0",
                "INSTALLER_TEST_NO_NETWORK": "1" if no_network else "0",
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert not list(temporary.iterdir()), "Installer must clean up its temporary files"
        assert before == {path.name: path.read_bytes() for path in downloads.iterdir()}
        if no_network:
            assert not (tmp_path / "requests.jsonl").exists()
        return result

    return downloads, script, run


@pytest.mark.parametrize(("machine", "arch"), [("x86_64", "amd64"), ("aarch64", "arm64")])
@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize(
    "failure", [None, "deployment", "images", "missing-checksum", "load", "unsupported-arch"]
)
def test_installer_verifies_architecture_and_payloads_before_loading(
    installer, tmp_path, machine, arch, offline, failure
):
    downloads, _, run = installer
    archive = downloads / DEPLOY
    images = downloads / f"moss-server-images-{TAG}-linux-{arch}.tar.gz"
    if failure == "deployment":
        archive.write_bytes(archive.read_bytes() + b"tampered")
    elif failure == "images":
        images.write_bytes(images.read_bytes() + b"tampered")
    elif failure == "missing-checksum":
        (downloads / "SHA256SUMS.txt").write_text(
            f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
        )
    arguments = ["--port", "18088"]
    if offline:
        arguments += ["--offline-dir", str(downloads)]
    result = run(
        *arguments,
        machine="ppc64le" if failure == "unsupported-arch" else machine,
        fail_load=failure == "load",
        no_network=offline,
    )
    if failure:
        assert result.returncode != 0
        expected = {
            "load": "Image import failed",
            "unsupported-arch": "Only Linux amd64 and arm64 are supported",
        }.get(failure, "checksum mismatch")
        assert expected in result.stderr
        assert not (tmp_path / "deployed.args").exists()
        assert not (tmp_path / "loaded.txt").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "deployed.args").read_text().splitlines() == [
            "--version",
            TAG,
            "--port",
            "18088",
        ]
        loaded = Path((tmp_path / "loaded.txt").read_text())
        assert loaded.name == images.name
        if offline:
            assert loaded == images, "Read large local archives without copying them"
    if offline or failure == "unsupported-arch":
        assert not (tmp_path / "requests.jsonl").exists()
    else:
        requests = [
            json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()
        ]
        urls = [next(arg for arg in args if arg.startswith("https://")) for args in requests]
        assert len(urls) == 3
        assert {url.rsplit("/", 1)[1] for url in urls} == {
            archive.name,
            images.name,
            "SHA256SUMS.txt",
        }
        assert all(f"/releases/download/{TAG}/" in url for url in urls)


@pytest.mark.parametrize("option", ["--offline", "--offline-dir", "--local-dir"])
def test_offline_works_without_curl_and_passes_deployment_options(installer, tmp_path, option):
    downloads, _, run = installer
    (tmp_path / "bin/curl").unlink()
    arguments = [option] if option == "--offline" else [option, str(downloads)]
    result = run(*arguments, "--install-dir", "/srv/test server", "--upgrade")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "deployed.args").read_text().splitlines() == [
        "--version",
        TAG,
        "--install-dir",
        "/srv/test server",
        "--upgrade",
    ]


def test_offline_can_use_already_loaded_images(installer, tmp_path):
    downloads, _, run = installer
    (downloads / f"moss-server-images-{TAG}-linux-amd64.tar.gz").unlink()
    result = run("--offline")
    assert result.returncode == 0, result.stderr
    assert "already-loaded Docker images" in result.stderr
    assert not (tmp_path / "loaded.txt").exists()
    assert (tmp_path / "deployed.args").exists()


@pytest.mark.parametrize("missing", [DEPLOY, "SHA256SUMS.txt"])
def test_offline_missing_files_fail_without_downloading_or_deploying(installer, tmp_path, missing):
    downloads, _, run = installer
    (downloads / missing).unlink()
    result = run("--offline")
    assert result.returncode != 0
    assert f"Offline file is missing: {downloads / missing}" in result.stderr
    assert not (tmp_path / "loaded.txt").exists()
    assert not (tmp_path / "deployed.args").exists()


def test_offline_source_installer_infers_unique_local_version(installer, tmp_path):
    _, script, run = installer
    script.write_text(TEMPLATE.read_text())
    result = run("--offline")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "deployed.args").read_text().splitlines() == ["--version", TAG]


def test_offline_multiple_versions_require_explicit_selection(installer, tmp_path):
    downloads, script, run = installer
    script.write_text(TEMPLATE.read_text())
    shutil.copyfile(downloads / DEPLOY, downloads / "moss-server-deploy-v2.0.0.tar.gz")
    result = run("--offline")
    assert result.returncode != 0
    assert "needs --version" in result.stderr
    assert not (tmp_path / "deployed.args").exists()
    result = run("--offline", "--version", TAG)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "arguments", [["--offline-dir"], ["--offline-dir", ""], ["--offline-dir", "absent"]]
)
def test_offline_invalid_directory_does_not_fall_back_to_network(installer, arguments):
    _, _, run = installer
    result = run(*arguments)
    assert result.returncode != 0
    assert "directory" in result.stderr.lower()
