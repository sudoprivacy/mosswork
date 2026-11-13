"""Build bundled resource packages for tests and explicit migration tools.

Bundled experts and skills remain part of the code library. They are not seeded
into the deployment resource market; only explicitly imported or build-time
external snapshots are published there.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from octop.infra.agents.experts.catalog import default_library_root
from octop.infra.db.services import SharedServices
from octop.infra.market.packages import MarketManifest, Package, parse_package
from octop.infra.utils.frontmatter import parse_frontmatter
from octop.infra.utils.immutable_files import store_object

_SLUG = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_EXPERT_ROOT_FILES = frozenset(
    {
        "CHANGELOG.md",
        "HEARTBEAT.md",
        "IDENTITY.md",
        "LICENSE",
        "LICENSE.md",
        "LICENSE.txt",
        "PROACTIVE.md",
        "README.md",
        "SOUL.md",
        "TOOLS.md",
    }
)
_SUPPORTING_DIRECTORIES = frozenset({"assets", "examples", "references", "scripts", "tests"})
_PRIVATE_PARTS = frozenset(
    {
        ".env",
        ".git",
        "__pycache__",
        "credentials.json",
        "memory.md",
        "node_modules",
        "user.md",
    }
)


@dataclass(frozen=True)
class DefaultRelease:
    resource_id: str
    slug: str
    kind: str
    package: Package


def _label(value: Any, locale: str, fallback: str) -> str:
    if isinstance(value, dict):
        return str(value.get(locale) or value.get("zh") or value.get("en") or fallback).strip()
    return str(value or fallback).strip()


def _safe_supporting_path(path: Path, root: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    rel = path.relative_to(root).as_posix()
    parts = Path(rel).parts
    if not parts or parts[0] not in _SUPPORTING_DIRECTORIES:
        return None
    if any(part.startswith(".") or part.startswith("_") for part in parts):
        return None
    if any(part.casefold() in _PRIVATE_PARTS for part in parts):
        return None
    return rel


def _read_text(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _normalise_skill_text(data: bytes, slug: str) -> tuple[dict[str, Any], bytes] | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    metadata, body = parse_frontmatter(text)
    description = str(metadata.get("description") or "").strip()
    if not body.strip() or not description:
        return None
    # A few bundled legacy skills used a display name in frontmatter. The
    # market contract uses a machine slug, so normalize only that one field.
    name = str(metadata.get("name") or slug).strip()
    if not _SLUG.fullmatch(name):
        name = slug
        metadata = {**metadata, "name": name}
        lines = text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.strip().startswith("name:"):
                ending = "\n" if line.endswith("\n") else ""
                lines[index] = f"name: {slug}{ending}"
                text = "".join(lines)
                break
    if not _SLUG.fullmatch(name):
        return None
    return metadata, text.encode("utf-8")


def _skill_release(skill_dir: Path) -> DefaultRelease | None:
    source = skill_dir / "SKILL.md"
    raw = _read_text(source)
    if raw is None:
        return None
    slug = skill_dir.name
    normalized = _normalise_skill_text(raw, slug)
    if normalized is None:
        return None
    metadata, skill_text = normalized
    manifest_slug = str(metadata.get("name") or slug).strip()
    if not _SLUG.fullmatch(manifest_slug):
        return None
    files: dict[str, bytes] = {"SKILL.md": skill_text}
    for path in sorted(skill_dir.rglob("*")):
        relative = _safe_supporting_path(path, skill_dir)
        if relative is None or relative == "SKILL.md":
            continue
        content = _read_text(path)
        if content is not None:
            files[relative] = content
    description = str(metadata.get("description") or f"Bundled skill: {manifest_slug}").strip()
    market: dict[str, Any] = {
        "format": "mosswork.market.v1",
        "kind": "skill",
        "slug": manifest_slug,
        "name": manifest_slug,
        "description": description[:4000],
        "version": "1.0.0",
        "changelog": "Bundled with mosswork.",
        "resource_id": None,
        "dependencies": [],
        "runtime": {"execution_targets": ["server"], "platforms": ["any"]},
        "configuration": {},
        "required_capabilities": [],
        "network": {"scope": "none"},
        "license": "See bundled source license",
        "author": "mosswork bundled",
    }
    package = parse_package(
        Package(
            manifest=MarketManifest.model_validate(market),
            files={"market.json": json.dumps(market, ensure_ascii=False).encode(), **files},
        ).archive()
    )
    return DefaultRelease(f"builtin-skill-{manifest_slug}", manifest_slug, "skill", package)


def _expert_release(
    expert_dir: Path,
    skill_ids: dict[str, str],
    slug_override: str | None = None,
) -> DefaultRelease | None:
    try:
        source_manifest = json.loads((expert_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    slug = slug_override or str(source_manifest.get("id") or expert_dir.name).strip()
    soul = _read_text(expert_dir / "SOUL.md")
    if not _SLUG.fullmatch(slug) or soul is None or not soul.strip():
        return None
    files: dict[str, bytes] = {"SOUL.md": soul}
    prompt_files = ["SOUL.md"]
    for name in sorted(_EXPERT_ROOT_FILES - {"SOUL.md"}):
        content = _read_text(expert_dir / name)
        if content is not None:
            files[name] = content
            if name.endswith(".md"):
                prompt_files.append(name)
    dependencies: list[dict[str, str]] = []
    skills_root = expert_dir / "skills"
    skill_dirs = sorted(skills_root.iterdir()) if skills_root.is_dir() else []
    for skill_dir in skill_dirs:
        skill_source = skill_dir / "SKILL.md"
        if not skill_source.is_file():
            continue
        try:
            metadata, _ = parse_frontmatter(skill_source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        skill_slug = str(metadata.get("name") or skill_dir.name).strip()
        if not _SLUG.fullmatch(skill_slug):
            skill_slug = skill_dir.name
        resource_id = skill_ids.get(skill_slug)
        if resource_id:
            dependencies.append({"resource_id": resource_id, "version": "1.0.0"})
    display = source_manifest.get("label")
    description = source_manifest.get("description")
    market: dict[str, Any] = {
        "format": "mosswork.market.v1",
        "kind": "expert",
        "slug": slug,
        "name": _label(display, "zh", slug),
        "description": _label(description, "zh", f"Bundled expert: {slug}")[:4000],
        "version": "1.0.0",
        "changelog": "Bundled with mosswork.",
        "resource_id": None,
        "dependencies": dependencies,
        "runtime": {"execution_targets": ["server"], "platforms": ["any"]},
        "configuration": {},
        "required_capabilities": [],
        "network": {"scope": "none"},
        "license": "See bundled source license",
        "author": "mosswork bundled",
    }
    manifest = {
        "id": slug,
        "label": display or {"zh": slug, "en": slug},
        "description": description or {"zh": slug, "en": slug},
        "prompt_files": prompt_files,
    }
    for key in ("welcome_message", "quick_prompts", "task_examples", "icon_name", "color"):
        if key in source_manifest:
            manifest[key] = source_manifest[key]
    package = parse_package(
        Package(
            manifest=MarketManifest.model_validate(market),
            files={
                "market.json": json.dumps(market, ensure_ascii=False).encode(),
                "manifest.json": json.dumps(manifest, ensure_ascii=False).encode(),
                **files,
            },
        ).archive()
    )
    return DefaultRelease(f"builtin-expert-{slug}", slug, "expert", package)


def build_bundled_releases(library_root: Path | None = None) -> list[DefaultRelease]:
    root = library_root or default_library_root()
    skills: dict[str, DefaultRelease] = {}
    expert_dirs = [path for path in sorted(root.iterdir()) if path.is_dir()]
    for expert_dir in expert_dirs:
        skills_root = expert_dir / "skills"
        skill_dirs = sorted(skills_root.iterdir()) if skills_root.is_dir() else []
        for skill_dir in skill_dirs:
            release = _skill_release(skill_dir)
            if release is not None:
                skills.setdefault(release.slug, release)
    skill_ids = {slug: release.resource_id for slug, release in skills.items()}
    experts = [
        release
        for expert_dir in expert_dirs
        if expert_dir.name != "default"
        for source_slug in [
            str(
                json.loads((expert_dir / "manifest.json").read_text(encoding="utf-8")).get(
                    "id", expert_dir.name
                )
            ).strip()
        ]
        for expert_slug in [source_slug if source_slug not in skill_ids else f"{source_slug}-expert"]
        for release in [_expert_release(expert_dir, skill_ids, expert_slug)]
        if release is not None
    ]
    return [*skills.values(), *experts]


def seed_bundled_market(services: SharedServices, library_root: Path | None = None) -> int:
    """Publish bundled releases and return the number newly inserted."""
    objects = services.paths.root / "resource-market" / "objects"
    seeded = 0
    for release in build_bundled_releases(library_root):
        payload = release.package.archive()
        digest = store_object(objects, payload)
        manifest = release.package.manifest.model_dump_json()
        inserted = services.resource_market_repo.seed_release(
            resource_id=release.resource_id,
            slug=release.slug,
            kind=release.kind,
            name=release.package.manifest.name,
            description=release.package.manifest.description,
            version_id=f"{release.resource_id}-v1",
            draft_id=f"{release.resource_id}-draft-1",
            version=release.package.manifest.version,
            digest=digest,
            manifest=manifest,
            inventory=json.dumps(release.package.inventory()),
            file_count=len(release.package.files),
            total_bytes=sum(len(data) for data in release.package.files.values()),
        )
        seeded += int(inserted)
    return seeded
