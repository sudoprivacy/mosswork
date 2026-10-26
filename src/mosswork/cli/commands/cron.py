"""mosswork cron commands."""

from __future__ import annotations

import json as _json
import sys

import click

from mosswork.cli.support.ctx import json_output_enabled, require_agent
from mosswork.cli.support.errors import fail_mosswork
from mosswork.infra.errors import MossworkError


@click.group()
def cron() -> None:
    """Cron job management commands."""


@cron.command("list")
@click.option("--agent", "agent_id", default=None)
@click.option("--user", "as_user", default=None)
def list_jobs(agent_id: str | None, as_user: str | None) -> None:
    """List cron jobs for an agent."""
    from rich.console import Console
    from rich.table import Table

    from mosswork.cli.support.offline_ops import list_cron_offline, resolve_cron_user_id

    aid = require_agent(agent_id)
    try:
        resolve_cron_user_id(aid, as_user)
        rows = list_cron_offline(aid)
    except MossworkError as exc:
        fail_mosswork(exc)
    if json_output_enabled():
        click.echo(_json.dumps(rows, indent=2))
        return
    table = Table(title="Cron jobs")
    for col in ("id", "trigger", "task_type", "enabled"):
        table.add_column(col)
    for row in rows:
        table.add_row(
            row.get("id", ""),
            row.get("trigger", ""),
            row.get("task_type", ""),
            str(bool(row.get("enabled", True))),
        )
    Console(file=sys.stdout).print(table)


@cron.command("create")
@click.option("--agent", "agent_id", default=None)
@click.option("--user", "as_user", default=None)
@click.option("--trigger", required=True)
@click.option("--prompt", required=True)
@click.option("--fresh-thread", is_flag=True, default=False)
@click.option("--task-type", default="agent")
def create_job(
    agent_id: str | None,
    as_user: str | None,
    trigger: str,
    prompt: str,
    fresh_thread: bool,
    task_type: str,
) -> None:
    """Create a cron job."""
    from mosswork.cli.support.offline_ops import create_cron_offline, resolve_cron_user_id

    aid = require_agent(agent_id)
    try:
        uid = resolve_cron_user_id(aid, as_user)
        row = create_cron_offline(
            agent_id=aid,
            user_id=uid,
            trigger=trigger,
            prompt=prompt,
            fresh_thread=fresh_thread,
            task_type=task_type,
        )
    except MossworkError as exc:
        fail_mosswork(exc)
    click.echo(_json.dumps(row, indent=2))
    click.echo("hint: restart `mosswork run` to load new cron jobs into the running server.")


@cron.command("delete")
@click.argument("cron_id")
@click.option("--agent", "agent_id", default=None)
@click.option("--user", "as_user", default=None)
def delete_job(agent_id: str | None, cron_id: str, as_user: str | None) -> None:
    """Delete a cron job."""
    from mosswork.cli.support.offline_ops import delete_cron_offline, resolve_cron_user_id

    aid = require_agent(agent_id)
    try:
        resolve_cron_user_id(aid, as_user)
        delete_cron_offline(aid, cron_id)
    except MossworkError as exc:
        fail_mosswork(exc)
    click.echo("deleted")


@cron.command("run-now")
@click.argument("cron_id")
@click.option("--agent", "agent_id", default=None)
@click.option("--user", "as_user", default=None)
def run_now(agent_id: str | None, cron_id: str, as_user: str | None) -> None:
    """Run a cron job to completion (embedded server)."""
    from mosswork.cli.support.embedded_ops import cron_run_now
    from mosswork.cli.support.offline_ops import resolve_cron_user_id

    aid = require_agent(agent_id)
    try:
        resolve_cron_user_id(aid, as_user)
        cron_run_now(aid, cron_id)
    except MossworkError as exc:
        fail_mosswork(exc)
    except Exception as exc:
        raise click.ClickException(str(exc) or type(exc).__name__) from exc
    click.echo("ok")
