"""Materialize a checksum-pinned Stack release for the macOS packaging job."""

import argparse
import hashlib
import re
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def extract(archive: Path, expected: str, destination: Path) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("A SHA-256 pin from the release configuration is required")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError("Stack archive checksum differs from the release pin")
        stream.seek(0)
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            root = Path(temporary) / "bundle"
            root.mkdir()
            with tarfile.open(fileobj=stream, mode="r:gz") as package:
                seen = set()
                for member in package:
                    name = PurePosixPath(member.name)
                    if name == PurePosixPath(".") and member.isdir():
                        continue
                    if (
                        name.is_absolute()
                        or ".." in name.parts
                        or "\\" in member.name
                        or not (member.isdir() or member.isreg())
                        or name in seen
                    ):
                        raise ValueError("Unexpected Stack archive member")
                    seen.add(name)
                    package.extract(member, root, filter="data")
            if not (root / "native-bundle.json").is_file() or not (root / "SHA256SUMS").is_file():
                raise ValueError("Expected a Stack bundle at the root of the archive")
            root.rename(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("sha256")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    extract(args.archive, args.sha256, args.destination)
