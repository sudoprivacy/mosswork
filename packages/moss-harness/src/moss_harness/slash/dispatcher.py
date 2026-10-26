"""Backward-compat re-export — prefer ``from moss_harness.slash import ...``."""

from moss_harness.slash.core import (
    RuntimeSlashDispatcher,
    build_runtime_dispatcher,
)

__all__ = ["RuntimeSlashDispatcher", "build_runtime_dispatcher"]
