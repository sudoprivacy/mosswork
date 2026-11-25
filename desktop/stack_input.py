"""Read and verify the reviewed pin for the Stack bootstrap input of the signed Mac client.

The pin in desktop/stack-input.json names one asset on a draft release of this
repository. Changing it requires a reviewed pull request; workflows only read it.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

DEFAULT_PIN = Path(__file__).with_name("stack-input.json")
HEX64 = re.compile(r"[a-f0-9]{64}")
COMMIT = re.compile(r"[a-f0-9]{40}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
FIELDS = {
    "platform",
    "release_tag",
    "asset_id",
    "asset_name",
    "size",
    "sha256",
    "manifest_sha256",
    "qualification",
    "sources",
}


def load(path: Path = DEFAULT_PIN) -> dict:
    pin = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(pin, dict) or set(pin) != FIELDS:
        raise ValueError(f"Stack input pin must contain exactly: {', '.join(sorted(FIELDS))}")
    if pin["platform"] != "macos-arm64":
        raise ValueError("Stack input pin must target macos-arm64")
    if not isinstance(pin["release_tag"], str) or not NAME.fullmatch(pin["release_tag"]):
        raise ValueError("Invalid release tag in Stack input pin")
    if not isinstance(pin["asset_name"], str) or not NAME.fullmatch(pin["asset_name"]):
        raise ValueError("Invalid asset name in Stack input pin")
    if not pin["asset_name"].endswith(".tar.gz"):
        raise ValueError("Stack input asset must be a .tar.gz archive")
    for key in ("asset_id", "size"):
        if type(pin[key]) is not int or pin[key] <= 0:
            raise ValueError(f"Invalid {key} in Stack input pin")
    for key in ("sha256", "manifest_sha256"):
        if not isinstance(pin[key], str) or not HEX64.fullmatch(pin[key]):
            raise ValueError(f"Invalid {key} in Stack input pin")
    if not isinstance(pin["qualification"], str) or not pin["qualification"]:
        raise ValueError("Invalid qualification in Stack input pin")
    sources = pin["sources"]
    if (
        not isinstance(sources, dict)
        or not sources
        or not all(isinstance(value, str) and COMMIT.fullmatch(value) for value in sources.values())
    ):
        raise ValueError("Stack input sources must map names to full commit hashes")
    return pin


def verify_archive(pin: dict, archive: Path, metadata: dict | None = None) -> None:
    if archive.stat().st_size != pin["size"]:
        raise ValueError("Stack input size differs from the reviewed pin")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != pin["sha256"]:
            raise ValueError("Stack input checksum differs from the reviewed pin")
    if metadata is not None:
        expected = {
            "id": pin["asset_id"],
            "name": pin["asset_name"],
            "size": pin["size"],
            "state": "uploaded",
            "digest": "sha256:" + pin["sha256"],
        }
        for key, value in expected.items():
            if metadata.get(key) != value:
                raise ValueError(f"Release asset {key} differs from the reviewed pin")


def verify_bundle(pin: dict, bundle: Path) -> dict:
    manifest_bytes = (bundle / "native-bundle.json").read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != pin["manifest_sha256"]:
        raise ValueError("Stack bundle manifest differs from the reviewed pin")
    manifest = json.loads(manifest_bytes)
    for key in ("platform", "qualification", "sources"):
        if manifest.get(key) != pin[key]:
            raise ValueError(f"Stack bundle {key} differs from the reviewed pin")
    return manifest


def summary(pin: dict, base: dict | None = None, manifest: dict | None = None) -> str:
    rows = ["| Field | Current main | This change |", "|---|---|---|"]
    for key in sorted(FIELDS - {"sources"}):
        old = "" if base is None else str(base.get(key, ""))
        new = str(pin[key])
        rows.append(f"| {key}{' **(changed)**' if old != new else ''} | `{old}` | `{new}` |")
    old_sources = {} if base is None else base.get("sources", {})
    for name in sorted(set(old_sources) | set(pin["sources"])):
        old, new = old_sources.get(name, ""), pin["sources"].get(name, "")
        rows.append(
            f"| sources.{name}{' **(changed)**' if old != new else ''} | `{old}` | `{new}` |"
        )
    text = ["## Stack input pin for review", "", *rows]
    if manifest is not None:
        text += ["", "### Components recorded in the package manifest", ""]
        for name, artifact in sorted(manifest.get("artifacts", {}).items()):
            text.append(f"- `{name}`: {artifact.get('source', 'source not recorded')}")
        python = manifest.get("python", {})
        if python:
            text.append(
                f"- `python`: {python.get('url', '')} (sha256 `{python.get('sha256', '')}`)"
            )
    return "\n".join(text) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", type=Path, default=DEFAULT_PIN)
    commands = parser.add_subparsers(dest="command", required=True)
    field = commands.add_parser("field", help="print one pin value")
    field.add_argument("key", choices=sorted(FIELDS - {"sources"}))
    verify = commands.add_parser("verify", help="check a downloaded archive against the pin")
    verify.add_argument("archive", type=Path)
    verify.add_argument("--metadata", type=Path, help="GitHub release asset JSON")
    verify.add_argument("--bundle", type=Path, help="extracted bundle directory")
    report = commands.add_parser("summary", help="write a Markdown review summary")
    report.add_argument("--base", type=Path, help="pin from the target branch")
    report.add_argument("--bundle", type=Path, help="extracted bundle directory")
    args = parser.parse_args(argv)

    pin = load(args.pin)
    if args.command == "field":
        print(pin[args.key])
    elif args.command == "verify":
        metadata = json.loads(args.metadata.read_text(encoding="utf-8")) if args.metadata else None
        verify_archive(pin, args.archive, metadata)
        if args.bundle:
            verify_bundle(pin, args.bundle)
        print(f"Stack input matches the reviewed pin: {pin['asset_name']} sha256 {pin['sha256']}")
    else:
        base = load(args.base) if args.base and args.base.is_file() else None
        manifest = verify_bundle(pin, args.bundle) if args.bundle else None
        sys.stdout.write(summary(pin, base, manifest))


if __name__ == "__main__":
    main()
