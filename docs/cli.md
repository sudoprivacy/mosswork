# CLI Reference

This distribution supports source deployment and local builds only; follow the
[enterprise deployment guide](../docker/enterprise/README_EN.md). The upstream
`pip install mosswork` package is not an installation method for mosswork.
The source build registers a single Click app named `mosswork`.
It is registered in `src/mosswork/cli/main.py`; commands are loaded
lazily from `src/mosswork/cli/registry.py`.

```
$ mosswork --help
Usage: mosswork [OPTIONS] COMMAND [ARGS]...

  mosswork command-line interface.

Options:
  -v, --version           Show the installed mosswork version.
  --user TEXT             Default --user for subcommands (admin acting on behalf
                          of a user). [env: MOSSWORK_USER]
  --agent TEXT            Default --agent for subcommands. [env: MOSSWORK_AGENT]
  --json                  Emit machine-readable JSON for list-style commands.
  -h, --help              Show this message and exit.

Commands:
  acp        Run mosswork agent as ACP server (stdio).
  admin      Admin commands.
  agent      Agent lifecycle commands.
  backup     Export and restore mosswork backups.
  chat       [deprecated] Alias for `mosswork chats`.
  chats      Chat REPL and session management.
  channel    Channel management commands.
  clean      Remove CLI state or wipe all of ~/.mosswork.
  completion Shell completion utilities.
  config     CLI state (base URL, defaults).
  cron       Cron job management commands.
  init       Bootstrap an mosswork server install.
  models     Model catalog and active-model settings.
  plugin     Install and manage plugins.
  provider   Provider management (admin write).
  run        Run mosswork-server in the foreground.
  service    System service lifecycle (start/stop/restart/status).
  skills     Per-agent skill enable/disable.
  update     Disabled for mosswork; use manual source upgrades.
  user       User management commands.
  version    Show the installed mosswork version.
```

> **Tip.** Regenerate the per-subcommand listings below with
> `make docs-cli` (each `mosswork <cmd> --help` is captured to stdout).

## Global options

| Option | Env | Effect |
|--------|-----|--------|
| `--user NAME` | `MOSSWORK_USER` | Default `--user` for subcommands (admin acting on behalf of a user) |
| `--agent ID` | `MOSSWORK_AGENT` | Default `--agent` for subcommands |
| `--json` | — | Emit machine-readable JSON for list-style commands |
| `-v, --version` | — | Print version and exit |

## Transport layers

mosswork commands pick one of three transports:

| Layer | When | Login? | Examples |
|-------|------|--------|----------|
| **Offline** (local DB only) | Need to read/write `~/.mosswork` without a running server | No | `init`, `backup`, `plugin`, `agent list`, `chats list/get/create/update/delete`, `cron list`, `user *`, `admin overview/audit`, `models presets/list/active` |
| **Attach** (HTTP / WS) | Need a live `mosswork run` process (IM, streams, model pulls) | Yes (`mosswork user login`) | `chats send/repl`, `channel test/probe`, `models ollama-*`, `skills enable/disable`, `provider test` |
| **Embedded** (in-process) | CLI boots `MossworkServer` for a single command | No | `mosswork acp`, `mosswork chats repl`, `mosswork chats send` (defaults to embedded), `mosswork agent create/from-expert/start/stop/reload` |

The dashboards and HTTP callers manage their own JWTs and do **not**
share `~/.mosswork/cli_state.json`.

## `mosswork init`

Bootstrap a fresh install (DB migrations, JWT secret, first admin).
Idempotent on the DB; pass `--force` to wipe `~/.mosswork` first.

```
Usage: mosswork init [OPTIONS]

  Bootstrap an mosswork server (~/.mosswork dir, DB migrations, JWT secret, first admin).

Options:
  --admin-username TEXT       [env: MOSSWORK_ADMIN_USERNAME]
  --admin-password TEXT       [env: MOSSWORK_ADMIN_PASSWORD]
  --admin-display-name TEXT   [env: MOSSWORK_ADMIN_DISPLAY_NAME]
  --force                     Wipe existing ~/.mosswork contents before bootstrapping.
  --yes                       Skip all interactive prompts.
  -h, --help                  Show this message and exit.
```

