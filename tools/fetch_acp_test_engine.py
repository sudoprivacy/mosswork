"""Fetch a verified native engine for tests; never install a product runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path


def fetch_engine(output: Path) -> Path:
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "tests/support/acp_runtime_engine.json").read_text(
            encoding="utf-8"
        )
    )
    machine = platform.machine().lower()
    architecture = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine, machine
    )
    asset = manifest["assets"][f"{sys.platform}-{architecture}"]
    url = f"https://github.com/{manifest['repository']}/releases/download/v{manifest['version']}/{asset['name']}"
    output.mkdir(parents=True, exist_ok=False)
    archive = output / asset["name"]
    request = urllib.request.Request(url, headers={"User-Agent": "MossWork-ACP-contract-test"})
    with urllib.request.urlopen(request, timeout=60) as response, archive.open("xb") as destination:
        shutil.copyfileobj(response, destination)
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != asset["sha256"]:
        raise ValueError(
            f"Release archive digest differs from the pinned test manifest: {asset['name']}"
        )
    binary_name = "scode.exe" if sys.platform == "win32" else "scode"
    binary = output / binary_name
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            matches = [
                item
                for item in bundle.infolist()
                if not item.is_dir() and Path(item.filename).name == binary_name
            ]
            if len(matches) != 1:
                raise ValueError("Expected exactly one native test engine")
            with bundle.open(matches[0]) as source, binary.open("xb") as destination:
                shutil.copyfileobj(source, destination)
    else:
        with tarfile.open(archive) as bundle:
            members = [
                item
                for item in bundle.getmembers()
                if item.isfile() and Path(item.name).name == binary_name
            ]
            if len(members) != 1:
                raise ValueError("Expected exactly one native test engine")
            source = bundle.extractfile(members[0])
            if source is None:
                raise ValueError("Native test engine is not a regular archive member")
            with source, binary.open("xb") as destination:
                shutil.copyfileobj(source, destination)
        binary.chmod(0o755)
    (output / "verified-download.json").write_text(
        json.dumps({"url": url, "archive_sha256": digest, "version": manifest["version"]}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return binary.resolve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    print(fetch_engine(parser.parse_args().output))
