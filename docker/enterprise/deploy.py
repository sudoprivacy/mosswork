#!/usr/bin/env python3
"""Install a released moss-server stack; retain credentials and data on reruns."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

from prepare import prepare


def read_config(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values


def write_config(path: Path, values: dict[str, str]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        for key, value in values.items():
            if any(char in value for char in '\n\r"$'):
                raise ValueError(f"Unsupported character in {key}")
            stream.write(f'{key}="{value}"\n')
    temporary.replace(path)


def deploy(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9._-]*", args.version):
        raise ValueError("Use a release tag such as v1.0.1b2")
    if args.bind is not None and ipaddress.ip_address(args.bind).version != 4:
        raise ValueError("--bind requires an IPv4 address")
    if args.port is not None and not 1 <= args.port <= 65535:
        raise ValueError("--port must be between 1 and 65535")
    install = args.install_dir.resolve()
    config = install / "deployment.env"
    fresh = not config.exists()
    image = f"moss-server:{args.version[1:]}"
    if config.exists():
        values = read_config(config)
        if values["MOSSWORK_APP_IMAGE"] != image and not args.upgrade:
            raise ValueError("Existing installation: back it up, then pass --upgrade")
    else:
        if args.upgrade:
            raise ValueError("--upgrade requires an existing deployment.env")
        prepare(install / "data", config, args.socket)
        values = read_config(config)
        values["MOSSWORK_BIND_ADDRESS"] = "0.0.0.0"
    values.update(
        MOSSWORK_APP_IMAGE=image,
        MOSSWORK_IM_IMAGE=f"moss-server-im:{args.version[1:]}",
    )
    if args.bind is not None:
        values["MOSSWORK_BIND_ADDRESS"] = args.bind
    if args.port is not None:
        values["MOSSWORK_PORT"] = str(args.port)
    if fresh:
        # Preserve generated credentials and the requested version if startup fails.
        write_config(config, values)
    # Each release keeps its own compose/init files for inspection and recovery.
    release = install / "releases" / args.version
    release.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent
    for name in ("compose.release.yml", "init-databases.sh", "prepare.py", "deploy.py"):
        if source / name != release / name:
            shutil.copy2(source / name, release / name)
    environment = {**os.environ, **values}
    compose = [
        "docker",
        "compose",
        "--project-name",
        "moss-server",
        "--env-file",
        str(config),
        "-f",
        str(release / "compose.release.yml"),
    ]

    def run(*arguments: str, extra_env: dict[str, str] | None = None) -> None:
        subprocess.run(
            compose + list(arguments), env={**environment, **(extra_env or {})}, check=True
        )

    def query(sql: str) -> str:
        return subprocess.check_output(
            compose
            + [
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "mosswork_app",
                "-d",
                "mosswork",
                "-Atc",
                sql,
            ],
            env=environment,
            text=True,
        ).strip()

    run("config", "--quiet")
    # Release images are imported by the installer; do not contact a registry.
    images = subprocess.check_output(compose + ["config", "--images"], env=environment, text=True)
    subprocess.run(
        ["docker", "image", "inspect", *images.split()], check=True, stdout=subprocess.DEVNULL
    )
    if args.upgrade:
        run("stop", "moss-server", "communication")
    run("up", "-d", "--no-build", "--wait", "--wait-timeout", "180", "postgres")
    run("run", "--rm", "-T", "--no-deps", "communication", "--migrate")
    has_users = query("SELECT to_regclass('public.users') IS NOT NULL") == "t"
    if not has_users or query("SELECT count(*) FROM users") == "0":
        credentials = install / "initial-admin.json"
        if credentials.exists():
            admin = json.loads(credentials.read_text())
        else:
            admin = {"username": "admin", "password": secrets.token_urlsafe(24) + "aA1!"}
            with credentials.open("x", encoding="utf-8") as stream:
                os.chmod(credentials, 0o600)
                json.dump(admin, stream)
                stream.write("\n")
        run(
            "run",
            "--rm",
            "-T",
            "--no-deps",
            "-e",
            "MOSSWORK_ADMIN_USERNAME",
            "-e",
            "MOSSWORK_ADMIN_PASSWORD",
            "moss-server",
            "moss-server",
            "init",
            "--yes",
            extra_env={
                "MOSSWORK_ADMIN_USERNAME": admin["username"],
                "MOSSWORK_ADMIN_PASSWORD": admin["password"],
            },
        )
        print(f"Initial admin credentials: {credentials} (root only; change password after login)")
    run("up", "-d", "--no-build", "--wait", "--wait-timeout", "300")
    write_config(config, values)
    current = install / "current"
    replacement = install / ".current-next"
    replacement.unlink(missing_ok=True)
    replacement.symlink_to(release, target_is_directory=True)
    replacement.replace(current)
    print(f"moss-server {args.version} ready on port {values.get('MOSSWORK_PORT', '8088')}")
    print(f"Configuration: {config}; data: {values['MOSSWORK_DATA_ROOT']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--install-dir", type=Path, default=Path("/opt/moss-server"))
    parser.add_argument("--socket", type=Path, default=Path("/var/run/docker.sock"))
    parser.add_argument("--bind", help="Listen IPv4 address (new installs: 0.0.0.0)")
    parser.add_argument("--port", type=int, help="Published port (new installs: 8088)")
    parser.add_argument("--upgrade", action="store_true")
    args = parser.parse_args()
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        parser.error("Run on the Linux Docker host with sudo")
    subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["docker", "compose", "version"], check=True)
    deploy(args)


if __name__ == "__main__":
    main()
