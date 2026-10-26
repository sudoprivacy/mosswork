"""tests/unit/test_paths.py"""

from __future__ import annotations

from pathlib import Path

from mosswork.infra.utils.paths import PathLayout


def test_root_paths(tmp_path: Path):
    p = PathLayout(tmp_path / ".mosswork")
    assert p.root == tmp_path / ".mosswork"
    assert p.db == tmp_path / ".mosswork" / "mosswork.db"
    assert p.logs_dir == tmp_path / ".mosswork" / "logs"
    assert p.log == tmp_path / ".mosswork" / "logs" / "mosswork.log"
    assert p.config == tmp_path / ".mosswork" / "config.json"


def test_log_dir_creation(tmp_path: Path):
    p = PathLayout(tmp_path / ".mosswork")
    assert not (tmp_path / ".mosswork" / "logs").exists()
    assert p.ensure_log() == tmp_path / ".mosswork" / "logs" / "mosswork.log"
    assert (tmp_path / ".mosswork" / "logs").is_dir()
    assert p.log == tmp_path / ".mosswork" / "logs" / "mosswork.log"


def test_user_dir_uses_username(tmp_path: Path):
    p = PathLayout(tmp_path / ".mosswork")
    assert p.user_dir("alice") == tmp_path / ".mosswork" / "users" / "alice"


def test_agent_workspace(tmp_path: Path):
    p = PathLayout(tmp_path / ".mosswork")
    ws = p.agent_workspace("agent01")
    assert ws == tmp_path / ".mosswork" / "agents" / "agent01"


def test_ensure_root_creates_dir(tmp_path: Path):
    p = PathLayout(tmp_path / ".mosswork")
    p.ensure_root()
    assert (tmp_path / ".mosswork").is_dir()


def test_ensure_agent_workspace(tmp_path: Path):
    p = PathLayout(tmp_path / ".mosswork")
    # New global path
    out = p.ensure_agent_workspace("a1")
    assert out.is_dir()
    assert out == tmp_path / ".mosswork" / "agents" / "a1"


def test_backups_dir(tmp_path: Path) -> None:
    p = PathLayout(tmp_path / ".mosswork")
    assert p.backups_dir == tmp_path / ".mosswork" / "backups"
    assert p.ensure_backups_dir().is_dir()


def test_knowledge_dir(tmp_path: Path) -> None:
    p = PathLayout(tmp_path / ".mosswork")
    assert p.knowledge_dir == tmp_path / ".mosswork" / "knowledge"


def test_path_layout_from_env_defaults_to_dot_mosswork(monkeypatch) -> None:
    monkeypatch.delenv("MOSSWORK_HOME", raising=False)
    assert PathLayout.from_env().root == Path.home() / ".mosswork"


def test_path_layout_from_env_honors_mosswork_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MOSSWORK_HOME", str(tmp_path))
    assert PathLayout.from_env().root == tmp_path
