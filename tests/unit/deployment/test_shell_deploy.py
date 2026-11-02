"""Execute the release shell entry points against a stateful fake Docker CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Deployment uses POSIX shell")
SCRIPTS = Path(__file__).resolve().parents[3] / "docker/enterprise"


@pytest.fixture
def shell_deployment(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ['DEPLOY_TEST_ROOT'])
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(args) + '\n')
if os.environ.get('DEPLOY_TEST_FAILURE') in args:
    raise SystemExit('requested Docker failure')
if args[:1] != ['compose']:
    raise SystemExit(0)
data = pathlib.Path(os.environ.get('MOSSWORK_DATA_ROOT', root / 'data'))
users = data / '.fake-users'
if args[-2:] == ['config', '--images']:
    print(os.environ['MOSSWORK_APP_IMAGE'])
    print(os.environ['MOSSWORK_IM_IMAGE'])
    print('postgres:17.11-bookworm')
elif 'psql' in args:
    if 'to_regclass' in args[-1]:
        print('t' if users.exists() else 'f')
    else:
        print('1' if users.exists() else '0')
elif 'init' in args:
    users.touch()
"""
    )
    docker.chmod(0o755)
    install = tmp_path / "nested parent" / "installation"
    env = {key: value for key, value in os.environ.items() if not key.startswith("MOSSWORK_")}
    env.update(PATH=str(binary) + os.pathsep + env["PATH"], DEPLOY_TEST_ROOT=str(tmp_path))

    def run(version="v0.0.3", *, failure="", upgrade=False, directory=install, script=None):
        if script:
            command = ["bash", str(directory / script)]
        else:
            command = [
                "bash",
                str(SCRIPTS / "deploy.sh"),
                "--version",
                version,
                "--install-dir",
                str(directory),
            ]
            if upgrade:
                command.append("--upgrade")
        return subprocess.run(
            command,
            env={**env, "DEPLOY_TEST_FAILURE": failure},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def calls():
        return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]

    return install, run, calls


def test_first_install_and_restart_keep_secrets_private(shell_deployment):
    install, run, calls = shell_deployment
    result = run()
    assert result.returncode == 0, result.stderr
    config = (install / ".env").read_text()
    initial = (install / "initial-admin.json").read_bytes()
    password = json.loads(initial)["password"]
    assert "MOSSWORK_BIND_ADDRESS=0.0.0.0" in config
    for name in (".env", "deployment.env", "initial-admin.json", "data/secrets/im_identity"):
        assert (install / name).stat().st_mode & 0o777 == 0o600
    assert not (install / ".deployment-runtime.env").exists()
    assert not (install / "current").readlink().is_absolute()
    assert (install / "docker-compose.yml").is_file()
    # PostgreSQL executes this bind-mounted script as the container's postgres user.
    assert (install / "current/init-databases.sh").stat().st_mode & 0o777 == 0o755
    result = run(script="start.sh")
    assert result.returncode == 0, result.stderr
    assert (install / "initial-admin.json").read_bytes() == initial
    assert (install / ".env").read_text() == config
    assert sum("init" in call for call in calls()) == 1
    assert password not in json.dumps(calls())
    assert run(script="stop.sh").returncode == 0
    assert calls()[-1][-1] == "down"
    assert (install / "data/.fake-users").exists()


@pytest.mark.parametrize("failure", ["inspect", "--migrate", "300"])
def test_failed_first_install_resumes_without_rotating_keys(shell_deployment, failure):
    install, run, calls = shell_deployment
    assert run(failure=failure).returncode != 0
    key = (install / "data/secrets/im_identity").read_bytes()
    result = run()
    assert result.returncode == 0, result.stderr
    assert (install / "data/secrets/im_identity").read_bytes() == key
    assert sum("init" in call for call in calls()) == 1


def test_upgrade_checks_database_and_only_commits_after_health(shell_deployment):
    install, run, calls = shell_deployment
    assert run().returncode == 0
    # Simulate a pre-shell installation, which has no local initialized marker.
    (install / "deployment.env").unlink()
    (install / ".env").rename(install / "deployment.env")
    config = (install / "deployment.env").read_bytes()
    assert run("v0.0.4").returncode != 0
    assert run("v0.0.4", upgrade=True, failure="300").returncode != 0
    assert (install / "deployment.env").read_bytes() == config
    assert (install / "current").resolve().name == "v0.0.3"
    result = run("v0.0.4", upgrade=True)
    assert result.returncode == 0, result.stderr
    assert "moss-server:0.0.4" in (install / ".env").read_text()
    assert (install / "current").resolve().name == "v0.0.4"
    assert sum("init" in call for call in calls()) == 1


def test_copied_directory_uses_new_project_port_and_data(shell_deployment, tmp_path):
    install, run, calls = shell_deployment
    assert run().returncode == 0
    copy = tmp_path / "copy"
    shutil.copytree(install, copy, symlinks=True)
    config = (copy / ".env").read_text().replace("moss-server\n", "moss-server-test\n")
    config = config.replace("MOSSWORK_PORT=8088", "MOSSWORK_PORT=18089")
    config = config.replace(str(install / "data"), str(copy / "data"))
    (copy / ".env").write_text(config)
    result = run(directory=copy, script="start.sh")
    assert result.returncode == 0, result.stderr
    assert (copy / "current").resolve().is_relative_to(copy)
    assert "18089" in result.stdout
    assert "MOSSWORK_PORT=8088" in (install / ".env").read_text()
    assert calls()[-1][calls()[-1].index("--project-name") + 1] == "moss-server-test"


def test_release_template_can_start_in_place(shell_deployment, tmp_path):
    _, run, _ = shell_deployment
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in (
        "deploy.sh",
        "prepare.sh",
        "start.sh",
        "stop.sh",
        "compose.release.yml",
        "init-databases.sh",
    ):
        shutil.copy2(SCRIPTS / name, bundle / name)
    (bundle / ".env").write_text(
        (SCRIPTS / ".env.example").read_text().replace("@@MOSSWORK_RELEASE_TAG@@", "v0.0.3")
    )
    result = run(directory=bundle, script="start.sh")
    assert result.returncode == 0, result.stderr
    assert (bundle / "data/.fake-users").exists()


@pytest.mark.parametrize("tag", ["main", "v0.0.3/evil", "v0.0.3\n"])
def test_invalid_version_cannot_write_installation(shell_deployment, tag):
    install, run, _ = shell_deployment
    assert run(tag).returncode != 0
    assert not install.exists()
