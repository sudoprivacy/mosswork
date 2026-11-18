"""Targeted offline packaging contract tests; no installers or downloads."""

import hashlib
import importlib.util
import json
import plistlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

_spec = importlib.util.spec_from_file_location(
    "package_company", Path(__file__).resolve().parents[1] / "package-company.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


class CompanyPackageTest(unittest.TestCase):
    def test_uninstall_preserves_employee_workspace_files(self):
        script = (
            Path(__file__).resolve().parents[1] / "src/build/windows/nsis/project.nsi"
        ).read_text()
        uninstall = script.split('Section "uninstall"', 1)[1]
        self.assertNotRegex(uninstall, r'(?i)RMDir\s+/r\s+"?\$INSTDIR')
        self.assertIn('Delete "$INSTDIR\\${PRODUCT_EXECUTABLE}"', uninstall)
        self.assertIn('RMDir "$INSTDIR"', uninstall)

    def setUp(self):
        self.release = {
            "mosswork_version": "1.0.0",
            "source_commit": "fixture",
            "source_dirty": False,
            "device_protocol_version": 2,
            "company_protocol_version": 1,
            "python_source_sha256": "",
        }
        mock = patch.object(module, "release_identity", return_value=self.release)
        mock.start()
        self.addCleanup(mock.stop)

    def payload(self, root: Path) -> Path:
        source = root / "darwin-arm64"
        names = [
            "launch.py",
            "runtime/bin/python3",
            "packages/octop/company_runtime.py",
            "packages/moss_harness/__init__.py",
            "tools/ffmpeg",
            "tools/ffprobe",
        ]
        names += [f"packages/{m}/__init__.py" for m in module.RUNTIME_MODULES]
        for component in module.LOCAL_COMPONENTS:
            name = component.replace("-", "_")
            names += [f"packages/{name}/__init__.py", f"packages/{name}-1.0.0.dist-info/METADATA"]
        names = list(dict.fromkeys(names))
        for name in names:
            item = source / name
            item.parent.mkdir(parents=True, exist_ok=True)
            if name in ("runtime/bin/python3", "tools/ffmpeg", "tools/ffprobe"):
                item.write_bytes(
                    b"\xcf\xfa\xed\xfe" + (0x100000C).to_bytes(4, "little") + b"\0" * 56
                )
            elif name.endswith("METADATA"):
                item.write_text("Version: 1.0.0\n")
            else:
                item.write_text("company-runtime")
        app_metadata = source / "packages/mosswork-1.0.0.dist-info/METADATA"
        app_metadata.parent.mkdir()
        app_metadata.write_text("Version: 1.0.0\n")
        names.append(app_metadata.relative_to(source).as_posix())
        self.release["python_source_sha256"] = module.python_source_digest(
            source / "packages/octop"
        )
        (source / "company-runtime-build.json").write_text(
            json.dumps(
                {
                    "platform": "darwin",
                    "arch": "arm64",
                    "protocol_version": 1,
                    "harness_version": "1.0.0",
                    "component_versions": dict.fromkeys(module.LOCAL_COMPONENTS, "1.0.0"),
                    "mosswork_version": "1.0.0",
                    "smoke_test_passed": True,
                    "file_hashes": {
                        name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                        for name in names
                    },
                }
            )
        )
        return source

    def test_manifest_freezes_all_tools(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.payload(root)
            archive = module.package_runtime(source, root / "payload.zip", "darwin", "arm64")
            with zipfile.ZipFile(archive) as bundle:
                manifest = json.loads(
                    bundle.read("mosswork-company-darwin-arm64/company-runtime-manifest.json")
                )
                self.assertEqual(manifest["arch"], "arm64")
                self.assertEqual(manifest["mosswork_version"], "1.0.0")
                self.assertEqual(manifest["device_protocol_version"], 2)
                for name, digest in manifest["files"].items():
                    self.assertEqual(
                        hashlib.sha256(
                            bundle.read("mosswork-company-darwin-arm64/" + name)
                        ).hexdigest(),
                        digest,
                    )

    def test_missing_tools_and_wrong_arch_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.payload(root)
            with self.assertRaisesRegex(ValueError, "different platform"):
                module.package_runtime(source, root / "bad.zip", "darwin", "amd64")
            (source / "tools/ffmpeg").unlink()
            with self.assertRaisesRegex(ValueError, "Missing offline runtime dependency"):
                module.package_runtime(source, root / "bad.zip", "darwin", "arm64")

    def test_changes_after_smoke_test_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.payload(root)
            (source / "packages/octop/company_runtime.py").write_text("modified source")
            with self.assertRaisesRegex(ValueError, "differs from"):
                module.package_runtime(source, root / "bad.zip", "darwin", "arm64")

    def test_unverified_payload_is_marked_unverified(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.payload(root)
            (source / "company-runtime-build.json").unlink()
            archive = module.package_runtime(source, root / "untested.zip", "darwin", "arm64")
            with zipfile.ZipFile(archive) as bundle:
                manifest = json.loads(
                    bundle.read("mosswork-company-darwin-arm64/company-runtime-manifest.json")
                )
                self.assertFalse(manifest["target_smoke_verified"])

    def test_old_application_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.payload(root)
            next((source / "packages").glob("mosswork-*.dist-info/METADATA")).write_text(
                "Version: 0.0.2\n"
            )
            with self.assertRaisesRegex(ValueError, "version must match"):
                module.package_runtime(source, root / "bad.zip", "darwin", "arm64")


@pytest.mark.parametrize(
    "platform,arch,extension,private,changed_after_compile",
    [
        ("darwin", "arm64", ".dmg", False, False),
        ("windows", "amd64", ".exe", False, False),
        ("darwin", "arm64", ".dmg", True, False),
        ("darwin", "arm64", ".dmg", True, True),
    ],
)
def test_single_arch_build_needs_only_its_own_payload(
    tmp_path, monkeypatch, platform, arch, extension, private, changed_after_compile
):
    root = tmp_path / "repo"
    source = root / "desktop/src"
    (source / "build/darwin").mkdir(parents=True)
    (source / "build/windows/nsis").mkdir(parents=True)
    (source / "assets").mkdir()
    (source / "assets/company-dashboard.zip").write_bytes(b"dashboard")
    (source / "build/darwin/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleExecutable": "mosswork",
                "CFBundleIdentifier": "io.github.sudoprivacy.mosswork",
            }
        )
    )
    (root / "pyproject.toml").write_text('[project]\nversion="1.0.1b2"\n')
    webview = tmp_path / "webview"
    webview.mkdir()
    with (webview / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe").open("wb") as stream:
        stream.truncate(50_000_001)
    output = tmp_path / "release"
    commands, payloads, links, private_copies = [], [], [], []

    def run(*args, cwd, env=None, capture_output=False):
        commands.append((args, env))
        if len(args) > 1 and args[1] == "--verify-private-payload":
            assert private and capture_output
            return json.dumps(
                {
                    "client_version": "1.0.1b2",
                    "platform": platform,
                    "arch": arch,
                    "payload_verified": True,
                }
            )
        if len(args) > 1 and args[1] == "--verify-update-payload":
            assert capture_output
            assert str(tmp_path / "build-company/native-verification") in env["MOSSWORK_HOME"]
            return json.dumps(
                {
                    "client_version": "1.0.1b2",
                    "runtime_version": "1.0.1b2",
                    "platform": platform,
                    "arch": arch,
                }
            )
        if args[0] == "go":
            if private:
                assert len(private_copies) == 1
                assert f"-X main.privateManifestSHA256={'1' * 64}" in str(args)
                assert f"-X main.privateChecksumsSHA256={'2' * 64}" in str(args)
            Path(args[args.index("-o") + 1]).write_bytes(b"shell")
        elif args[:3] == ("wails3", "generate", "icons"):
            Path(args[args.index("-macfilename") + 1]).write_bytes(b"icon")
        elif args[:2] == ("hdiutil", "create"):
            Path(args[-1]).write_bytes(b"dmg")
        elif args[0] == "makensis":
            name = next(
                str(a).split("=", 1)[1]
                for a in args
                if str(a).lstrip("-/").startswith("DINSTALLER_OUTFILE=")
            )
            Path(name).write_bytes(b"exe")
        elif args[0] == "ditto":
            Path(args[-1]).write_bytes(b"whole signed app archive")

    def payload(source, destination, target_platform, target_arch):
        payloads.append((target_platform, target_arch))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"verified runtime")
        return destination

    monkeypatch.setattr(module, "__file__", str(root / "desktop/package-company.py"))
    monkeypatch.setattr(module, "release_identity", lambda: {"source_commit": "fixture"})
    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(module, "host_target", lambda: (platform, arch))
    monkeypatch.setattr(module, "package_runtime", payload)
    # This is a packaging orchestration test: platform tools are already mocked.
    # Record the Finder shortcut as well, so the same contract runs on Windows.
    monkeypatch.setattr(module.os, "symlink", lambda target, link: links.append((target, link)))
    if private:

        def copy_private_bundle(*args):
            private_copies.append(args)
            return {
                "source": "fixture",
                "checksums_sha256": "2" * 64,
                "manifest_sha256": (
                    "3" if changed_after_compile and len(private_copies) > 1 else "1"
                )
                * 64,
            }

        monkeypatch.setattr(module, "copy_private_bundle", copy_private_bundle)
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "package-company.py",
            "--platform",
            platform,
            "--arch",
            arch,
            "--runtime-dir",
            str(tmp_path / "payloads"),
            *(["--bootstrap-bundle", str(tmp_path / "bootstrap")] if private else []),
            *(["--company-url", "https://accounts.example.test/"] if private else []),
            "--webview2-dir",
            str(webview),
            "--output",
            str(output),
        ],
    )
    if changed_after_compile:
        with pytest.raises(ValueError, match="changed after desktop compilation"):
            module.main()
        assert len(private_copies) == 2
        assert not any(
            args[0] == "codesign" or "--verify-private-payload" in args for args, _ in commands
        )
        return
    module.main()
    assert payloads == [(platform, arch)]
    assert [env["GOARCH"] for args, env in commands if args[0] == "go"] == [arch]
    assert not any(args[0] == "lipo" for args, _ in commands)
    installers = [p for p in output.glob("*" + extension) if not p.name.endswith("-update.exe")]
    assert len(installers) == 1
    assert arch in installers[0].name
    if private:
        assert "mosswork-company" in installers[0].name
        assert any("company_embedded" in str(args) for args, _ in commands)
        info = plistlib.loads(
            (output.parent / "build-company/dmg/mosswork.app/Contents/Info.plist").read_bytes()
        )
        assert info["CFBundleIdentifier"] == "io.github.sudoprivacy.mosswork"
        assert any("--verify-private-payload" in args for args, _ in commands)
        assert any("--verify-update-payload" in args for args, _ in commands)
        assert (
            output.parent
            / f"build-company/dmg/mosswork.app/Contents/Resources/mosswork-darwin-{arch}.zip"
        ).is_file()
        status = json.loads((output / "company-package-status.json").read_text())
        assert status["company_runtime_included"] is True
        assert status["native_update_payload_checks"] == {f"{platform}-{arch}": True}
        assert status["native_private_payload_verified"] == {f"{platform}-{arch}": True}
    assert all(
        "-X main.desktopVersion=1.0.1b2" in str(args) for args, _ in commands if args[0] == "go"
    )
    assert all(
        ("-X main.companyDefaultURL=https://accounts.example.test" in str(args)) == private
        for args, _ in commands if args[0] == "go"
    )
    updates = list(output.glob("*-update.*"))
    assert len(updates) == 1
    assert updates[0].name.startswith("mosswork-company-")
    if platform == "darwin":
        assert links == [("/Applications", output.parent / "build-company/dmg/Applications")]
    if platform == "windows":
        assert updates[0].read_bytes() == b"shell"
        assert not any("MOSSWORK_COMPANY_RUNTIME_ZIP=" in str(args) for args, _ in commands)
        assert (output.parent / "build-company/src/bundled/portable.zip").is_file()


