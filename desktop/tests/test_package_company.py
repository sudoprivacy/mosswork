"""Targeted offline packaging contract tests; no installers or downloads."""

import hashlib
import importlib.util
import json
import os
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
            "packages/mosswork/company_runtime.py",
            "packages/moss_harness/__init__.py",
            "tools/ffmpeg",
            "tools/ffprobe",
            "packages/moss_harness-0.0.1.dist-info/METADATA",
        ]
        names += [f"packages/{m}/__init__.py" for m in module.RUNTIME_MODULES]
        for name in names:
            item = source / name
            item.parent.mkdir(parents=True, exist_ok=True)
            if name in ("runtime/bin/python3", "tools/ffmpeg", "tools/ffprobe"):
                item.write_bytes(
                    b"\xcf\xfa\xed\xfe" + (0x100000C).to_bytes(4, "little") + b"\0" * 56
                )
            elif name.endswith("METADATA"):
                item.write_text("Version: 0.0.1\n")
            else:
                item.write_text("company-runtime")
        app_metadata = source / "packages/mosswork-1.0.0.dist-info/METADATA"
        app_metadata.parent.mkdir()
        app_metadata.write_text("Version: 1.0.0\n")
        names.append(app_metadata.relative_to(source).as_posix())
        self.release["python_source_sha256"] = module.python_source_digest(
            source / "packages/mosswork"
        )
        (source / "company-runtime-build.json").write_text(
            json.dumps(
                {
                    "platform": "darwin",
                    "arch": "arm64",
                    "protocol_version": 1,
                    "harness_version": "0.0.1",
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
            (source / "packages/mosswork/company_runtime.py").write_text("modified source")
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
                "Version: 0.0.1\n"
            )
            with self.assertRaisesRegex(ValueError, "version must match"):
                module.package_runtime(source, root / "bad.zip", "darwin", "arm64")


@pytest.mark.skipif(os.name != "posix", reason="macOS app packaging creates POSIX symlinks")
@pytest.mark.parametrize(
    "platform,arch,extension", [("darwin", "arm64", ".dmg"), ("windows", "amd64", ".exe")]
)
def test_single_arch_build_needs_only_its_own_payload(
    tmp_path, monkeypatch, platform, arch, extension
):
    root = tmp_path / "repo"
    source = root / "desktop/src"
    (source / "build/darwin").mkdir(parents=True)
    (source / "build/windows/nsis").mkdir(parents=True)
    (source / "assets").mkdir()
    (source / "assets/company-dashboard.zip").write_bytes(b"dashboard")
    (source / "build/darwin/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": "mosswork"})
    )
    (root / "pyproject.toml").write_text('[project]\nversion="1.0.1b2"\n')
    webview = tmp_path / "webview"
    webview.mkdir()
    with (webview / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe").open("wb") as stream:
        stream.truncate(50_000_001)
    output = tmp_path / "release"
    commands, payloads = [], []

    def run(*args, cwd, env=None):
        commands.append((args, env))
        if args[0] == "go":
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

    def payload(source, destination, target_platform, target_arch):
        payloads.append((target_platform, target_arch))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"verified runtime")
        return destination

    monkeypatch.setattr(module, "__file__", str(root / "desktop/package-company.py"))
    monkeypatch.setattr(module, "release_identity", lambda: {"source_commit": "fixture"})
    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(module, "package_runtime", payload)
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
            "--webview2-dir",
            str(webview),
            "--output",
            str(output),
        ],
    )
    module.main()
    assert payloads == [(platform, arch)]
    assert [env["GOARCH"] for args, env in commands if args[0] == "go"] == [arch]
    assert not any(args[0] == "lipo" for args, _ in commands)
    assert len(list(output.glob("*" + extension))) == 1
    assert arch in next(output.glob("*" + extension)).name


def test_exported_source_identity_does_not_require_git(tmp_path, monkeypatch):
    root = tmp_path / "export"
    package = root / "src/mosswork"
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
