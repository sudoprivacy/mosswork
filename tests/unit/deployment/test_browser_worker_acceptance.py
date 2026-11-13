"""Keep private Worker ownership diagnostics out of reusable CI artifacts."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "browser_worker_acceptance", ROOT / "tools/accept_browser_worker.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    original_path = sys.path[:]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
    return module


def test_tool_import_keeps_shared_python_search_path() -> None:
    original = sys.path[:]
    load_tool()
    assert sys.path == original


def private_failure(output: Path) -> bytes:
    private = output / "worker-profile/launch-failure.json"
    private.parent.mkdir(parents=True)
    data = json.dumps(
        {
            "error_code": "transient",
            "error": "fixture-private-launch-identity",
            "pid": 123,
            "started": True,
            "launch_token": "fixture-private-launch-identity",
            "future_private_field": "fixture-private-launch-identity",
        }
    ).encode()
    private.write_bytes(data)
    return data


def test_reusable_summary_does_not_copy_private_launch_values(tmp_path: Path) -> None:
    original = private_failure(tmp_path)
    load_tool().write_launch_diagnostic(tmp_path)
    summary = tmp_path / "worker-launch-diagnostic.json"
    assert b"fixture-private-launch-identity" not in summary.read_bytes()
    assert json.loads(summary.read_text()) == {
        "valid": True,
        "error_code": "transient",
        "started": True,
        "processIdentityPresent": True,
        "privateLaunchIdentityPresent": True,
    }
    assert (tmp_path / "worker-profile/launch-failure.json").read_bytes() == original


def test_missing_private_diagnostic_is_not_a_secondary_failure(tmp_path: Path) -> None:
    load_tool().write_launch_diagnostic(tmp_path)
    assert not (tmp_path / "worker-launch-diagnostic.json").exists()


def test_launch_profile_does_not_echo_private_arguments() -> None:
    profile = load_tool().launch_profile(
        ["--no-sandbox", "--proxy-server=fixture-private-launch-identity"]
    )
    assert "fixture-private-launch-identity" not in json.dumps(profile)
    assert profile == {"sandboxDisabled": True, "additionalArgumentCount": 2}


def test_default_launch_profile_does_not_claim_sandbox_was_disabled() -> None:
    assert load_tool().launch_profile([]) == {
        "sandboxDisabled": False,
        "additionalArgumentCount": 0,
    }


@pytest.mark.parametrize("raw", [b"invalid", b'["fixture-private-launch-identity"]'])
def test_malformed_diagnostic_has_only_a_bounded_failure_flag(tmp_path: Path, raw: bytes) -> None:
    private = tmp_path / "worker-profile/launch-failure.json"
    private.parent.mkdir()
    private.write_bytes(raw)
    load_tool().write_launch_diagnostic(tmp_path)
    assert json.loads((tmp_path / "worker-launch-diagnostic.json").read_text()) == {"valid": False}


@pytest.mark.parametrize("code", ["fixture-private-launch-identity", [], {}, 7, None])
def test_unknown_error_code_does_not_become_an_artifact_channel(
    tmp_path: Path, code: object
) -> None:
    private_failure(tmp_path)
    private = tmp_path / "worker-profile/launch-failure.json"
    value = json.loads(private.read_text())
    value["error_code"] = code
    private.write_text(json.dumps(value))
    load_tool().write_launch_diagnostic(tmp_path)
    raw = (tmp_path / "worker-launch-diagnostic.json").read_bytes()
    assert b"fixture-private-launch-identity" not in raw
    assert json.loads(raw)["error_code"] == "unrecognized"


def test_configured_upload_excludes_private_launch_identity(tmp_path: Path) -> None:
    output = tmp_path / "browser-worker"
    private_failure(output)
    load_tool().write_launch_diagnostic(output)
    (output / "receipt.json").write_text(
        json.dumps(
            {
                "chromeLaunch": load_tool().launch_profile(
                    ["--no-sandbox", "--proxy-server=fixture-private-launch-identity"]
                )
            }
        )
    )
    workflow = yaml.safe_load((ROOT / ".github/workflows/mosswork-browser-ci.yml").read_text())
    upload = next(
        step
        for step in workflow["jobs"]["browser"]["steps"]
        if step.get("uses") == "actions/upload-artifact@v4"
    )
    selected = [
        Path(line.strip().replace("${{ runner.temp }}", str(tmp_path)))
        for line in upload["with"]["path"].splitlines()
        if line.strip()
    ]
    selected = [path for path in selected if path.is_file()]
    archive = tmp_path / "selected-artifact.zip"
    with ZipFile(archive, "w") as zipped:
        for path in selected:
            zipped.write(path, path.relative_to(tmp_path).as_posix())
    with ZipFile(archive) as zipped:
        payloads = [zipped.read(name) for name in zipped.namelist()]
        assert all(b"fixture-private-launch-identity" not in data for data in payloads)
        assert "browser-worker/worker-launch-diagnostic.json" in zipped.namelist()
        assert "browser-worker/worker-profile/launch-failure.json" not in zipped.namelist()
