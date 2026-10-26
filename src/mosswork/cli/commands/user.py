"""mosswork user commands."""

from __future__ import annotations

import sys

import click

from mosswork.cli.support.errors import fail_mosswork
from mosswork.infra.errors import MossworkError


@click.group()
def user() -> None:
    """User management commands (local DB)."""


@user.command("create")
@click.argument("username")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=False)
@click.option("--role", default="user")
@click.option("--display-name", default=None)
@click.option("--email", default=None, help="Optional email (also usable for login).")
def create(
    username: str, password: str, role: str, display_name: str | None, email: str | None
) -> None:
    """Create a new user."""
    from mosswork.cli.support.offline_ops import create_user_offline

    try:
        click.echo(
            create_user_offline(
                username=username,
                password=password,
                role=role,
                display_name=display_name,
                email=email,
            )
        )
    except MossworkError as exc:
        fail_mosswork(exc)


@user.command("list")
def list_users() -> None:
    """List all users."""
    from rich.console import Console
    from rich.table import Table

    from mosswork.cli.support.ctx import json_output_enabled
    from mosswork.cli.support.offline_ops import list_users_offline

    rows = list_users_offline()
    if json_output_enabled():
        import json as _json

        click.echo(_json.dumps(rows, indent=2))
        return
    table = Table(title="Users")
    table.add_column("id")
    table.add_column("username")
    table.add_column("email")
    table.add_column("role")
    table.add_column("disabled")
    for u in rows:
        table.add_row(
            str(u["id"]),
            u["username"],
            u.get("email") or "-",
            u["role"],
            str(bool(u.get("disabled"))),
        )
    Console(file=sys.stdout).print(table)


@user.command("set-email")
@click.argument("username")
@click.argument("email", required=False, default=None)
@click.option("--clear", is_flag=True, default=False, help="Remove the user's email.")
def set_email(username: str, email: str | None, clear: bool) -> None:
    """Set or clear a user's email."""
    from mosswork.cli.support.offline_ops import set_user_email_offline

    if clear:
        email = None
    elif email is None:
        raise click.UsageError("provide EMAIL or --clear")
    try:
        click.echo(set_user_email_offline(username, email))
    except MossworkError as exc:
        fail_mosswork(exc)


@user.command("passwd")
@click.argument("username")
@click.option("--password", prompt=True, hide_input=True)
def passwd(username: str, password: str) -> None:
    """Reset a user's password."""
    from mosswork.cli.support.offline_ops import set_user_password_offline

    try:
        set_user_password_offline(username, password)
    except MossworkError as exc:
        fail_mosswork(exc)
    click.echo("ok")


@user.command("role")
@click.argument("username")
@click.argument("role")
def set_role(username: str, role: str) -> None:
    """Set a user's role."""
    from mosswork.cli.support.offline_ops import set_user_role_offline

    try:
        set_user_role_offline(username, role)
    except MossworkError as exc:
        fail_mosswork(exc)
    click.echo("ok")


@user.command("disable")
@click.argument("username")
def disable(username: str) -> None:
    """Disable a user."""
    from mosswork.cli.support.offline_ops import disable_user_offline

    try:
        disable_user_offline(username)
    except MossworkError as exc:
        fail_mosswork(exc)
    click.echo("ok")


@user.command("delete")
@click.argument("username")
@click.option("--yes", is_flag=True, default=False, help="skip confirmation")
def delete(username: str, yes: bool) -> None:
    """Delete a user (irreversible)."""
    from mosswork.cli.support.offline_ops import delete_user_offline

    if not yes:
        click.confirm(f"Really delete user {username}?", abort=True)
    try:
        delete_user_offline(username)
    except MossworkError as exc:
        fail_mosswork(exc)
    click.echo("deleted")
