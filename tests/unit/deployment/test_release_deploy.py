"""Exercise installation/retry/upgrade behavior without a Docker daemon."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux installer uses POSIX symlinks")


@pytest.fixture
def installation(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[3] / "docker/enterprise"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("release_deploy", scripts / "deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    state = {"users": False, "fail": None, "prepared": 0}

    def prepare(data, env_file, socket):
        state["prepared"] += 1
        data.mkdir(parents=True)
        (data / "identity").write_text("original identity key")
        module.write_config(
            env_file,
            {
                "MOSSWORK_DATA_ROOT": str(data),
                "MOSSWORK_APP_IMAGE": "mosswork-enterprise:source",
                "MOSSWORK_APP_PASSWORD": "original-password",
                "MOSSWORK_BIND_ADDRESS": "127.0.0.1",
            },
        )

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if state["fail"] in command:
            raise subprocess.CalledProcessError(1, command)
        if "init" in command:
            state["users"] = True

    def query(command, **kwargs):
        if command[-1] == "--images":
            return "\n".join(
                [
                    kwargs["env"]["MOSSWORK_APP_IMAGE"],
                    kwargs["env"]["MOSSWORK_IM_IMAGE"],
                    "postgres:17.11-bookworm",
                ]
            )
        if "to_regclass" in command[-1]:
            return "t\n" if state["users"] else "f\n"
        return "1\n" if state["users"] else "0\n"

    monkeypatch.setattr(module, "prepare", prepare)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "check_output", query)
    args = argparse.Namespace(
        version="v1.0.1b2",
        install_dir=tmp_path / "install",
        socket=tmp_path / "docker.sock",
        bind=None,
        port=None,
        upgrade=False,
    )
    return module, args, calls, state


def test_install_initializes_once_and_keeps_credentials_off_command_line(installation):
    module, args, calls, state = installation
    module.deploy(args)
    initial = json.loads((args.install_dir / "initial-admin.json").read_text())
    assert (args.install_dir / "initial-admin.json").stat().st_mode & 0o777 == 0o600
    assert initial["password"] not in str([call[0] for call in calls])
    assert sum("init" in command for command, _ in calls) == 1
    module.deploy(args)
    assert state["prepared"] == 1
    assert sum("init" in command for command, _ in calls) == 1
    assert json.loads((args.install_dir / "initial-admin.json").read_text()) == initial
    config = module.read_config(args.install_dir / "deployment.env")
    assert config["MOSSWORK_BIND_ADDRESS"] == "0.0.0.0"
    assert config["MOSSWORK_APP_PASSWORD"] == "original-password"
    assert (args.install_dir / "data/identity").read_text() == "original identity key"
    assert config["MOSSWORK_APP_IMAGE"] == "moss-server:1.0.1b2"
    assert config["MOSSWORK_IM_IMAGE"] == "moss-server-im:1.0.1b2"
    assert not any("pull" in command for command, _ in calls)
    inspected = next(
        command for command, _ in calls if command[:3] == ["docker", "image", "inspect"]
    )
    assert inspected[3:] == [
        "moss-server:1.0.1b2",
        "moss-server-im:1.0.1b2",
        "postgres:17.11-bookworm",
    ]


@pytest.mark.parametrize("failure", ["inspect", "--migrate", "300"])
def test_failed_first_install_can_resume_without_rotating_keys(installation, failure):
    module, args, calls, state = installation
    state["fail"] = failure
    with pytest.raises(subprocess.CalledProcessError):
        module.deploy(args)
    state["fail"] = None
    module.deploy(args)
    assert state["prepared"] == 1
    assert sum("init" in command for command, _ in calls) == 1
    assert (args.install_dir / "current").resolve().name == args.version


def test_upgrade_is_explicit_and_only_commits_configuration_after_health_check(installation):
    module, args, calls, state = installation
    module.deploy(args)
    config = args.install_dir / "deployment.env"
    original = config.read_bytes()
    args.version = "v1.0.2"
    with pytest.raises(ValueError, match="--upgrade"):
        module.deploy(args)
    assert config.read_bytes() == original
    args.upgrade = True
    state["fail"] = "300"
    with pytest.raises(subprocess.CalledProcessError):
        module.deploy(args)
    assert config.read_bytes() == original
    assert (args.install_dir / "current").resolve().name == "v1.0.1b2"
    state["fail"] = None
    module.deploy(args)
    assert module.read_config(config)["MOSSWORK_APP_IMAGE"].endswith(":1.0.2")
    assert sum("init" in command for command, _ in calls) == 1
    assert any("stop" in command for command, _ in calls)


def test_existing_database_skips_admin_creation_even_without_local_receipt(installation):
    module, args, calls, state = installation
    state["users"] = True
    module.deploy(args)
    assert not any("init" in command for command, _ in calls)
    assert not (args.install_dir / "initial-admin.json").exists()


@pytest.mark.parametrize("version", ["main", "v1.2.3/evil", "v1.2.3\n"])
def test_bad_tags_do_not_write_configuration(installation, version):
    module, args, calls, state = installation
    args.version = version
    with pytest.raises(ValueError):
        module.deploy(args)
    assert state["prepared"] == 0
    assert calls == []
