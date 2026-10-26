"""Contracts between the vendored engine and the mosswork distribution."""

from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
from moss_harness import HarnessAgent
from moss_harness.backends.workspace import remote_workspace_virtual_root
from moss_harness.cli.main import cli
from moss_harness.runtime_env import resolve_local_execute_env
from moss_harness.skills.catalog import _summary_dict


def test_engine_cli_uses_distribution_version_and_managed_updates():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"moss-harness, version {version('moss-harness')}"
    result = runner.invoke(cli, ["update"])
    assert result.exit_code == 1
    assert "https://github.com/sudoprivacy/mosswork/releases" in result.output


def test_engine_protects_host_and_harness_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MOSSWORK_HOME", str(tmp_path))
    monkeypatch.setenv("MOSS_HARNESS_DEBUG", "0")
    (tmp_path / ".env").write_text(
        "MOSSWORK_HOME=/other\nMOSS_HARNESS_DEBUG=1\nPROJECT_MODE=test\n"
    )
    env = resolve_local_execute_env(workspace_dir=tmp_path)
    assert env["MOSSWORK_HOME"] == str(tmp_path)
    assert env["MOSS_HARNESS_DEBUG"] == "0"
    assert env["PROJECT_MODE"] == "test"


def test_scoped_workspace_paths_resolve_below_mount(tmp_path, monkeypatch):
    agent = HarnessAgent.__new__(HarnessAgent)
    agent._config = SimpleNamespace(workspace_dir=Path("/.mosswork/workspaces/agent-1"))
    monkeypatch.setattr(agent, "_peek_backend_mount", lambda: (tmp_path, True))
    assert agent._host_workspace_path() == tmp_path / ".mosswork/workspaces/agent-1"
    assert agent._agent_visible_workspace_dir() == Path("/.mosswork/workspaces/agent-1")


def test_remote_workspace_uses_branded_virtual_path(tmp_path):
    assert remote_workspace_virtual_root(tmp_path / "agent-1") == "/.mosswork/workspaces/agent-1"
    assert (
        remote_workspace_virtual_root(Path("/.mosswork/workspaces/agent-1"))
        == "/.mosswork/workspaces/agent-1"
    )


def test_skill_catalog_prefers_current_metadata_and_supports_legacy():
    current = {"label": {"en": "Current"}, "summary": {"en": "Current summary"}}
    legacy = {"label": {"en": "Legacy"}, "summary": {"en": "Legacy summary"}}
    result = _summary_dict(
        "demo", {"metadata": {"mosswork": current, "octop": legacy}}, enabled=True, kind="builtin"
    )
    assert result["label"] == current["label"]
    assert result["summary"] == current["summary"]
    result = _summary_dict("demo", {"metadata": {"octop": legacy}}, enabled=True, kind="builtin")
    assert result["label"] == legacy["label"]
