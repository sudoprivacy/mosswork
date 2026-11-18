"""Notarize and verify the actual company DMG and whole-application update.

The signature, ticket and mounted-image checks reuse the maintained SudoWork
macOS candidate workflow. Credentials remain in the signing operator's Keychain.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID


def run(*args, timeout=60, stderr=False):
    result = subprocess.run(args, check=True, capture_output=True, timeout=timeout, text=True)
    return result.stderr if stderr else result.stdout


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_receipt(path, value, *, exclusive=False):
    with path.open("x" if exclusive else "w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def notarize(artifact, profile, receipt, *, runner=run):
    """Record the submission before waiting; never resubmit after an uncertain wait."""
    if receipt.exists():
        raise ValueError("A notary submission already exists; inspect its ID before retrying")
    record = {
        "id": None,
        "artifact": artifact.name,
        "sha256": digest(artifact),
        "status": "submission-unconfirmed",
    }
    # A timeout while uploading can still leave a submission at Apple. Claim
    # the operation before starting it; an absent ID is not permission to retry.
    write_receipt(receipt, record, exclusive=True)
    try:
        submission = json.loads(
            runner(
                "xcrun",
                "notarytool",
                "submit",
                str(artifact),
                "--keychain-profile",
                profile,
                "--output-format",
                "json",
                timeout=600,
            )
        )
        submission_id = str(UUID(submission["id"]))
    except (subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError(
            "Notary submission is unconfirmed; inspect notarytool history before retrying"
        ) from None
    record.update(id=submission_id, status="submitted")
    write_receipt(receipt, record)
    try:
        outcome = json.loads(
            runner(
                "xcrun",
                "notarytool",
                "wait",
                submission_id,
                "--keychain-profile",
                profile,
                "--timeout",
                "20m",
                "--output-format",
                "json",
                timeout=1260,
            )
        )
    except (subprocess.SubprocessError, ValueError):
        raise RuntimeError(
            f"Notarization is unconfirmed; inspect submission {submission_id}"
        ) from None
    record["status"] = (
        outcome.get("status", "unknown") if outcome.get("id") == submission_id else "unconfirmed"
    )
    write_receipt(receipt, record)
    if outcome.get("id") != submission_id or outcome.get("status") != "Accepted":
        raise ValueError(f"Notarization was not accepted; inspect submission {submission_id}")
    return record


def signed_app(app, team, identifier, architectures, version, *, ticket=True, runner=run):
    """Reject ad-hoc/wrong-team signatures before contacting the notary service."""
    if not re.fullmatch(r"[A-Z0-9]{10}", team):
        raise ValueError("Expected a Developer ID team identifier")
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if (
        info.get("CFBundleIdentifier") != identifier
        or info.get("CFBundleShortVersionString") != version
        or info.get("CFBundleVersion") != version
    ):
        raise ValueError("Unexpected application identity or version")
    executable = info.get("CFBundleExecutable", "")
    if not executable or Path(executable).name != executable:
        raise ValueError("Invalid bundle executable")
    actual_arches = runner("lipo", "-archs", str(app / "Contents/MacOS" / executable)).split()
    if sorted(actual_arches) != sorted(architectures):
        raise ValueError("Unexpected application architecture")
    runner("codesign", "--verify", "--deep", "--strict", "--verbose=4", str(app))
    code_hashes = {}
    for architecture in architectures:
        signature = runner(
            "codesign", "--display", "--verbose=4", "--arch", architecture, str(app), stderr=True
        )
        if (
            not re.search(r"^Authority=Developer ID Application:", signature, re.M)
            or not re.search(r"^TeamIdentifier=" + re.escape(team) + r"$", signature, re.M)
            or not re.search(r"^Identifier=" + re.escape(identifier) + r"$", signature, re.M)
            or not re.search(r"^CodeDirectory .*flags=.*\bruntime\b", signature, re.M)
        ):
            raise ValueError(
                "Expected Developer ID, team, identifier and hardened runtime signature"
            )
        code_hash = re.search(r"^CDHash=([a-f0-9]+)$", signature, re.M)
        if not code_hash:
            raise ValueError("Missing application code directory hash")
        code_hashes[architecture] = code_hash[1]
    if ticket:
        runner("xcrun", "stapler", "validate", str(app))
        runner("spctl", "--assess", "--type", "execute", "--verbose=4", str(app))
    return {"cdhash": code_hashes, "version": version, "identifier": identifier}


def notarize_app(app, stage, profile, team, identifier, architectures, version, *, runner=run):
    signed_app(app, team, identifier, architectures, version, ticket=False, runner=runner)
    private = app / "Contents/Resources/private-bootstrap"
    if private.exists():
        signed_private_components(private, team, architectures, runner=runner)
    archive = stage / "notary-app.zip"
    runner("ditto", "-c", "-k", "--keepParent", str(app), str(archive))
    result = notarize(archive, profile, stage / "notary-app.json", runner=runner)
    runner("xcrun", "stapler", "staple", str(app))
    signed_app(app, team, identifier, architectures, version, runner=runner)
    return result


def signed_private_components(bundle, team, architectures, *, runner=run):
    """Inspect final pinned bytes; signing happens upstream of Stack assembly."""
    manifest = json.loads((bundle / "native-bundle.json").read_bytes())
    for name in ("scode", "daemon", "operator", "grpcurl"):
        artifact = manifest["artifacts"][name]
        relative = Path(artifact["file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Private component leaves its bundle")
        path = bundle / relative
        if path.is_symlink() or digest(path) != artifact["sha256"]:
            raise ValueError("Private component differs from its producer pin: " + name)
        if sorted(runner("lipo", "-archs", str(path)).split()) != sorted(architectures):
            raise ValueError("Private component architecture differs: " + name)
        runner("codesign", "--verify", "--strict", str(path))
        signature = runner("codesign", "--display", "--verbose=4", str(path), stderr=True)
        if (
            not re.search(r"^Authority=Developer ID Application:", signature, re.M)
            or not re.search(r"^TeamIdentifier=" + re.escape(team) + r"$", signature, re.M)
            or not re.search(r"^CodeDirectory .*flags=.*\bruntime\b", signature, re.M)
            or not re.search(r"^Timestamp=.+$", signature, re.M)
        ):
            raise ValueError(
                "Private components require Developer ID before Stack assembly: " + name
            )


@contextmanager
def mounted_dmg(image, mount, *, runner=run):
    mount.mkdir()
    runner(
        "hdiutil",
        "attach",
        "-readonly",
        "-nobrowse",
        "-noautoopen",
        "-mountpoint",
        str(mount),
        str(image),
    )
    try:
        yield mount
    finally:
        try:
            runner("hdiutil", "detach", str(mount))
        except subprocess.CalledProcessError as error:
            if error.returncode != 16:
                raise
            runner("hdiutil", "detach", "-force", str(mount))


def one_app(directory):
    apps = [p for p in directory.glob("*.app") if p.is_dir() and not p.is_symlink()]
    if len(apps) != 1:
        raise ValueError("Expected exactly one application in distribution")
    return apps[0]


def content_manifest(app):
    """Compare all payload bytes, including the compressed runtime, and symlink targets."""
    return {
        p.relative_to(app).as_posix(): (
            "link:" + str(p.readlink()) if p.is_symlink() else digest(p)
        )
        for p in app.rglob("*")
        if p.is_symlink() or p.is_file()
    }


def verify_distribution(dmg, update, team, identifier, architectures, version, *, runner=run):
    runner("codesign", "--verify", "--strict", str(dmg))
    runner("xcrun", "stapler", "validate", str(dmg))
    runner(
        "spctl",
        "--assess",
        "--type",
        "open",
        "--context",
        "context:primary-signature",
        "--verbose=4",
        str(dmg),
    )
    with tempfile.TemporaryDirectory(prefix="mosswork-signature-") as temporary:
        root = Path(temporary)
        with mounted_dmg(dmg, root / "mounted", runner=runner) as mount:
            app = one_app(mount)
            dmg_identity = signed_app(app, team, identifier, architectures, version, runner=runner)
            dmg_files = content_manifest(app)
        runner("ditto", "-x", "-k", str(update), str(root / "update"))
        app = one_app(root / "update")
        update_identity = signed_app(app, team, identifier, architectures, version, runner=runner)
        if dmg_identity != update_identity or dmg_files != content_manifest(app):
            raise ValueError(
                "DMG and update ZIP contain different applications or runtime payloads"
            )
    return {
        "team_identifier": team,
        "application": dmg_identity,
        "artifacts": {p.name: digest(p) for p in (dmg, update)},
        "checks": [
            "developer-id",
            "hardened-runtime",
            "deep-strict-signature",
            "stapled-ticket",
            "gatekeeper",
            "dmg-zip-payload-parity",
        ],
        "qualification": "distribution verified; installation and first launch still required",
    }
