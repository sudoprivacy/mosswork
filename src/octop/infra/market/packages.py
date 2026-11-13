"""Offline package contract. Parsing never extracts or executes uploaded files."""

from __future__ import annotations

import hashlib
import io
import json
import lzma
import re
import stat
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.utils.frontmatter import parse_frontmatter

MAX_BYTES = 64 * 1024 * 1024
MAX_FILES = 2_000
SEMVER = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
SLUG = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"

# These entries are produced by desktop file managers while creating an
# archive. They carry folder view/resource-fork metadata and are not resource
# package content. They are ignored during import so developers do not need to
# edit an archive after using Finder, Explorer, or a Linux file manager.
_ARCHIVE_METADATA_NAMES = frozenset(
    {
        ".ds_store",
        ".directory",
        "desktop.ini",
        "ehthumbs.db",
        "iconcache.db",
        "thumbs.db",
    }
)
_ARCHIVE_METADATA_DIRECTORIES = frozenset(
    {
        "$recycle.bin",
        ".appledouble",
        ".documentrevisions-v100",
        ".spotlight-v100",
        ".temporaryitems",
        ".trash",
        ".trashes",
        "__macosx",
        "system volume information",
    }
)


def _is_archive_metadata(path: PurePosixPath) -> bool:
    """Return whether *path* is known file-manager metadata.

    Keep this list narrow: arbitrary hidden files remain rejected by the
    package contract because they may contain credentials or personal data.
    """

    parts = path.parts
    if any(part.casefold() in _ARCHIVE_METADATA_DIRECTORIES for part in parts):
        return True
    name = path.name
    folded_name = name.casefold()
    if folded_name in _ARCHIVE_METADATA_NAMES or name == "Icon\r":
        return True
    if name.startswith("._"):
        return True
    return any(part.casefold().startswith(".trash-") for part in parts)


def invalid(reason: str, path: str = "") -> MossworkError:
    return MossworkError.localized(
        ErrorCode.MARKET_PACKAGE_INVALID,
        details={"reason": reason, "path": path},
    )


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Dependency(Contract):
    resource_id: str = Field(min_length=1, max_length=100)
    version: str = Field(pattern=SEMVER, max_length=40)


class Runtime(Contract):
    execution_targets: list[Literal["server", "desktop"]] = Field(min_length=1, max_length=2)
    platforms: list[str] = Field(min_length=1, max_length=20)
    python: str = Field(default="", max_length=100)
    runtime_profile: str = Field(default="", max_length=100)


class Configuration(Contract):
    type: Literal["string", "url", "integer", "boolean", "credential_ref"]
    required: bool = False
    description: str = Field(default="", max_length=1000)


class Network(Contract):
    scope: Literal["none", "intranet"] = "none"


class MarketManifest(Contract):
    format: Literal["mosswork.market.v1", "octop.market.v1"]
    kind: Literal["skill", "expert"]
    slug: str = Field(pattern=SLUG, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=4000)
    version: str = Field(pattern=SEMVER, max_length=40)
    changelog: str = Field(min_length=1, max_length=8000)
    resource_id: str | None = Field(default=None, max_length=100)
    dependencies: list[Dependency] = Field(default_factory=list, max_length=100)
    runtime: Runtime
    configuration: dict[str, Configuration] = Field(default_factory=dict, max_length=100)
    required_capabilities: list[str] = Field(default_factory=list, max_length=100)
    network: Network = Field(default_factory=Network)
    license: str = Field(min_length=1, max_length=200)
    author: str = Field(default="", max_length=200)
    icon_path: str | None = Field(
        default=None,
        max_length=200,
        description="Optional package-relative icon path for offline market cards.",
    )

    @model_validator(mode="after")
    def unique_dependencies(self) -> MarketManifest:
        identities = [item.resource_id for item in self.dependencies]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate_dependency")
        return self


