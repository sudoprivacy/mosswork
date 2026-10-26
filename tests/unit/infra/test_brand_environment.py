from moss_harness.runtime_env import resolve_local_execute_env


def test_workspace_cannot_override_renamed_host_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("MOSSWORK_HOME", str(tmp_path / "server"))
    monkeypatch.setenv("MOSSWORK_AGENT_ISOLATION", "docker")
    (tmp_path / ".env").write_text(
        "MOSSWORK_HOME=/other\nMOSSWORK_AGENT_ISOLATION=none\nPROJECT_MODE=dev\n"
    )
    env = resolve_local_execute_env(workspace_dir=tmp_path)
    assert env["MOSSWORK_HOME"] == str(tmp_path / "server")
    assert env["MOSSWORK_AGENT_ISOLATION"] == "docker"
    assert env["PROJECT_MODE"] == "dev"
