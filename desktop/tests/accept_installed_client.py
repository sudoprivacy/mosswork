"""Install published client artifacts in a disposable native GUI runner."""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from desktop.macos_distribution import digest, mounted_dmg, one_app, write_receipt


def run(*command: str | Path, timeout: int = 180) -> bytes:
    return subprocess.check_output([str(part) for part in command], timeout=timeout)


def wait_for[T](process: subprocess.Popen[bytes], probe: Callable[[], T], description: str) -> T:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Client exited before {description}: {process.returncode}")
        result = probe()
        if result:
            return result
        time.sleep(0.25)
    raise TimeoutError(description)


@contextmanager
def client(binary: Path, env: dict[str, str], log: Path) -> Iterator[subprocess.Popen[bytes]]:
    with log.open("wb") as stream:
        process = subprocess.Popen(
            [binary], cwd=binary.parent, env=env, stdout=stream, stderr=subprocess.STDOUT
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        timeout=15,
                        check=False,
                    )
                else:
                    process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)


def windows(
    installer: Path, target: Path, env: dict[str, str], evidence: Path, sums: dict[str, str]
) -> dict[str, object]:
    run(installer, "/S", f"/D={target}", timeout=300)
    binary = target / "mosswork.exe"
    update = installer.with_name(f"{installer.stem}-update.exe")
    installed_digest = digest(binary)
    if installed_digest != sums[update.name]:
        raise ValueError("Installed executable differs from the published update executable")
    webview_profile = Path(env["APPDATA"]) / binary.name
    if webview_profile.exists():
        raise ValueError("This test requires a fresh Windows runner profile")
    driver = Path(__file__).with_name("accept_installed_windows_ui.ps1")
    command = (
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-STA",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        driver,
    )
    with client(binary, env, evidence / "client-first-launch.log") as process:
        run(*command, "-ClientPid", str(process.pid), "-SwitchToEnglish", timeout=120)
        settings = Path(env["MOSSWORK_HOME"]) / "desktop-settings.json"
        assert json.loads(settings.read_text(encoding="utf-8"))["locale"] == "en"
        assert webview_profile.is_dir()
    with client(binary, env, evidence / "client-relaunch.log") as process:
        run(*command, "-ClientPid", str(process.pid), timeout=66)
    return {
        "installed_executable_sha256": installed_digest,
        "windows_ui_automation_login_heading": "Employee sign in",
        "locale_persisted_after_relaunch": True,
    }


WINDOW_PROBE = """
import CoreGraphics
import Foundation
let pid = Int(CommandLine.arguments[1])!
let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
    kCGNullWindowID) as? [[String: Any]] ?? []
let main = windows.filter {
    guard $0[kCGWindowOwnerPID as String] as? Int == pid,
          $0[kCGWindowLayer as String] as? Int == 0,
          let bounds = $0[kCGWindowBounds as String] as? [String: Double] else { return false }
    return bounds["Width", default: 0] >= 600 && bounds["Height", default: 0] >= 400
}
print(main.count)
"""


def macos(
    installer: Path, target: Path, env: dict[str, str], evidence: Path, version: str
) -> dict[str, object]:
    with mounted_dmg(installer, target.parent / "mounted") as mounted:
        source = one_app(mounted)
        bundle = target / source.name
        run("ditto", source, bundle)
    run("codesign", "--verify", "--deep", "--strict", bundle)
    with (bundle / "Contents/Info.plist").open("rb") as stream:
        metadata = plistlib.load(stream)
    assert metadata["CFBundleShortVersionString"] == version
    binary = bundle / "Contents/MacOS" / metadata["CFBundleExecutable"]
    swift = target.parent / "visible-window.swift"
    swift.write_text(WINDOW_PROBE, encoding="utf-8")
    probe = target.parent / "visible-window"
    run("swiftc", "-o", probe, swift)
    with client(binary, env, evidence / "client.log") as process:
        count = wait_for(
            process, lambda: int(run(probe, str(process.pid), timeout=10)), "visible native window"
        )
    return {
        "bundle_id": metadata["CFBundleIdentifier"],
        "installed_executable_sha256": digest(binary),
        "visible_native_windows": count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", args.version) is None:
        parser.error("expected a numeric release version")
    native = (platform.system(), platform.machine().lower())
    targets = {("Darwin", "arm64"): "macos-arm64.dmg", ("Windows", "amd64"): "windows-amd64.exe"}
    target = targets.get(native)
    if target is None:
        parser.error(f"unsupported runner: {native}")
    artifact_platform, extension = target.split(".")
    release = args.release_dir.resolve()
    installer = release / f"mosswork-company-{artifact_platform}-{args.version}.{extension}"
    sums = {
        name: value
        for value, name in (
            line.split(maxsplit=1) for line in (release / "SHA256SUMS.txt").read_text().splitlines()
        )
    }
    installer_digest = digest(installer)
    if installer_digest != sums[installer.name]:
        raise ValueError(f"Checksum mismatch for {installer.name}")
    output = args.output.resolve()
    evidence = output / "evidence"
    evidence.mkdir(parents=True)
    install_path = output / "installed"
    env = {
        **os.environ,
        "MOSSWORK_HOME": str(output / "profile"),
        "MOSSWORK_COMPANY_URL": "",
    }
    if extension == "dmg":
        result = macos(installer, install_path, env, evidence, args.version)
    else:
        result = windows(installer, install_path, env, evidence, sums)
    receipt = {
        "version": args.version,
        "platform": artifact_platform,
        "artifact": installer.name,
        "installer_sha256": installer_digest,
        **result,
    }
    write_receipt(evidence / "receipt.json", receipt, exclusive=True)
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
