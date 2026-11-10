"""Build, test, install and verify the current portable compiler worker.

The output is a new private directory. No production identity, model, source
database or credentials are consumed. Compiler interoperability is a separate
required acceptance using the returned installed CLI and its SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="New package acceptance directory"
    )
    args = parser.parse_args()
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    package = Path(__file__).resolve().parents[1] / "packages" / "sudograph-worker"
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        parser.error("Require Node.js >=22.21 and npm")
    npm_command = (
        [node, str(Path(npm).parent / "node_modules/npm/bin/npm-cli.js")]
        if sys.platform == "win32"
        else [npm]
    )
    environment = {**os.environ, "npm_config_engine_strict": "true", "npm_config_jobs": "1"}
    steps: list[dict[str, object]] = []

    def run(name: str, command: list[str], *, compiler: bool = False) -> str:
        started = time.monotonic()
        result = subprocess.run(
            command,
            cwd=package,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                | (subprocess.BELOW_NORMAL_PRIORITY_CLASS if compiler else 0)
                if sys.platform == "win32"
                else 0
            ),
        )
        (output / f"{name}.stdout.log").write_text(result.stdout, encoding="utf-8")
        (output / f"{name}.stderr.log").write_text(result.stderr, encoding="utf-8")
        steps.append(
            {
                "name": name,
                "exit_code": result.returncode,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "priority": "BelowNormal" if compiler and sys.platform == "win32" else "Normal",
            }
        )
        print(json.dumps(steps[-1]), flush=True)
        if result.returncode:
            raise RuntimeError(f"{name} failed; retained stdout/stderr in the output directory")
        return result.stdout

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    try:
        lock = package / "npm-shrinkwrap.json"
        lock_hash = sha(lock)
        run(
            "install",
            [*npm_command, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            compiler=True,
        )
        assert sha(lock) == lock_hash, "Dependency lock changed during installation"
        run("build", [*npm_command, "run", "build"], compiler=True)
        run("refusal-tests", [*npm_command, "test"])
        packed = json.loads(
            run(
                "pack",
                [
                    *npm_command,
                    "pack",
                    "--ignore-scripts",
                    "--json",
                    "--pack-destination",
                    str(output),
                ],
            )
        )
        assert len(packed) == 1
        archive = output / packed[0]["filename"]
        installed = output / "installed"
        run(
            "install-artifact",
            [
                *npm_command,
                "install",
                "--prefix",
                str(installed),
                "--omit=dev",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                str(archive),
            ],
            compiler=True,
        )
        installed_package = installed / "node_modules/@sudo/sudograph-worker"
        run("verify-artifact", [node, "tools/artifact.mjs", "--verify", str(installed_package)])
        assert sha(lock) == lock_hash
        worker = installed_package / "dist/compilerContextWorkerCli.js"
        receipt = {
            "format": "mosswork-sudograph-worker-package/v1",
            "passed": True,
            "at_utc": datetime.now(UTC).isoformat(),
            "archive": str(archive),
            "archive_sha256": sha(archive),
            "npm_integrity": packed[0]["integrity"],
            "worker": str(worker),
            "worker_sha256": sha(worker),
            "build_manifest_sha256": sha(installed_package / "dist/manifest.json"),
            "dependency_lock_sha256": lock_hash,
            "steps": steps,
            "scope": "Current-source build, real HTTP/MCP refusal tests and installed package byte verification; no compiler/model/native/grant/comment/production acceptance",
        }
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(receipt), flush=True)
        return 0
    except BaseException as error:
        (output / "incomplete.json").write_text(
            json.dumps(
                {"passed": False, "steps": steps, "error": f"{type(error).__name__}: {error}"},
                indent=2,
            ),
            encoding="utf-8",
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
