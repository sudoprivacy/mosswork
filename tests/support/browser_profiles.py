"""Owned temporary browser profile roots with real lease teardown."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from octop.infra.browser.control import close_profile_controls
from octop.infra.utils.paths import PathLayout


@pytest.fixture
async def browser_paths(tmp_path: Path) -> AsyncIterator[PathLayout]:
    paths = PathLayout(tmp_path)
    try:
        yield paths
    finally:
        await close_profile_controls(paths.root)