@dataclass(frozen=True)
class Package:
    manifest: MarketManifest
    files: dict[str, bytes]

    def archive(self) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, content in sorted(self.files.items()):
                entry = zipfile.ZipInfo(path, date_time=(2020, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(entry, content)
        return buffer.getvalue()

    def inventory(self) -> list[dict[str, str | int]]:
        return [
            {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in sorted(self.files.items())
        ]


def parse_package(payload: bytes) -> Package:
    if len(payload) > MAX_BYTES:
        raise invalid("size_limit")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if not entries:
                raise invalid("file_limit")
            usable_entries: list[zipfile.ZipInfo] = []
            for entry in entries:
                raw = entry.orig_filename
                path = PurePosixPath(raw)
                mode = entry.external_attr >> 16
                if (
                    path.is_absolute()
                    or raw.rstrip("/") != path.as_posix()
                    or "\\" in raw
                    or "\x00" in raw
                    or any(part in {"..", "."} or ":" in part for part in raw.split("/"))
                    or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or entry.flag_bits & 1
                ):
                    raise invalid("unsafe_path", raw)
                if not _is_archive_metadata(path):
                    usable_entries.append(entry)
            if not usable_entries:
                raise invalid("file_limit")
            if len(usable_entries) > MAX_FILES:
                raise invalid("file_limit")
            names = [entry.filename for entry in usable_entries]
            prefix = ""
            if "market.json" not in names:
                roots = {name.split("/")[0] for name in names}
                if len(roots) == 1:
                    prefix = f"{next(iter(roots))}/"
            files: dict[str, bytes] = {}
            seen: set[str] = set()
            total = 0
            for entry in usable_entries:
                raw = entry.orig_filename
                relative = raw.removeprefix(prefix)
                if not relative or entry.is_dir():
                    continue
                if relative.casefold() in seen:
                    raise invalid("duplicate_path", relative)
                seen.add(relative.casefold())
                parts = PurePosixPath(relative).parts
                if any(part.startswith(".") for part in parts) or any(
                    part.casefold()
                    in {"user.md", "memory.md", "credentials.json", "__pycache__", "node_modules"}
                    for part in parts
                ):
                    raise invalid("private_content", relative)
                total += entry.file_size
                if total > MAX_BYTES:
                    raise invalid("size_limit")
                with archive.open(entry) as stream:
                    data = stream.read(entry.file_size + 1)
                if len(data) != entry.file_size:
                    raise invalid("size_limit", relative)
                files[relative] = data
    except (
        zipfile.BadZipFile,
        NotImplementedError,
        RuntimeError,
        OSError,
        EOFError,
        zlib.error,
        lzma.LZMAError,
    ) as exc:
        raise invalid("invalid_zip") from exc
    try:
        if len(files.get("market.json", b"")) > 256 * 1024:
            raise invalid("market_manifest", "market.json")
        manifest = MarketManifest.model_validate_json(files.get("market.json", b""))
    except ValidationError as exc:
        raise invalid("market_manifest", "market.json") from exc
    common = {"market.json", "README.md", "CHANGELOG.md", "LICENSE", "LICENSE.txt", "LICENSE.md"}
    roots_allowed = common | (
        {"SKILL.md"}
        if manifest.kind == "skill"
        else {
            "manifest.json",
            "SOUL.md",
            "IDENTITY.md",
            "AGENTS.md",
            "TOOLS.md",
            "HEARTBEAT.md",
            "PROACTIVE.md",
        }
    )
    directories = {"references", "assets", "examples", "tests"}
    if manifest.kind == "skill":
        directories.add("scripts")
    for name in files:
        if name not in roots_allowed and ("/" not in name or name.split("/")[0] not in directories):
            raise invalid("unsupported_file", name)
    if manifest.icon_path is not None:
        icon_path = PurePosixPath(manifest.icon_path)
        if (
            icon_path.is_absolute()
            or ".." in icon_path.parts
            or icon_path.as_posix() not in files
            or not icon_path.as_posix().startswith("assets/")
        ):
            raise invalid("icon_path", manifest.icon_path)
    if manifest.kind == "skill":
        try:
            content = files["SKILL.md"].decode("utf-8")
            metadata, body = parse_frontmatter(content)
            if (
                metadata.get("name") != manifest.slug
                or not str(metadata.get("description", "")).strip()
                or not body.strip()
            ):
                raise invalid("skill_manifest", "SKILL.md")
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            raise invalid("skill_manifest", "SKILL.md") from exc
    else:
        try:
            display = json.loads(files["manifest.json"].decode("utf-8"))
            if (
                not isinstance(display, dict)
                or display.get("id") != manifest.slug
                or not files["SOUL.md"].decode("utf-8").strip()
            ):
                raise invalid("expert_manifest")
            if "desktop" in manifest.runtime.execution_targets and display.get("subagents"):
                raise invalid("desktop_subagents")
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            raise invalid("expert_manifest") from exc
    for name, data in files.items():
        if name.endswith((".md", ".json")):
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise invalid("utf8", name) from exc
    files["market.json"] = manifest.model_dump_json(indent=2).encode("utf-8")
    return Package(manifest, files)


def semver_key(version: str) -> tuple[int, ...]:
    if not re.fullmatch(SEMVER, version):
        raise invalid("version")
    return tuple(int(part) for part in version.split("."))