## `mosswork run`

Start the FastAPI app in the foreground (delegates to
`mosswork.launch.run_foreground_blocking`).

```
Usage: mosswork run [OPTIONS]

  Start the mosswork server (foreground; uvicorn).

Options:
  --host TEXT                 Override MOSSWORK_BIND_HOST.
  --port INTEGER RANGE        Override MOSSWORK_PORT (0-65535, 0 = OS-assigned).
  --reload / --no-reload      Enable uvicorn auto-reload (dev only).
  --ssl / --no-ssl            Enable HTTPS with a self-signed cert (or a real one).
  --certfile PATH             TLS certificate (PEM).
  --keyfile PATH              TLS private key (PEM).
  --log-level [debug|info|warning|error]
  -h, --help                  Show this message and exit.
```

## `mosswork service`

Install and manage the mosswork system service. The backend is detected
automatically (systemd on Linux, launchd on macOS); the scope
(`user` vs `system`) can be forced via `--scope` or
`MOSSWORK_SERVICE_SCOPE`.

```
Usage: mosswork service [OPTIONS] COMMAND [ARGS]...

  Manage the mosswork system service (systemd on Linux, launchd on macOS).

Commands:
  start     Install (if missing) and start the service.
  stop      Stop the service.
  restart   Restart the service.
  status    Print whether the service is installed / running / healthy.
```

`status` probes the HTTP health endpoint and prints journal/log hints
on failure.

On Linux, `start` and `restart` install a systemd drop-in
(`mosswork.service.d/10-nofile.conf`) with `LimitNOFILE=65535` (user
units are capped to the process hard rlimit so systemd cannot fail to
spawn). The main unit file is not rewritten unless it is missing or
you pass `--force-install`. `restart` always runs `systemctl restart`
even if writing the drop-in fails, so an upgrade that only restarts
still comes back. To undo: delete the drop-in, then
`systemctl daemon-reload && systemctl restart mosswork`.

## `mosswork user`

Local-DB user management. `login` requires a running server; the
rest are offline.

```
Usage: mosswork user [OPTIONS] COMMAND [ARGS]...

  User management commands (local DB; no server login required).

Commands:
  create    Create a new user.
  list      List all users.
  passwd    Reset a user's password (admin).
  role      Set a user's role.
  disable   Disable a user.
  delete    Delete a user.
  login     Login against a running server; stores JWT for optional remote HTTP attach.
```

## `mosswork agent`

Lifecycle and templates. Most commands boot an embedded
`MossworkServer` to operate on the local DB without a remote round-trip.

```
Usage: mosswork agent [OPTIONS] COMMAND [ARGS]...

  Agent lifecycle commands.

Commands:
  create        Create a new agent.
  from-expert   Create an agent from a bundled expert template.
  list          List agents.
  use           Pin this agent as the default for subsequent commands.
  start         Start the agent runtime.
  stop          Stop the agent runtime.
  reload        Reload the agent runtime.
  delete        Delete the agent row (and runtime).
  experts       List bundled expert templates.
```

`create` and `from-expert` accept `--user` (admin only) to create
agents on behalf of another user. `use` writes the agent id into
`~/.mosswork/cli_state.json` so subsequent commands default to it.

## `mosswork chats`

Thread CRUD + interactive REPL. The REPL and `send` use an embedded
server (no separate `mosswork run` needed); `list` / `get` / `create` /
`update` / `delete` work fully offline against `~/.mosswork/mosswork.db`.

```
Usage: mosswork chats [OPTIONS] COMMAND [ARGS]...

  Thread list/history and interactive chat (CLI channel / local DB).

Commands:
  list        List conversation threads for an agent.
  get         Show message history for a thread.
  create      Start a new thread (/new equivalent).
  update      Rename or pin a thread.
  delete      Delete a thread.
  send        Send one message and stream the response.
  repl        Interactive chat REPL (embedded server + CLI gateway channel).
```

`mosswork chat` is a deprecated alias that prints a stderr warning and
forwards to `chats`.

## `mosswork channel`

