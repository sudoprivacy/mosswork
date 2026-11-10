"""A clean install must resolve vendored dependencies without a registry cache."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_vendored_xlsx_installs_offline_with_an_empty_cache(tmp_path, repo_root):
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        pytest.skip("npm is required for the dashboard dependency acceptance")
    dashboard = repo_root / "dashboard"
    package = json.loads((dashboard / "package.json").read_text())
    lock = json.loads((dashboard / "package-lock.json").read_text())
    spec = package["dependencies"]["xlsx"]
    assert spec.startswith("file:vendor/")
    project = tmp_path / "dashboard"
    tarball = Path(spec.removeprefix("file:"))
    (project / tarball).parent.mkdir(parents=True)
    shutil.copyfile(dashboard / tarball, project / tarball)
    shutil.copyfile(dashboard / ".npmrc", project / ".npmrc")
    fixture = {
        "name": "vendored-dashboard-dependency-acceptance",
        "version": "1.0.0",
        "private": True,
        "dependencies": {"xlsx": spec},
    }
    (project / "package.json").write_text(json.dumps(fixture))
    fixture_lock = {
        "name": fixture["name"],
        "version": fixture["version"],
        "lockfileVersion": lock["lockfileVersion"],
        "requires": True,
        "packages": {"": fixture, "node_modules/xlsx": lock["packages"]["node_modules/xlsx"]},
    }
    (project / "package-lock.json").write_text(json.dumps(fixture_lock))
    env = dict(os.environ)
    for key in ("npm_config_replace_registry_host", "NPM_CONFIG_REPLACE_REGISTRY_HOST"):
        env.pop(key, None)
    result = subprocess.run(
        [
            npm,
            "ci",
            "--offline",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--cache",
            str(tmp_path / "empty-cache"),
        ],
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout
    installed = json.loads((project / "node_modules/xlsx/package.json").read_text())
    assert installed["version"] == lock["packages"]["node_modules/xlsx"]["version"]
