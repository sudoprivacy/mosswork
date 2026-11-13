#!/usr/bin/env python3
"""Fetch a reproducible, offline SkillHub catalog snapshot.

The generated directory is consumed by ``octop.infra.market.skillhub_defaults``
at server startup.  It contains no runtime network dependency: metadata,
normalized local-market ZIPs, and downloaded expert portraits are all stored
under ``src/octop/infra/market/skillhub_snapshot``.

This is a build-time importer.  Run it only after checking that the upstream
resources and portraits may be redistributed with the product.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_API = "https://api.skillhub.cn"
DEFAULT_OUTPUT = Path("src/octop/infra/market/skillhub_snapshot")
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_ICON_BYTES = 5 * 1024 * 1024
SKILL_SEARCH_LIMIT = 100
SKILL_SEARCH_TERMS = tuple("abcdefghijklmnopqrstuvwxyz0123456789")
SKILL_RANKING_ENDPOINTS = (
    "hot",
    "featured",
    "newest",
    "recommended",
    "trending",
)
SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_DISPLAY_ACRONYMS = {
    "ai": "AI",
    "api": "API",
    "cli": "CLI",
    "cn": "CN",
    "csv": "CSV",
    "ddd": "DDD",
    "docx": "DOCX",
    "gpt": "GPT",
    "http": "HTTP",
    "https": "HTTPS",
    "html": "HTML",
    "json": "JSON",
    "llm": "LLM",
    "mcp": "MCP",
    "pdf": "PDF",
    "ppt": "PPT",
    "sdk": "SDK",
    "sql": "SQL",
    "ssh": "SSH",
    "ui": "UI",
    "url": "URL",
    "x": "X",
    "xlsx": "XLSX",
    "xml": "XML",
}


def _humanize_slug(value: str) -> str:
    """Turn a machine slug into a readable fallback when upstream has no title."""
    tokens = [token for token in re.split(r"[-_]+", value.strip()) if token]
    result: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.isdigit() and index + 1 < len(tokens) and tokens[index + 1].isdigit():
            result.append(f"{token}.{tokens[index + 1]}")
            index += 2
            continue
        lower = token.casefold()
        result.append(_DISPLAY_ACRONYMS.get(lower, token[:1].upper() + token[1:]))
        index += 1
    return " ".join(result) or value


def _display_name(candidate: Any, source_slug: str) -> str:
    value = str(candidate or "").strip()
    if not value or value.casefold() in {source_slug.casefold(), _safe_slug("skillhub-skill", source_slug).casefold()}:
        return _humanize_slug(source_slug)
    return value


def _icon_suffix(url: str, payload: bytes) -> str | None:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.casefold()
    if payload.startswith(b"\x89PNG"):
        return ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp"
    if len(payload) > 12 and payload[4:12] in {b"ftypavif", b"ftypavis"}:
        return ".avif"
    if payload.lstrip().startswith(b"<svg"):
        return ".svg"
    return suffix if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg"} else None


def _download_icon(url: Any) -> tuple[str, bytes] | None:
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return None
    payload = _request(url, accept="image/*")
    if len(payload) > MAX_ICON_BYTES:
        raise ValueError("icon too large")
    suffix = _icon_suffix(url, payload)
    if suffix is None:
        raise ValueError("unsupported icon format")
    return suffix, payload


def _request(url: str, *, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "mosswork-snapshot/1"})
    with urllib.request.urlopen(req, timeout=45) as response:
        body = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(body) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"response too large: {url}")
    return body


def _safe_slug(prefix: str, value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "resource"
    result = f"{prefix}-{slug}"
    if not SLUG_RE.fullmatch(result):
        raise ValueError(f"invalid generated slug: {result}")
    return result[:80].rstrip("-")


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    marker = "\n---"
    end = text.find(marker, 3)
    if end < 0:
        return {}, text
    header = text[4:end]
    body = text[end + len(marker) :].lstrip("\r\n")
    metadata: dict[str, Any] = {}
    # SkillHub's manifests use simple scalar YAML fields for the fields needed
    # by the local market. Keep the importer dependency-free and preserve the
    # full original file as the skill body.
    for line in header.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        value = value.strip().strip('"\'')
        if value and value != ">-" and value != "|":
            metadata[key.strip()] = value
    return metadata, body


def _normalize_skill_files(payload: bytes, local_slug: str) -> tuple[dict[str, bytes], dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [item.filename for item in archive.infolist() if not item.is_dir()]
        root_skill = next(
            (name for name in names if PurePosixPath(name).as_posix().casefold() == "skill.md"),
            None,
        )
        if root_skill is None:
            roots = {name.split("/", 1)[0] for name in names}
            skill_name = next(
                (name for name in names if PurePosixPath(name).name.casefold() == "skill.md"),
                None,
            )
            if len(roots) == 1 and skill_name is not None:
                prefix = f"{next(iter(roots))}/"
            else:
                raise ValueError("SkillHub package has no root SKILL.md")
        else:
            prefix = ""
        files: dict[str, bytes] = {}
        seen_paths: set[str] = set()
        for name in names:
            relative = name.removeprefix(prefix)
            path = PurePosixPath(relative)
            if not relative or path.is_absolute() or ".." in path.parts or "\\" in relative:
                raise ValueError(f"unsafe package path: {name}")
            if any(part.startswith(".") for part in path.parts) or any(
                part.casefold() in {"user.md", "memory.md", "credentials.json", "node_modules", "__pycache__"}
                for part in path.parts
            ):
                continue
            if path.name.startswith("._") or path.name.casefold() in {
                ".ds_store",
                "thumbs.db",
                "desktop.ini",
                "_meta.json",
            }:
                continue
            if path.name.casefold() == "skill.md":
                relative = "SKILL.md"
            elif "/" not in relative and path.suffix.casefold() in {".sh", ".py", ".js", ".ts"}:
                relative = f"scripts/{path.name}"
            elif PurePosixPath(relative).parts[0] not in {
                "references",
                "assets",
                "examples",
                "tests",
                "scripts",
            } and relative not in {
                "README.md",
                "CHANGELOG.md",
                "LICENSE",
                "LICENSE.md",
                "LICENSE.txt",
            }:
                # The local contract intentionally has a small allow-list.
                # Preserve other upstream material under references instead
                # of silently dropping it.
                relative = f"references/{relative}"
            # ZIP paths must remain portable across the offline deployment
            # hosts. Windows rejects characters such as ``:`` in filenames;
            # upstream packages occasionally contain them in documentation
            # names (for example ``assets:report-template.md``). Preserve
            # the file while making its archive path valid everywhere.
            relative = "/".join(
                re.sub(r'[<>:"/\\|?*]', "-", part).rstrip(" .") or "_"
                for part in PurePosixPath(relative).parts
            )
            normalized_relative = PurePosixPath(relative).as_posix()
            if normalized_relative.casefold() in seen_paths:
                # The local package contract is case-insensitive.  Upstream
                # archives occasionally contain SECURITY.md and security.md;
                # keep the first deterministic entry instead of producing a
                # package that cannot be imported on Windows.
                continue
            seen_paths.add(normalized_relative.casefold())
            data = archive.read(name)
            if path.suffix.lower() in {".md", ".json", ".yaml", ".yml", ".sh", ".py", ".js", ".ts"}:
                data.decode("utf-8")
            files[normalized_relative] = data
    text = files["SKILL.md"].decode("utf-8")
    metadata, body = _parse_frontmatter(text)
    description = str(metadata.get("description") or "").strip()
    if not description or description in {">-", "|"}:
        description = next((line.strip() for line in body.splitlines() if line.strip()), local_slug)
    if not body.strip():
        raise ValueError("SkillHub SKILL.md has no body")
    # Rebuild a minimal valid frontmatter block. Some upstream packages use
    # YAML quoting that is accepted by their site but not by our strict parser.
    text = (
        "---\n"
        f"name: {local_slug}\n"
        f"description: {json.dumps(description[:400], ensure_ascii=False)}\n"
        "---\n\n"
        f"{body}"
    )
    files["SKILL.md"] = text.encode("utf-8")
    return files, metadata


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def _fetch_skillsets(api: str) -> list[dict[str, Any]]:
    """Read every page of the public expert/skillset catalog."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1
    while True:
        payload = json.loads(
            _request(
                f"{api}/api/v1/skillsets?{urllib.parse.urlencode({'page': page, 'pageSize': 100})}",
                accept="application/json",
            )
        )
        page_rows = payload.get("skillSets") if isinstance(payload, dict) else None
        if not isinstance(page_rows, list):
            raise ValueError("SkillHub response has no skillSets array")
        for item in page_rows:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or "").strip()
            if slug and slug not in seen:
                seen.add(slug)
                rows.append(item)
        total = payload.get("total") if isinstance(payload, dict) else None
        if not page_rows or len(page_rows) < 100 or (isinstance(total, int) and len(rows) >= total):
            break
        page += 1
    return rows


