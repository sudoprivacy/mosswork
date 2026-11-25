"""Exercise distribution rejection and uncertain notary outcomes without Apple credentials."""

from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest
from desktop import macos_distribution as macos

TEAM = "ABCDEFGHIJ"
IDENTIFIER = "io.github.sudoprivacy.mosswork"
SUBMISSION = "21a76b7e-16f2-41a0-b0fd-05c6af91b78f"


def app_fixture(root):
    app = root / "mosswork.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": IDENTIFIER,
                "CFBundleShortVersionString": "0.0.7",
                "CFBundleVersion": "0.0.7",
                "CFBundleExecutable": "mosswork",
            }
        )
    )
    (app / "Contents/MacOS/mosswork").write_bytes(b"fixture native code")
    (app / "Contents/Resources").mkdir()
    (app / "Contents/Resources/runtime.zip").write_bytes(b"exact pinned runtime")
    return app


def signature_runner(*args, **kwargs):
    if args[0] == "lipo":
        return "arm64\n"
    if args[:2] == ("codesign", "--display"):
        return (
            f"Authority=Developer ID Application: Fixture\nTeamIdentifier={TEAM}\n"
            f"Identifier={IDENTIFIER}\nCodeDirectory flags=0x10000(runtime)\nCDHash=aabbccdd\n"
        )
    return ""


@pytest.mark.parametrize(
    "replacement",
    [
        ("Authority=Developer ID Application: Fixture", "Signature=adhoc"),
        (TEAM, "WRONGTEAM1"),
        (IDENTIFIER, "other.application"),
        ("flags=0x10000(runtime)", "flags=0x0(none)"),
        ("CDHash=aabbccdd", ""),
    ],
)
def test_invalid_signature_is_refused_before_notary(tmp_path, replacement):
    app = app_fixture(tmp_path)
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        return signature_runner(*args, **kwargs).replace(*replacement)

    with pytest.raises(ValueError):
        macos.notarize_app(
            app, tmp_path, "profile", TEAM, IDENTIFIER, ["arm64"], "0.0.7", runner=runner
        )
    assert not any("notarytool" in args for args in calls)


@pytest.mark.parametrize("failed_tool", ["stapler", "spctl"])
def test_missing_ticket_or_gatekeeper_denial_never_qualifies(tmp_path, failed_tool):
    app = app_fixture(tmp_path)

    def runner(*args, **kwargs):
        if failed_tool in args:
            raise subprocess.CalledProcessError(1, args)
        return signature_runner(*args, **kwargs)

    with pytest.raises(subprocess.CalledProcessError):
        macos.signed_app(app, TEAM, IDENTIFIER, ["arm64"], "0.0.7", runner=runner)


@pytest.mark.parametrize("outcome", ["Invalid", "In Progress", "timeout", "wrong-id"])
def test_rejected_or_uncertain_submission_retains_id_and_cannot_resubmit(
    tmp_path, monkeypatch, outcome
):
    artifact = tmp_path / "application.zip"
    artifact.write_bytes(b"signed application")
    receipt = tmp_path / "notary.json"
    calls = []
    elapsed = 0

    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(macos, "monotonic", lambda: elapsed)
    monkeypatch.setattr(macos, "sleep", sleep)

    def runner(*args, **kwargs):
        calls.append(args)
        if args[2] == "submit":
            return json.dumps({"id": SUBMISSION})
        assert args[:4] == ("xcrun", "notarytool", "info", SUBMISSION)
        assert json.loads(receipt.read_text())["id"] == SUBMISSION
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(args, 1)
        return json.dumps(
            {
                "id": "another" if outcome == "wrong-id" else SUBMISSION,
                "status": "Accepted" if outcome == "wrong-id" else outcome,
            }
        )

    with pytest.raises((ValueError, RuntimeError)):
        macos.notarize(artifact, "existing-profile", receipt, runner=runner)
    assert json.loads(receipt.read_text())["id"] == SUBMISSION
    if outcome == "wrong-id":
        assert json.loads(receipt.read_text())["status"] == "unconfirmed"
    with pytest.raises(ValueError, match="already exists"):
        macos.notarize(artifact, "existing-profile", receipt, runner=runner)
    assert sum(args[2] == "submit" for args in calls) == 1


