"""mosswork version command."""

from __future__ import annotations

import click

from octop.branding import SERVER_NAME


@click.command("version")
def version() -> None:
    """Show the installed moss-server version."""
    try:
        from importlib.metadata import version as _v

        v = _v("mosswork")
    except Exception:
        v = "unknown"
    click.echo(f"{SERVER_NAME} v{v}")
