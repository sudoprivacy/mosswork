"""Path constants and resolution for CLI configuration files."""

from __future__ import annotations

from pathlib import Path

_MOSS_HARNESS_DIR_NAME = ".moss-harness"
_CONFIG_FILENAME = "config.json"
_CREDENTIALS_FILENAME = "credentials.json"
_SESSIONS_DIR_NAME = "sessions"
_WORKSPACE_DIR_NAME = "workspace"
_ENV_FILENAME = ".env"


class CliPaths:
    """Resolved paths for global and project-level config.

    The CLI's primary home is **global** (``~/.moss-harness/``) — that's
    where ``moss-harness init`` lays down config + workspace + sessions
    by default. A project-level ``<cwd>/.moss-harness/`` can still
    override fields in the global ``config.json`` (project wins on
    conflict, see ``loader.load_config``).
    """

    def __init__(self, project_dir: Path) -> None:
        self._project_root = project_dir

    # ------------------------------------------------------------------
    # Global (``~/.moss-harness``)
    # ------------------------------------------------------------------

    @property
    def global_dir(self) -> Path:
        """``~/.moss-harness/`` — user-level Harness home."""
        return Path.home() / _MOSS_HARNESS_DIR_NAME

    @property
    def global_config_file(self) -> Path:
        """``~/.moss-harness/config.json``"""
        return self.global_dir / _CONFIG_FILENAME

    @property
    def credentials_file(self) -> Path:
        """``~/.moss-harness/credentials.json``"""
        return self.global_dir / _CREDENTIALS_FILENAME

    @property
    def global_workspace_dir(self) -> Path:
        """``~/.moss-harness/workspace/`` — agent's default workspace.

        Holds the bundled ``_builtin_skills/`` and seeded markdown
        templates (``AGENTS.md``, ``MEMORY.md``, …). Equivalent to
        ``HarnessAgentConfig.workspace_dir`` for default installs.
        """
        return self.global_dir / _WORKSPACE_DIR_NAME

    @property
    def global_sessions_dir(self) -> Path:
        """``~/.moss-harness/sessions/`` — CLI session metadata."""
        return self.global_dir / _SESSIONS_DIR_NAME

    @property
    def global_env_file(self) -> Path:
        """``~/.moss-harness/.env``"""
        return self.global_dir / _ENV_FILENAME

    # ------------------------------------------------------------------
    # Project (``<cwd>/.moss-harness``)
    # ------------------------------------------------------------------

    @property
    def project_dir(self) -> Path:
        """``<project>/.moss-harness/`` — optional project-level overrides."""
        return self._project_root / _MOSS_HARNESS_DIR_NAME

    @property
    def project_config_file(self) -> Path:
        """``<project>/.moss-harness/config.json``"""
        return self.project_dir / _CONFIG_FILENAME

    @property
    def sessions_dir(self) -> Path:
        """Active sessions directory.

        Project-level if ``<project>/.moss-harness/sessions/`` exists,
        otherwise the global ``~/.moss-harness/sessions/``. Lets users
        keep a per-project history without having to set anything up
        themselves.
        """
        project_sessions = self.project_dir / _SESSIONS_DIR_NAME
        if project_sessions.is_dir():
            return project_sessions
        return self.global_sessions_dir

    @property
    def project_env_file(self) -> Path:
        """``<project_dir>/.env``"""
        return self._project_root / _ENV_FILENAME
