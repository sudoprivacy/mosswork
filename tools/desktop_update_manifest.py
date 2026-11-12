"""Build and validate the native stable update feed from actual release payloads."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

FEED = "desktop-update.json"
TARGETS = Path(__file__).resolve().parents[1] / "desktop/src/internal/desktopupdate/targets.json"
STABLE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")


def manifest(directory: Path, tag: str) -> dict[str, Any]:
    """Derive URLs, sizes and integrity values from one complete stable release."""
    version = tag.removeprefix("v")
    if tag != f"v{version}" or not STABLE.fullmatch(version):
        raise ValueError("The stable desktop feed requires a vX.Y.Z release tag")
    artifacts = []
    for target in json.loads(TARGETS.read_text()):
        platform, arch = target["platform"], target["arch"]
        name, extension = target["artifact_platform"], target["extension"]
        filename = f"mosswork-company-{name}-{arch}-{version}-update.{extension}"
        source = directory / filename
        size = source.stat().st_size
        if not 0 < size <= 2 << 30:
            raise ValueError(f"Invalid update artifact size: {filename}")
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").digest()
        artifacts.append(
            {
                "platform": platform,
                "arch": arch,
                "filename": filename,
                "url": f"../releases/{tag}/{filename}",
                "size": size,
                "digestAlgo": "sha256",
                "digest": base64.b64encode(digest).decode("ascii"),
            }
        )
    return {
        "schemaVersion": 1,
        "channel": "stable",
        "version": version,
        "metadata": {"distribution": "mosswork-company"},
        "artifacts": artifacts,
    }


def verify(directory: Path, tag: str) -> None:
    """Refuse feeds whose advertised bytes or paths differ from staged assets."""
    expected = manifest(directory, tag)
    if json.loads((directory / FEED).read_text()) != expected:
        raise ValueError("Desktop update feed differs from the actual complete release payloads")


def can_promote(url: str, tag: str) -> bool:
    """Keep a recovered older publisher from rolling the native feed backward."""
    version = tag.removeprefix("v")
    if not STABLE.fullmatch(version):
        raise ValueError("Invalid stable promotion version")
    try:
        request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(64 * 1024 + 1)
        if len(data) > 64 * 1024:
            raise ValueError("Existing desktop feed exceeds its size limit")
        current = json.loads(data)["version"]
        if not isinstance(current, str) or not STABLE.fullmatch(current):
            raise ValueError("Invalid existing stable desktop feed")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return True
        raise
    return tuple(map(int, version.split("."))) >= tuple(map(int, current.split(".")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--can-promote")
    args = parser.parse_args()
    if args.can_promote:
        raise SystemExit(0 if can_promote(args.can_promote, args.tag) else 10)
    if args.verify:
        verify(args.directory, args.tag)
        return
    if not STABLE.fullmatch(args.tag.removeprefix("v")):
        # Prerelease installers are immutable downloads; never advertise them
        # to stable clients or make their version strings look like semver.
        print("Prerelease: no stable desktop update feed is generated.")
        return
    data = manifest(args.directory, args.tag)
    (args.directory / FEED).write_text(json.dumps(data, indent=2) + "\n")


if __name__ == "__main__":
    main()