def _fetch_skill_catalog(api: str) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Discover public skill-market records from search and showcase feeds.

    SkillHub does not expose a paged all-skills endpoint. Querying each slug
    character and merging the showcase feeds gives the build-time snapshot the
    complete set of records exposed by the public catalog APIs, while the
    expert references below cover skills that are not currently ranked.  The
    upstream search endpoint caps each query at 100 rows, so this is the full
    public catalog discoverable through the selected search terms and feeds,
    rather than a claim about private or unindexed upstream records.
    """
    catalog: dict[str, dict[str, Any]] = {}
    rankings: dict[str, list[dict[str, Any]]] = {}

    def add_rows(rows: Any) -> None:
        if not isinstance(rows, list):
            return
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            slug = str(raw.get("slug") or raw.get("name") or "").strip()
            if slug:
                catalog.setdefault(slug, dict(raw))

    with ThreadPoolExecutor(max_workers=8) as pool:
        search_futures = {
            pool.submit(
                _request,
                f"{api}/api/v1/search?{urllib.parse.urlencode({'q': term, 'limit': SKILL_SEARCH_LIMIT})}",
                accept="application/json",
            ): ("search", term)
            for term in SKILL_SEARCH_TERMS
        }
        ranking_futures = {
            pool.submit(
                _request,
                f"{api}/api/v1/showcase/{name}",
                accept="application/json",
            ): ("ranking", name)
            for name in SKILL_RANKING_ENDPOINTS
        }
        all_futures = {**search_futures, **ranking_futures}
        for future in as_completed(all_futures):
            kind, name = all_futures[future]
            try:
                data = json.loads(future.result())
            except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
                print(f"warning: {kind} catalog unavailable for {name}: {exc}", file=sys.stderr)
                continue
            if kind == "search":
                add_rows(data.get("results") if isinstance(data, dict) else None)
            else:
                rows = data.get("skills") if isinstance(data, dict) else None
                rankings[name] = [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
                add_rows(rows)
    return catalog, rankings


def _package(manifest: dict[str, Any], files: dict[str, bytes]) -> bytes:
    files = {"market.json": _json_bytes(manifest), **files}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return buffer.getvalue()


def _skill_record(
    api: str,
    slug: str,
    catalog: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bytes]:
    url = f"{api}/api/v1/download?{urllib.parse.urlencode({'slug': slug})}"
    catalog = catalog or {}
    try:
        payload = _request(url, accept="application/zip,application/octet-stream")
        files, metadata = _normalize_skill_files(payload, _safe_slug("skillhub-skill", slug))
        local_slug = _safe_slug("skillhub-skill", slug)
        catalog_name = _display_name(
            catalog.get("displayName") or catalog.get("name"), slug
        )
        metadata_name = str(metadata.get("displayName") or "").strip()
        display_name = _display_name(
            catalog_name or metadata_name or metadata.get("name"), slug
        )
        catalog_description = str(
            catalog.get("description_zh")
            or catalog.get("description")
            or catalog.get("summary")
            or ""
        ).strip()
        description = (
            catalog_description
            or str(metadata.get("description") or "").strip()
            or f"SkillHub skill: {slug}"
        )
        manifest = {
            "format": "mosswork.market.v1",
            "kind": "skill",
            "slug": local_slug,
            "name": display_name[:120],
            "description": description[:4000],
            "version": "1.0.0",
            "changelog": f"Imported from Tencent SkillHub ({slug}).",
            "resource_id": None,
            "dependencies": [],
            "runtime": {"execution_targets": ["server"], "platforms": ["any"]},
            "configuration": {},
            "required_capabilities": [],
            "network": {"scope": "none"},
            "license": "SkillHub upstream (verify before redistribution)",
            "author": str(metadata.get("author") or "SkillHub"),
        }
        try:
            icon = _download_icon(catalog.get("icon_url") or catalog.get("iconUrl"))
        except (OSError, urllib.error.URLError, ValueError) as exc:
            icon = None
            if catalog.get("icon_url") or catalog.get("iconUrl"):
                print(f"warning: icon unavailable for {slug}: {exc}", file=sys.stderr)
        if icon:
            suffix, icon_bytes = icon
            files[f"assets/icon{suffix}"] = icon_bytes
            manifest["icon_path"] = f"assets/icon{suffix}"
        files["references/skillhub-source.json"] = _json_bytes(
            {
                "source": "Tencent SkillHub",
                "slug": slug,
                "display_name": display_name,
                "download_url": url,
            }
        )
        archive = _package(manifest, files)
        return {
            "source_slug": slug,
            "local_slug": local_slug,
            "status": "downloaded",
            "version": manifest["version"],
            "archive": f"skills/{local_slug}.zip",
            "sha256": hashlib.sha256(archive).hexdigest(),
            "source_url": url,
            "archive_bytes": len(archive),
        }, archive
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"source_slug": slug, "status": "missing", "source_url": url}, b""
        return {"source_slug": slug, "status": "error", "source_url": url, "error": f"HTTP {exc.code}"}, b""
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"source_slug": slug, "status": "invalid", "source_url": url, "error": str(exc)}, b""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    api = args.api.rstrip("/")
    skillsets = _fetch_skillsets(api)
    skill_catalog, skill_rankings = _fetch_skill_catalog(api)
    unique_slugs = {
        str(slug).strip()
        for item in skillsets
        for slug in item.get("skillSlugs", [])
        if str(slug).strip()
    }
    unique_slugs.update(skill_catalog)
    unique_slugs = sorted(unique_slugs)

    skills_dir = args.output / "skills"
    icons_dir = args.output / "icons"
    experts_dir = args.output / "experts"
    for directory in (skills_dir, icons_dir, experts_dir):
        directory.mkdir(parents=True, exist_ok=True)
        # The snapshot directory is a generated build artifact. Remove files
        # from an older catalog so a refresh cannot ship stale ZIPs or icons
        # that are absent from index.json.
        for old_file in directory.iterdir():
            if old_file.is_file():
                old_file.unlink()

    skill_records: list[dict[str, Any]] = []
    archives: dict[str, bytes] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(_skill_record, api, slug, skill_catalog.get(slug)): slug
            for slug in unique_slugs
        }
        for future in as_completed(futures):
            try:
                record, archive = future.result()
            except Exception as exc:  # Keep the rest of the catalog importable.
                slug = futures[future]
                record, archive = {
                    "source_slug": slug,
                    "status": "error",
                    "source_url": f"{api}/api/v1/download?slug={urllib.parse.quote(slug)}",
                    "error": str(exc),
                }, b""
            skill_records.append(record)
            if archive:
                archives[record["archive"]] = archive
    for relative, archive in archives.items():
        (args.output / relative).write_bytes(archive)
    skill_records.sort(key=lambda item: item["source_slug"])
    downloaded = {item["source_slug"]: item for item in skill_records if item["status"] == "downloaded"}

    expert_records: list[dict[str, Any]] = []
    for item in skillsets:
        source_slug = str(item.get("slug") or "").strip()
        if not source_slug:
            continue
        missing = [slug for slug in item.get("skillSlugs", []) if slug not in downloaded]
        local_slug = _safe_slug("skillhub-expert", source_slug)
        icon = item.get("iconUrl")
        icon_path = None
        if isinstance(icon, str) and icon.startswith(("https://", "http://")):
            try:
                downloaded_icon = _download_icon(icon)
                if downloaded_icon is None:
                    raise ValueError("unsupported icon format")
                suffix, icon_payload = downloaded_icon
                icon_path = f"icons/{local_slug}{suffix}"
                (args.output / icon_path).write_bytes(icon_payload)
            except (OSError, urllib.error.URLError, ValueError) as exc:
                print(f"warning: icon unavailable for {source_slug}: {exc}", file=sys.stderr)
        if missing:
            expert_records.append({"source_slug": source_slug, "local_slug": local_slug, "status": "skipped_missing_skills", "missing_skills": missing})
            continue
        dependencies = [
            {"resource_id": f"skillhub-skill-{_safe_slug('resource', str(slug)).removeprefix('resource-')}", "version": "1.0.0"}
            for slug in item.get("skillSlugs", [])
        ]
        manifest = {
            "format": "mosswork.market.v1",
            "kind": "expert",
            "slug": local_slug,
            "name": _display_name(item.get("displayName") or item.get("name"), source_slug),
            "description": str(item.get("summary") or f"SkillHub expert: {source_slug}")[:4000],
            "version": "1.0.0",
            "changelog": f"Imported from Tencent SkillHub ({source_slug}).",
            "resource_id": None,
            "dependencies": dependencies,
            "runtime": {"execution_targets": ["server"], "platforms": ["any"]},
            "configuration": {},
            "required_capabilities": [],
            "network": {"scope": "none"},
            "license": "SkillHub upstream (verify before redistribution)",
            "author": "SkillHub",
        }
        display_name = _display_name(item.get("displayName") or item.get("name"), source_slug)
        display = {
            "zh": display_name,
            "en": _display_name(item.get("displayNameEn"), source_slug),
        }
        expert_manifest: dict[str, Any] = {
            "id": local_slug,
            "label": display,
            "description": {"zh": item.get("summary") or source_slug, "en": item.get("summaryEn") or source_slug},
            "prompt_files": ["SOUL.md"],
        }
        files = {
            "manifest.json": _json_bytes(expert_manifest),
            "SOUL.md": str(item.get("content") or "").encode("utf-8"),
            "references/skillhub-source.json": _json_bytes({"source": "Tencent SkillHub", "slug": source_slug, "source_url": f"{api}/api/v1/skillsets"}),
        }
        if icon_path:
            icon_bytes = (args.output / icon_path).read_bytes()
            files["assets/icon" + Path(icon_path).suffix.lower()] = icon_bytes
            manifest["icon_path"] = "assets/icon" + Path(icon_path).suffix.lower()
            expert_manifest["icon_path"] = manifest["icon_path"]
            files["manifest.json"] = _json_bytes(expert_manifest)
        archive = _package(manifest, files)
        archive_path = f"experts/{local_slug}.zip"
        (args.output / archive_path).write_bytes(archive)
        expert_records.append({"source_slug": source_slug, "local_slug": local_slug, "status": "downloaded", "archive": archive_path, "sha256": hashlib.sha256(archive).hexdigest(), "icon_path": icon_path})

    index = {
        "format": "mosswork.skillhub.snapshot.v1",
        "source": {
            "name": "Tencent SkillHub",
            "api": api,
            "fetched_at": datetime.now(UTC).isoformat(),
            "catalog_discovery": {
                "search_terms": list(SKILL_SEARCH_TERMS),
                "ranking_endpoints": list(SKILL_RANKING_ENDPOINTS),
            },
        },
        "skillsets": sorted(expert_records, key=lambda item: item["source_slug"]),
        "skills": skill_records,
        "skill_catalog": sorted(
            skill_catalog.values(),
            key=lambda item: str(item.get("slug") or item.get("name") or ""),
        ),
        "skill_rankings": skill_rankings,
    }
    (args.output / "index.json").write_bytes(_json_bytes(index))
    print(json.dumps({"skillsets": len(skillsets), "catalog_skills": len(skill_catalog), "experts_publishable": sum(item["status"] == "downloaded" for item in expert_records), "skills_downloaded": len(downloaded), "skills_missing": len(skill_records) - len(downloaded), "output": str(args.output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
