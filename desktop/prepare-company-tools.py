#!/usr/bin/env python3
"""Install the pinned, standalone FFmpeg tools into a company runtime payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path


def install_zip(archive: Path, record: dict, payload: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        selected = []
        for name in record["files"]:
            matches = [item for item in members if Path(item.filename).name == name]
            if (
                len(matches) != 1
                or matches[0].is_dir()
                or stat.S_ISLNK(matches[0].external_attr >> 16)
            ):
                raise ValueError(f"Expected exactly one regular {name} in {record['url']}")
            selected.append((name, matches[0]))
        tools = payload / "tools"
        tools.mkdir(exist_ok=True)
        for name, item in selected:
            target = tools / name
            with bundle.open(item) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            target.chmod(0o755)
        notices = payload / "third-party/ffmpeg"
        notices.mkdir(parents=True, exist_ok=True)
        for item in members:
            name = Path(item.filename).name
            if (
                name.lower().startswith(("license", "copying", "readme"))
                and not item.is_dir()
                and not stat.S_ISLNK(item.external_attr >> 16)
            ):
                with bundle.open(item) as source:
                    (notices / name).write_bytes(source.read())


def install_archive(archive: Path, record: dict, payload: Path) -> None:
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != record["sha256"]:
        raise ValueError(f"FFmpeg checksum mismatch: {record['url']}")
    if zipfile.is_zipfile(archive):
        install_zip(archive, record, payload)
        return
    tools = payload / "tools"
    tools.mkdir(exist_ok=True)
    with tarfile.open(archive) as bundle:
        for name in record["files"]:
            matches = [
                item
                for item in bundle.getmembers()
                if Path(item.name).name == name and item.isfile()
            ]
            if len(matches) != 1:
                raise ValueError(f"Expected exactly one {name} in {record['url']}")
            target = tools / name
            with bundle.extractfile(matches[0]) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            target.chmod(0o755)
        notices = payload / "third-party/ffmpeg"
        notices.mkdir(parents=True, exist_ok=True)
        for item in bundle.getmembers():
            name = Path(item.name).name
            if name.lower().startswith(("license", "copying", "readme")) and item.isfile():
                with bundle.extractfile(item) as source:
                    (notices / name).write_bytes(source.read())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=("darwin-amd64", "darwin-arm64", "windows-amd64"))
    parser.add_argument("payload", type=Path)
    args = parser.parse_args()
    records = json.loads(Path(__file__).with_name("company-tools.json").read_text())[args.platform]
    with tempfile.TemporaryDirectory(prefix="mosswork-ffmpeg-") as temp:
        for index, record in enumerate(records):
            archive = Path(temp) / f"{index}.tar.gz"
            with (
                urllib.request.urlopen(
                    urllib.request.Request(
                        record["url"], headers={"User-Agent": "mosswork-runtime-builder"}
                    ),
                    timeout=120,
                ) as response,
                archive.open("wb") as target,
            ):
                shutil.copyfileobj(response, target)
            install_archive(archive, record, args.payload)
    notices = args.payload / "third-party/ffmpeg"
    (notices / "downloads.json").write_text(json.dumps(records, indent=2) + "\n")
    shutil.copy2(Path(__file__).with_name("ffmpeg-NOTICE.md"), notices / "NOTICE.md")
    shutil.copy2(Path(__file__).with_name("FFmpeg-GPL-3.0.txt"), notices / "GPL-3.0.txt")


if __name__ == "__main__":
    main()