def test_transient_notary_status_failure_keeps_polling_original_id(tmp_path, monkeypatch):
    artifact = tmp_path / "client.dmg"
    artifact.write_bytes(b"signed image")
    receipt = tmp_path / "notary.json"
    responses = iter(["In Progress", subprocess.TimeoutExpired("notarytool", 30), "Accepted"])
    commands = []
    monkeypatch.setattr(macos, "sleep", lambda _seconds: None)

    def runner(*args, **kwargs):
        commands.append(args[2])
        if args[2] == "submit":
            return json.dumps({"id": SUBMISSION})
        assert args[:4] == ("xcrun", "notarytool", "info", SUBMISSION)
        assert json.loads(receipt.read_text())["id"] == SUBMISSION
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return json.dumps({"id": SUBMISSION, "status": response})

    assert macos.notarize(artifact, "profile", receipt, runner=runner)["status"] == "Accepted"
    assert commands == ["submit", "info", "info", "info"]


@pytest.mark.parametrize("failure", ["timeout", "malformed", "missing-id"])
def test_uncertain_upload_is_recorded_before_network_and_refuses_repeat(tmp_path, failure):
    artifact = tmp_path / "application.zip"
    artifact.write_bytes(b"signed application")
    receipt = tmp_path / "notary.json"
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        assert args[:3] == ("xcrun", "notarytool", "submit")
        recorded = json.loads(receipt.read_text())
        assert recorded["status"] == "submission-unconfirmed"
        assert recorded["sha256"] == macos.digest(artifact)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 1)
        return "not-json" if failure == "malformed" else "{}"

    with pytest.raises(RuntimeError, match="history before retrying"):
        macos.notarize(artifact, "profile", receipt, runner=runner)
    assert json.loads(receipt.read_text())["id"] is None
    with pytest.raises(ValueError, match="already exists"):
        macos.notarize(artifact, "profile", receipt, runner=runner)
    assert len(calls) == 1


def test_app_is_stapled_and_assessed_only_after_acceptance(tmp_path):
    app = app_fixture(tmp_path)
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("ditto", "-c"):
            Path(args[-1]).write_bytes(b"archive")
        if "notarytool" in args:
            return json.dumps({"id": SUBMISSION, "status": "Accepted"})
        return signature_runner(*args, **kwargs)

    macos.notarize_app(
        app, tmp_path, "profile", TEAM, IDENTIFIER, ["arm64"], "0.0.7", runner=runner
    )
    check = next(i for i, args in enumerate(calls) if args[:3] == ("xcrun", "notarytool", "info"))
    staple = next(i for i, args in enumerate(calls) if args[:3] == ("xcrun", "stapler", "staple"))
    assess = next(i for i, args in enumerate(calls) if args[0] == "spctl")
    assert check < staple < assess


@pytest.mark.parametrize("replace_runtime", [False, True])
def test_compare_actual_dmg_and_update_payloads_and_always_detach(tmp_path, replace_runtime):
    template = app_fixture(tmp_path / "template")
    dmg, update = tmp_path / "installer.dmg", tmp_path / "update.zip"
    dmg.write_bytes(b"image")
    update.write_bytes(b"update")
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("hdiutil", "attach"):
            target = Path(args[args.index("-mountpoint") + 1]) / template.name
            shutil.copytree(template, target)
        if args[:2] == ("ditto", "-x"):
            target = Path(args[-1]) / template.name
            shutil.copytree(template, target)
            if replace_runtime:
                (target / "Contents/Resources/runtime.zip").write_bytes(b"stale runtime")
        return signature_runner(*args, **kwargs)

    if replace_runtime:
        with pytest.raises(ValueError, match="different applications or runtime"):
            macos.verify_distribution(
                dmg, update, TEAM, IDENTIFIER, ["arm64"], "0.0.7", runner=runner
            )
    else:
        result = macos.verify_distribution(
            dmg, update, TEAM, IDENTIFIER, ["arm64"], "0.0.7", runner=runner
        )
        assert result["artifacts"][dmg.name] == macos.digest(dmg)
        assert "first launch still required" in result["qualification"]
    assert sum(args[:2] == ("hdiutil", "detach") for args in calls) == 1


def test_failed_mounted_app_check_still_detaches(tmp_path):
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        return ""

    with (
        pytest.raises(ValueError, match="exactly one application"),
        macos.mounted_dmg(tmp_path / "installer.dmg", tmp_path / "mount", runner=runner) as mount,
    ):
        macos.one_app(mount)
    assert calls[-1][:2] == ("hdiutil", "detach")
