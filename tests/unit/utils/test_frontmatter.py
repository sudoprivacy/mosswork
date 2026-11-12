"""tests/unit/utils/test_frontmatter.py"""

from __future__ import annotations

from datetime import date

import pytest
import yaml

from octop.infra.utils.frontmatter import is_agent_file, parse_frontmatter


def test_frontmatter_at_first_line() -> None:
    meta, body = parse_frontmatter("---\nname: foo\n---\nbody")
    assert meta == {"name": "foo"}
    assert body == "body"
    assert is_agent_file("---\nname: foo\n---\nbody")


def test_leading_multiline_html_comment_is_skipped() -> None:
    text = "<!-- agent-params\n     more -->\n---\nname: foo\n---\nbody"
    meta, body = parse_frontmatter(text)
    assert meta == {"name": "foo"}
    assert body == "body"
    assert is_agent_file(text)


def test_leading_one_line_html_comment_is_skipped() -> None:
    """The documented invariant: leading HTML comments are skipped before the
    first ``---`` fence, including a comment that closes on its own line."""
    text = "<!-- agent-params -->\n---\nname: foo\n---\nbody"
    meta, body = parse_frontmatter(text)
    assert meta == {"name": "foo"}
    assert body == "body"
    assert is_agent_file(text)


def test_leading_blank_lines_are_skipped() -> None:
    text = "\n\n---\nname: foo\n---\nbody"
    meta, body = parse_frontmatter(text)
    assert meta == {"name": "foo"}
    assert body == "body"


def test_no_frontmatter_returns_the_whole_text() -> None:
    text = "# Title\n\nParagraph"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == text
    assert not is_agent_file(text)


@pytest.fixture(params=[False, True], ids=["python-fallback", "compiled-safe"])
def safe_loader_implementation(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.param:
        if not hasattr(yaml, "CSafeLoader"):
            pytest.skip("The installed PyYAML distribution has no LibYAML extension")
    else:
        monkeypatch.delattr(yaml, "CSafeLoader", raising=False)


def test_safe_loaders_preserve_yaml_value_types(safe_loader_implementation: None) -> None:
    text = (
        "---\nenabled: yes\nnumber: 0x10\nday: 2026-11-12\nnested: [中文, null, 3.5]\n---\n# Body\n"
    )
    meta, body = parse_frontmatter(text)
    assert meta == {
        "enabled": True,
        "number": 16,
        "day": date(2026, 11, 12),
        "nested": ["中文", None, 3.5],
    }
    assert body == "# Body\n"


def test_safe_loaders_preserve_aliases_without_sharing_between_calls(
    safe_loader_implementation: None,
) -> None:
    text = (
        "---\nbase: &base {name: original}\nalias: *base\nmerged: {<<: *base, count: 2}\n---\nBody"
    )
    meta, body = parse_frontmatter(text)
    assert meta["base"] is meta["alias"]
    assert meta["merged"] == {"name": "original", "count": 2}
    meta["base"]["name"] = "changed"
    fresh, fresh_body = parse_frontmatter(text)
    assert fresh["base"] == {"name": "original"}
    assert body == fresh_body == "Body"


@pytest.mark.parametrize(
    "raw",
    [
        "name: [unterminated",
        "danger: !!python/object/apply:builtins.str [unexpected]",
        "name: !unregistered value",
        "[one, two]",
        "plain scalar",
    ],
)
def test_safe_loaders_preserve_malformed_or_nonmapping_source(
    safe_loader_implementation: None, raw: str
) -> None:
    text = f"---\n{raw}\n---\nOriginal body\n"
    assert parse_frontmatter(text) == ({}, text)


@pytest.mark.parametrize("raw", ["", "null", "[]", "{}"])
def test_safe_loaders_preserve_existing_empty_metadata_behavior(
    safe_loader_implementation: None, raw: str
) -> None:
    text = f"---\n{raw}\n---\nOriginal body\n"
    assert parse_frontmatter(text) == ({}, "Original body\n")


def test_available_compiled_safe_loader_is_actually_used(monkeypatch: pytest.MonkeyPatch) -> None:
    native_loader = getattr(yaml, "CSafeLoader", None)
    if native_loader is None:
        pytest.skip("The installed PyYAML distribution has no LibYAML extension")
    calls: list[str] = []

    class ObservedSafeLoader(native_loader):
        def __init__(self, stream: str) -> None:
            calls.append(stream)
            super().__init__(stream)

    monkeypatch.setattr(yaml, "CSafeLoader", ObservedSafeLoader)
    assert parse_frontmatter("---\nname: example\n---\nBody") == ({"name": "example"}, "Body")
    assert len(calls) == 1