@pytest.mark.parametrize("invalid", [
    "https://user:password@example.test", "https://example.test/path",
    "https://example.test/?key=value", "https://example.test/#fragment",
    "https://example.test -X main.x=y", "https://example.test:99999",
])
def test_company_origin_rejects_non_origin_values(invalid):
    with pytest.raises(module.argparse.ArgumentTypeError):
        module.company_origin(invalid)


def test_bootstrap_component_cannot_replace_company_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "package-company.py",
            "--platform",
            "darwin",
            "--arch",
            "arm64",
            "--bootstrap-bundle",
            str(tmp_path / "bootstrap"),
            "--output",
            str(tmp_path / "release"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
    assert not (tmp_path / "release").exists()


def test_exported_source_identity_does_not_require_git(tmp_path, monkeypatch):
    root = tmp_path / "export"
    package = root / "src/octop"
    for name in ("infra/devices/protocol.py", "infra/company_runtime/__init__.py"):
        target = package / name
        target.parent.mkdir(parents=True)
        target.write_text("PROTOCOL_VERSION = 1\n")
    (root / "pyproject.toml").write_text('[project]\nversion="1.0.1b2"\n')
    (root / "PUBLIC_SOURCE.json").write_text(
        json.dumps({"source_commit": "original-tag-commit", "source_worktree_dirty": False})
    )
    monkeypatch.setattr(module, "__file__", str(root / "desktop/package-company.py"))
    with patch.object(
        module.subprocess, "check_output", side_effect=AssertionError("Git not available")
    ):
        identity = module.release_identity()
    assert identity["source_commit"] == "original-tag-commit"
    assert identity["source_dirty"] is False


if __name__ == "__main__":
    unittest.main()
