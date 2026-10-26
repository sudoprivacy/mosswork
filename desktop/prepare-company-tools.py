#!/usr/bin/env python3
"""Install the pinned, standalone FFmpeg tools into a company runtime payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def install_archive(archive: Path, record: dict, payload: Path) -> None:
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != record["sha256"]:
        raise ValueError(f"FFmpeg checksum mismatch: {record['url']}")
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
    parser.add_argument("platform", choices=("darwin-arm64", "windows-amd64"))
    parser.add_argument("payload", type=Path)
    args = parser.parse_args()
    records = json.loads(Path(__file__).with_name("company-tools.json").read_text())[args.platform]
    with tempfile.TemporaryDirectory(prefix="mosswork-ffmpeg-") as temp:
        for index, record in enumerate(records):
            archive = Path(temp) / f"{index}.tar.gz"
            with (
                urllib.request.urlopen(record["url"], timeout=120) as response,
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
