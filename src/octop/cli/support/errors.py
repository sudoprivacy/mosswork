"""CLI error helpers."""

from __future__ import annotations

from typing import NoReturn

import click

from octop.infra.errors import MossworkError


def fail_mosswork(exc: MossworkError) -> NoReturn:
    click.echo(f"error: {exc.message}", err=True)
    raise SystemExit(1) from exc