Local DB channel CRUD plus platform-specific bot creators. The
`wecom` / `weixin` / `feishu-setup` subcommands drive the QR-code
bot-creator flows; `config` is the offline config editor.

```
Usage: mosswork channel [OPTIONS] COMMAND [ARGS]...

  Channel management commands.

Commands:
  list          List channels for an agent.
  get           Show one channel row.
  create        Create a channel.
  patch         Update a channel.
  delete        Delete a channel.
  test          Test a channel (instantiate → start → stop).
  config        Edit channel config offline.
  feishu-setup  Drive the Feishu bot-creator flow.
  bind          Bind-group helper for WeCom / Weixin (QR login, etc.).
```

## `mosswork cron`

Local DB cron management. `run-now` requires a running `mosswork run`
because the actual fire is dispatched through the live
`CronManager`.

```
Usage: mosswork cron [OPTIONS] COMMAND [ARGS]...

  Cron job management commands.

Commands:
  list        List cron jobs for an agent.
  create      Create a cron job.
  delete      Delete a cron job.
  run-now     Trigger a cron job immediately.
```

`create --trigger` accepts cron expressions (`"0 9 * * *"`) and the
`interval:N` / `date:ISO8601` aliases. `--task-type text` pushes the
prompt verbatim; `--task-type agent` (default) runs the LLM and
pushes the reply. `--prompt` is required, must be non-empty and ≤
2000 characters.

## `mosswork provider`

Local DB provider CRUD. `test` requires a running server.

```
Usage: mosswork provider [OPTIONS] COMMAND [ARGS]...

  Provider management commands (local DB).

Commands:
  list      List providers.
  create    Create a provider.
  delete    Delete a provider.
  test      Ping the provider (requires running server).
```

## `mosswork models`

Provider presets and active model management.

```
Usage: mosswork models [OPTIONS] COMMAND [ARGS]...

  Model catalog and active-model settings.

Commands:
  presets       List built-in provider templates from moss-harness.
  list          List all resolved models across enabled providers.
  active        Show or set the global default model (admin).
  config        Interactively create a provider from presets and set the active model.
  ollama-list   List local Ollama models (requires a running server).
  ollama-pull   Pull a model via Ollama (requires a running server).
  ollama-rm     Remove a local Ollama model (requires a running server).
```

## `mosswork skills`

Per-agent skill enable / disable. All subcommands need a running
server (the dashboard's Skill Hub and bundled `~/.mosswork/skills/`
library are queried at boot).

```
Usage: mosswork skills [OPTIONS] COMMAND [ARGS]...

  Manage agent skills (enable / disable / list).

Commands:
  list      List skills for an agent.
  enable    Enable a skill for the agent.
  disable   Disable a skill for the agent.
  config    Show / edit the per-agent skills config.
```

## `mosswork admin`

Local DB admin operations. `rotate-jwt-secret` works without a
running server (it edits the SQLite secret row directly).

```
Usage: mosswork admin [OPTIONS] COMMAND [ARGS]...

  Admin commands (local DB).

Commands:
  overview             Show admin overview (user count, agent state distribution).
  audit                Show audit log entries.
  providers            Global (admin) providers.
  rotate-jwt-secret    Rotate the JWT secret directly via the local DB.
```

## `mosswork captcha`

Login captcha maintenance against the local database. Works fully offline
(edits the SQLite settings row directly) — the escape hatch for when a
misconfigured captcha provider (wrong keys, unreachable vendor, hostname
not allowlisted) locks everyone out of the dashboard, since the settings
UI itself requires login to reach.

```
Usage: mosswork captcha [OPTIONS] COMMAND [ARGS]...

  Login captcha maintenance against the local database.

Commands:
  reset    Clear stored captcha settings so login falls back to the slider default.
```

Boot-env captcha (`MOSSWORK_CAPTCHA_*`) is not touched by `reset` — unset
those in the environment file if they are the actual lockout cause.
Restart `mosswork run` (if already running) for the change to apply.

## `mosswork backup`

Export and restore mosswork backups. Works fully offline.

