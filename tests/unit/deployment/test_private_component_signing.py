"""Do not submit a signed shell that still carries ad-hoc Private components."""

import hashlib
import json

import pytest
from desktop import macos_distribution as distribution


def private_bundle(tmp_path):
    root = tmp_path / "mosswork.app/Contents/Resources/private-bootstrap"
    root.mkdir(parents=True)
    artifacts = {}
    for name in ("scode", "daemon", "operator", "grpcurl"):
        (root / name).write_bytes(name.encode())
        artifacts[name] = {"file": name, "sha256": hashlib.sha256(name.encode()).hexdigest()}
    (root / "native-bundle.json").write_text(json.dumps({"artifacts": artifacts}))
    return root


@pytest.mark.parametrize("invalid", ["ad-hoc", "wrong-team", "no-runtime", "no-timestamp"])
def test_refuses_components_before_archive_or_notary(tmp_path, monkeypatch, invalid):
    root = private_bundle(tmp_path)
    monkeypatch.setattr(distribution, "signed_app", lambda *args, **kwargs: {})
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        assert args[0] not in {"ditto", "xcrun"}
        if args[0] == "lipo":
            return "arm64"
        if args[1] == "--verify":
            return ""
        signature = (
            "Authority=Developer ID Application: Example\nTeamIdentifier=ABCDEFGHIJ\n"
            "CodeDirectory v=20500 flags=0x10000(runtime)\nTimestamp=Nov 17 2026\n"
        )
        return {
            "ad-hoc": "Signature=adhoc\nTeamIdentifier=not set\n",
            "wrong-team": signature.replace("ABCDEFGHIJ", "KLMNOPQRST"),
            "no-runtime": signature.replace("runtime", "adhoc"),
            "no-timestamp": signature.replace("Timestamp=Nov 17 2026\n", ""),
        }[invalid]

    with pytest.raises(ValueError, match="before Stack assembly"):
        distribution.notarize_app(
            root.parents[2],
            tmp_path,
            "unused-profile",
            "ABCDEFGHIJ",
            "com.example.mosswork",
            ["arm64"],
            "0.0.7",
            runner=run,
        )
    assert calls
    assert not (tmp_path / "notary-app.zip").exists()
    assert not (tmp_path / "notary-app.json").exists()


def test_signing_checks_preserve_pinned_bytes(tmp_path):
    root = private_bundle(tmp_path)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    seen = []

    def run(*args, **kwargs):
        seen.append(args)
        if args[0] == "lipo":
            return "arm64"
        return (
            "Authority=Developer ID Application: Example\nTeamIdentifier=ABCDEFGHIJ\n"
            "CodeDirectory v=20500 flags=0x10000(runtime)\nTimestamp=Nov 17 2026\n"
        )

    distribution.signed_private_components(root, "ABCDEFGHIJ", ["arm64"], runner=run)
    assert len(seen) == 12
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}
