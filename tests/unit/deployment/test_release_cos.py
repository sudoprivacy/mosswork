"""Exercise the real publisher and curl against an HTTP object-store fixture."""

from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Publisher requires a POSIX shell")
SCRIPT = Path(__file__).resolve().parents[3] / "tools/publish-release-to-cos.sh"
TAG = "v0.0.7"
CLIENTS = {
    "mosswork-latest-macos-arm64.dmg": "mosswork-company-macos-arm64-0.0.7.dmg",
    "mosswork-latest-windows-amd64.exe": "mosswork-company-windows-amd64-0.0.7.exe",
}


@pytest.fixture
def publisher(tmp_path):
    assets = tmp_path / "release files"
    assets.mkdir()
    payloads = {
        "install-moss-server.sh": b"#!/bin/sh\n",
        f"moss-server-deploy-{TAG}.tar.gz": b"deployment archive\x00",
        f"moss-server-images-{TAG}-linux-amd64.tar.gz": b"server archive\x00",
        "moss-server-linux-amd64-images.json": b'{"architecture":"amd64"}',
        CLIENTS["mosswork-latest-macos-arm64.dmg"]: b"DMG\x00binary installer",
        CLIENTS["mosswork-latest-windows-amd64.exe"]: b"MZ\x00binary installer",
    }
    for name, data in payloads.items():
        (assets / name).write_bytes(data)
    (assets / "SHA256SUMS.txt").write_text(
        "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in payloads.items())
    )
    storage = tmp_path / "objects"
    storage.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    command = binaries / "coscmd"
    command.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
assert args[:2] == ['upload', '-H'] and len(args) == 5, args
headers = json.loads(args[2])
key = pathlib.PurePosixPath(args[4])
assert not key.is_absolute() and '..' not in key.parts
destination = pathlib.Path(os.environ['COS_TEST_STORAGE']).joinpath(*key.parts)
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(args[3], destination)
destination.with_name(destination.name + '.headers.json').write_text(json.dumps(headers))
"""
    )
    command.chmod(0o755)
    header_overrides = {}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def metadata(self, path):
            source = Path(path)
            metadata_file = source.with_name(source.name + ".headers.json")
            result = json.loads(metadata_file.read_text()) if metadata_file.is_file() else {}
            if source.name == "mosswork-latest-windows-amd64.exe":
                result.update(header_overrides)
            return result

        def guess_type(self, path):
            return self.metadata(path).get("Content-Type") or "application/xml"

        def end_headers(self):
            for name, value in self.metadata(self.translate_path(self.path)).items():
                if name != "Content-Type" and value:
                    self.send_header(name, value)
            super().end_headers()

        def log_message(self, _format, *_args):
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(storage))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def run(overrides=None):
        header_overrides.update(overrides or {})
        return subprocess.run(
            ["bash", str(SCRIPT), str(assets), TAG, "--all"],
            env={
                **os.environ,
                "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                "NO_PROXY": "127.0.0.1",
                "COS_PUBLIC_BASE_URL": base,
                "COS_ROOT_PATH": "mosswork/release",
                "COS_TEST_STORAGE": str(storage),
            },
            capture_output=True,
            text=True,
            timeout=45,
        )

    try:
        yield run, base, storage, payloads
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_actual_publisher_preserves_download_headers_bytes_and_cache_policy(publisher):
    run, base, _storage, payloads = publisher
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    for alias, original in CLIENTS.items():
        for key, cache in [
            (f"releases/{TAG}/{original}", "public, max-age=31536000, immutable"),
            (f"latest/{alias}", "no-cache, no-store, must-revalidate"),
        ]:
            with urllib.request.urlopen(f"{base}/mosswork/release/{key}", timeout=5) as response:
                assert response.headers.get_content_type() == "application/octet-stream"
                assert response.headers.get_content_disposition() == "attachment"
                assert response.headers["Cache-Control"] == cache
                assert response.read() == payloads[original]


@pytest.mark.parametrize(
    "overrides",
    [{"Content-Type": "application/xml"}, {"Content-Disposition": None}],
    ids=["xml-instead-of-binary", "missing-attachment"],
)
def test_actual_publisher_refuses_wrong_headers_even_when_all_bytes_match(publisher, overrides):
    run, _base, storage, payloads = publisher
    result = run(overrides)
    assert result.returncode != 0
    assert "invalid download headers" in result.stderr
    for alias, original in CLIENTS.items():
        assert (storage / "mosswork/release/latest" / alias).read_bytes() == payloads[original]