```
Usage: mosswork backup [OPTIONS] COMMAND [ARGS]...

  Backup and restore database + local agent workspaces.

Commands:
  create     Create a backup archive (DB + workspaces + config).
  restore    Restore a backup archive into ~/.mosswork.
  auto       Automatic backup status and one-shot run.
```

### `mosswork backup auto`

Automatic backups are scheduled **inside** a running `mosswork run` process
(via the same system-job mechanism as TLS renewal). Configure in
`config.json` under `backup`, with env overrides
`MOSSWORK_BACKUP_AUTO_ENABLED`, `MOSSWORK_BACKUP_SCHEDULE`,
`MOSSWORK_BACKUP_RETENTION_COUNT`, or via the dashboard Backup settings.

```
Usage: mosswork backup auto [OPTIONS] COMMAND [ARGS]...

Commands:
  status  Print automatic backup settings from config.json.
  run     Create one automatic backup now and prune by retention.
```

Default schedule when enabled: `cron:0 4 * * *` (daily 04:00, server
timezone). Automatic archives are named `mosswork-auto-backup-*.tar.gz`;
retention only deletes those files and never touches manual
`mosswork-backup-*.tar.gz` archives.

## `mosswork update` (disabled)

Online self-update is disabled for mosswork. Both `mosswork update` and
`mosswork update --check` stop without checking or installing upstream packages.
Use the [manual upgrade procedure](updates_EN.md) to build this repository’s
source and distribute matching clients.

## `mosswork plugin`

Install and manage third-party plugins (drop into `~/.mosswork/plugins/`).
The same plugin manager powers the dashboard's "Plugins" page.

mosswork also **seeds bundled plugins** into `~/.mosswork/plugins/` during
`mosswork init` and every `mosswork run` start. They are listed in Admin →
Plugins but stay **globally disabled** until you turn them on. An id
already recorded in `config.json` → `bundled_plugins_seeded` is not
copied again after uninstall.

```
Usage: mosswork plugin [OPTIONS] COMMAND [ARGS]...

  Install and manage plugins.

Commands:
  list       List installed plugins.
  install    Install a plugin (local path or git URL).
  uninstall  Remove an installed plugin.
  reload     Reload all installed plugins without restart.
```

## `mosswork acp`

Expose an mosswork agent as a stdio JSON-RPC ACP server. Boots a
standalone `MossworkServer` (reads `~/.mosswork`); does **not** require
`mosswork run` to be running.

```
Usage: mosswork acp [OPTIONS]

  Run mosswork agent as ACP server (stdio).

Options:
  --agent TEXT   Agent to expose (default: MOSSWORK_AGENT or first agent).
  --debug        Log to stderr.
  -h, --help     Show this message and exit.
```

See [ACP integration](./acp.md) for the Zed setup example and the
runner object schema.

## `mosswork clean`

Reset CLI state or wipe the whole `~/.mosswork` tree. Destructive — read
the help carefully.

```
Usage: mosswork clean [OPTIONS]

  Remove CLI state or wipe all of ~/.mosswork.

Options:
  --state / --all       What to remove.
  --yes                 Skip the confirmation prompt.
  -h, --help            Show this message and exit.
```

## `mosswork config`

Inspect / edit `~/.mosswork/cli_state.json` (default base URL, user,
agent).

```
Usage: mosswork config [OPTIONS] COMMAND [ARGS]...

  CLI state (base URL, defaults).

Commands:
  show       Print the current CLI state.
  set-user   Pin the default --user.
```

## `mosswork completion` / `mosswork version`

```text
$ mosswork completion show bash   # dump a shell snippet
$ mosswork completion install     # append to ~/.zshrc / ~/.bashrc
$ mosswork version                # print the installed mosswork version
```

## CLI state file

`~/.mosswork/cli_state.json` stores:

```json
{
  "base_url": "http://127.0.0.1:8088",
  "token": null,
  "default_user": null,
  "default_agent": null
}
```

The path is exposed as `mosswork.cli.support.state.default_state_path()`
and can be overridden via `MOSSWORK_HOME`. Delete the file to log out
the CLI without hitting the server. The dashboard and HTTP callers
do not share this file — they manage their own tokens.
