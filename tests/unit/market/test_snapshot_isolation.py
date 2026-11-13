"""Exercise both dataset scopes through the real shared pytest fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from octop.infra.market import skillhub_defaults


def test_unrelated_tests_use_the_supported_absent_snapshot_path(repo_root: Path) -> None:
    bundled = repo_root / "src/octop/infra/market/skillhub_snapshot"
    assert bundled != skillhub_defaults.SNAPSHOT_ROOT
    assert skillhub_defaults.build_skillhub_releases() == []
    assert not (skillhub_defaults.SNAPSHOT_ROOT / "index.json").exists()
    assert (bundled / "index.json").is_file()


@pytest.mark.usefixtures("full_skillhub_snapshot")
def test_full_snapshot_opt_in_retains_the_actual_bundled_index(repo_root: Path) -> None:
    bundled = repo_root / "src/octop/infra/market/skillhub_snapshot"
    assert bundled == skillhub_defaults.SNAPSHOT_ROOT
    index = json.loads((bundled / "index.json").read_text(encoding="utf-8"))
    assert index["format"] == "mosswork.skillhub.snapshot.v1"
    assert sum(record.get("status") == "downloaded" for record in index["skills"]) >= 300
    assert sum(record.get("status") == "downloaded" for record in index["skillsets"]) >= 48


def test_an_explicit_snapshot_remains_available_to_unit_fixtures(tmp_path: Path) -> None:
    (tmp_path / "index.json").write_text(
        json.dumps({"format": "mosswork.skillhub.snapshot.v1", "skills": [], "skillsets": []}),
        encoding="utf-8",
    )
    assert skillhub_defaults.build_skillhub_releases(tmp_path) == []
