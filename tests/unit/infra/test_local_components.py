"""Distribution contracts and persistent state across local component restarts."""

from importlib import import_module, util
from importlib.metadata import version
from pathlib import Path

import pytest
from moss_browser.settings import MossBrowserSettings, state_dir
from moss_memory import Memory


@pytest.mark.parametrize("component", ["harness", "memory", "browser", "gateway"])
def test_runtime_uses_local_component_and_has_no_upstream_replacement(component):
    module = import_module(f"moss_{component}")
    assert version(f"moss-{component}") == version("mosswork")
    assert util.find_spec(f"octop_{component}") is None
    assert (
        Path(module.__file__)
        .resolve()
        .is_relative_to(Path(__file__).resolve().parents[3] / "packages" / f"moss-{component}")
    )


def test_memory_reopens_existing_namespace_without_losing_facts(tmp_path):
    config = {"db_path": str(tmp_path / "memory.sqlite")}
    memory = Memory("agent_existing", backend="sqlite", backend_config=config)
    try:
        node = memory.store("用户喜欢用中文沟通", topic="language")
    finally:
        memory.backend.close()
    reopened = Memory("agent_existing", backend="sqlite", backend_config=config)
    try:
        assert reopened.get(node.id).content == "用户喜欢用中文沟通"
        assert reopened.recall("中文")[0].id == node.id
    finally:
        reopened.backend.close()


@pytest.mark.parametrize("legacy_name", [".octop-browser", ".harness-browser"])
def test_browser_keeps_existing_login_profile_directory(tmp_path, monkeypatch, legacy_name):
    legacy = tmp_path / legacy_name
    (legacy / "profiles").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("BROWSER_USE_PROFILES_DIR", raising=False)
    assert state_dir() == legacy
    assert MossBrowserSettings().profiles_dir == legacy / "profiles"
    (tmp_path / ".moss-browser").mkdir()
    assert state_dir() == tmp_path / ".moss-browser"
