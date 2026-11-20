"""Sign the native code shipped inside the company and bootstrap runtime archives."""

from __future__ import annotations

import json
import re
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from desktop.macos_distribution import digest, verify_developer_binary

# Thin Mach-O headers and both sizes of universal headers, in either byte order.
MACH_MAGICS = {
    bytes.fromhex(value)
    for value in (
        "feedface",
        "cefaedfe",
        "feedfacf",
        "cffaedfe",
        "cafebabe",
        "bebafeca",
        "cafebabf",
        "bfbafeca",
    )
}


def sign_runtime_tree(root: Path, identity: str, team: str) -> None:
    """The caller owns this staging tree. codesign signs all slices of a fat file."""
    count = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) not in MACH_MAGICS:
                continue
        # Static objects and Java classes can also have these leading bytes.
        description = subprocess.check_output(["file", "-b", str(path)], text=True)
        if not re.search(
            r"Mach-O (?:\d+-bit )?(?:executable|dynamically linked shared library|bundle)\b",
            description,
        ):
            continue
        previous = subprocess.run(["codesign", "--display", str(path)], capture_output=True)
        preserve = (
            ["--preserve-metadata=identifier,entitlements"] if previous.returncode == 0 else []
        )
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(mode | stat.S_IWUSR)
        try:
            subprocess.run(
                [
                    "codesign",
                    "--force",
                    "--timestamp",
                    "--options",
                    "runtime",
                    *preserve,
                    "--sign",
                    identity,
                    str(path),
                ],
                check=True,
            )
            verify_developer_binary(path, team)
        finally:
            path.chmod(mode)
        count += 1
    if not count:
        raise ValueError(f"No native runtime code in {root}")
    print(f"Signed and verified {count} native runtime files")


def sign_bootstrap_python(directory: Path, identity: str, team: str, *, native: bool) -> dict:
    """Transform a validated private staging copy before the Go shell pins its bytes."""
    archive = directory / "python-runtime.tar.gz"
    metadata = directory / "native-bundle.json"
    manifest = json.loads(metadata.read_bytes())
    original = digest(archive)
    if original != manifest["python"]["sha256"] or original != manifest["files"][archive.name]:
        raise ValueError("Bootstrap Python archive differs from its declared digest")
    with tempfile.TemporaryDirectory(dir=directory.parent) as temporary:
        root = Path(temporary)
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            for member in members:
                name = PurePosixPath(member.name)
                if name.parts[:1] != ("python",) or ".." in name.parts:
                    raise ValueError("Unexpected bootstrap Python archive path")
            source.extractall(root, members=members, filter="data")
        python = root / "python"
        if python.is_symlink() or not (python / "bin/python3").is_file():
            raise ValueError("Missing bootstrap Python interpreter")
        sign_runtime_tree(python, identity, team)
        if native:
            subprocess.run(
                [python / "bin/python3", "-I", "-B", "-c", "import ctypes, hashlib, sqlite3, ssl"],
                check=True,
                cwd=root,
            )
        signed = root / archive.name
        with tarfile.open(signed, "w:gz", compresslevel=6) as output:
            output.add(python, arcname="python")
        signed.replace(archive)
    manifest["python"].setdefault("sourceSha256", original)
    manifest["python"]["sha256"] = manifest["files"][archive.name] = digest(archive)
    metadata.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    pins = {**manifest["files"], metadata.name: digest(metadata)}
    checksums = directory / "SHA256SUMS"
    checksums.write_text(
        "".join(f"{pins[name]}  {name}\n" for name in sorted(pins)), encoding="utf-8"
    )
    return {"manifest_sha256": pins[metadata.name], "checksums_sha256": digest(checksums)}
