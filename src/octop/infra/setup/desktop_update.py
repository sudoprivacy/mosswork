"""Serve complete native client releases through the selected company server."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urljoin

import httpx

RELEASE_ROOT = "https://sudowork-release-1309794936.cos.accelerate.myqcloud.com/mosswork/release/"
FEED_URL = RELEASE_ROOT + "latest/desktop-update.json"
STABLE = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
ARTIFACT = re.compile(
    rf"mosswork-company-(?:windows-amd64|macos-arm64)-({STABLE})-update\.(?:exe|zip)"
)


def artifact_path(version: str, filename: str) -> str:
    match = ARTIFACT.fullmatch(filename)
    if match is None or match[1] != version:
        raise ValueError("Invalid native update artifact")
    if ("windows-amd64" in filename) != filename.endswith(".exe"):
        raise ValueError("Invalid native update artifact extension")
    return f"releases/v{version}/{filename}"


async def fetch_manifest() -> dict[str, Any]:
    async with (
        httpx.AsyncClient(timeout=30, follow_redirects=False) as client,
        client.stream("GET", FEED_URL, headers={"Cache-Control": "no-cache"}) as response,
    ):
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > 64 * 1024:
                raise ValueError("Native update manifest exceeds its size limit")
    manifest: dict[str, Any] = json.loads(data)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != 1
        or manifest.get("channel") != "stable"
        or not isinstance(manifest.get("metadata"), dict)
        or manifest.get("metadata", {}).get("distribution") != "mosswork-company"
        or not isinstance(manifest.get("version"), str)
        or not re.fullmatch(STABLE, manifest.get("version", ""))
        or not isinstance(manifest.get("artifacts"), list)
        or not manifest["artifacts"]
    ):
        raise ValueError("Invalid native update manifest")
    for artifact in manifest["artifacts"]:
        if not isinstance(artifact, dict):
            raise ValueError("Invalid native update artifact")
        relative = artifact_path(manifest["version"], artifact["filename"])
        if urljoin(FEED_URL, artifact["url"]) != RELEASE_ROOT + relative:
            raise ValueError("Native update artifact leaves the official release directory")
        # Preserve hashes and sizes; both discovery and downloads use the server.
        artifact["url"] = "../" + relative
    return manifest


async def open_artifact(version: str, filename: str) -> tuple[httpx.AsyncClient, httpx.Response]:
    relative = artifact_path(version, filename)
    client = httpx.AsyncClient(timeout=60, follow_redirects=False)
    try:
        response = await client.send(
            client.build_request(
                "GET", RELEASE_ROOT + relative, headers={"Accept-Encoding": "identity"}
            ),
            stream=True,
        )
        response.raise_for_status()
        return client, response
    except BaseException:
        await client.aclose()
        raise


async def artifact_bytes(
    client: httpx.AsyncClient, response: httpx.Response
) -> AsyncIterator[bytes]:
    try:
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > 2 << 30:
                raise ValueError("Native update artifact exceeds its size limit")
            yield chunk
    finally:
        await response.aclose()
        await client.aclose()
