"""The engine is updated with the mosswork distribution."""

from __future__ import annotations

import click


@click.command()
@click.option("--pre", is_flag=True, default=False, help="Reserved for release compatibility.")
def update(pre: bool) -> None:
    """Show where to obtain moss-harness updates."""
    raise click.ClickException(
        "moss-harness is maintained in the mosswork repository. "
        "Upgrade mosswork from https://github.com/sudoprivacy/mosswork/releases "
        "or update the source checkout and run uv sync."
    )
