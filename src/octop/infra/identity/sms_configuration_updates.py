"""Update an existing managed SMS record through the original vault and writer.

This port retains Moss103's stage/verify/version/history sequence. It does not
take over an unmanaged legacy configuration, activate a runtime, send SMS, or
report provider readiness. The trusted composition must supply the existing
vault and hold canonical writer exclusion throughout the operation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import CanonicalIdentitySnapshot, CanonicalSnapshotService
from octop.infra.identity.current_human import assert_current_human
from octop.infra.identity.organization_members import _assert_same_actor
from octop.infra.identity.phone_auth import _JS_WHITESPACE
from octop.infra.identity.platform_login_policy import _require_platform_administrator
from octop.infra.identity.sms_configuration import (
    SMS_CONFIGURATION_VAULT_NAMESPACE,
    SavedSmsConfiguration,
    SmsConfigurationValue,
    SqliteSavedSmsConfigurationReader,
)
from octop.infra.identity.write_authority import CanonicalWriterAdmission, assert_canonical_writer

_BOOLEAN = frozenset({"enabled", "mockDelivery"})
_TEXT = frozenset({"sdkAppId", "signName", "templateId", "region"})
_LIMITS = {
    "codeTtlSec": (60, 3600),
    "resendCooldownSec": (1, 3600),
    "maxSendsPerHour": (1, 100),
    "maxVerifyAttempts": (1, 20),
}
_SECRETS = ("secretId", "secretKey")


class CanonicalSmsConfigurationVault(Protocol):
    """Existing trusted platform vault adapter; never request-selected storage."""

    async def get_secret(self, namespace: str, key: str) -> str | None: ...

    async def put_secret(self, namespace: str, key: str, value: str) -> None:
        """Stage an immutable versioned value; return None or raise."""
        ...


@dataclass(frozen=True)
class SmsConfigurationSaved:
    version: str
    restart_required: bool = True


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR, status=503, details={"dependency": "canonical_sms_configuration"}
    )


def _invalid() -> MossworkError:
    return MossworkError.localized(ErrorCode.IDENTITY_REQUEST_INVALID, status=400)


def _conflict() -> MossworkError:
    return MossworkError.localized(ErrorCode.IDENTITY_REQUEST_INVALID, status=409)


def _changes(
    configuration: Mapping[str, object], secrets: Mapping[str, object]
) -> tuple[dict[str, SmsConfigurationValue], dict[str, str]]:
    config: dict[str, SmsConfigurationValue] = {}
    private: dict[str, str] = {}
    for key, value in configuration.items():
        if key in _BOOLEAN and type(value) is bool:
            config[key] = value
        elif key in _TEXT and isinstance(value, str):
            config[key] = value.strip(_JS_WHITESPACE)
        elif key in _LIMITS and type(value) in (int, float):
            if (
                not isinstance(value, (int, float))
                or abs(value) > 2**53 - 1
                or not math.isfinite(value)
            ):
                raise _invalid()
            if int(value) != value:
                raise _invalid()
            config[key] = int(value)
        elif key == "templateParams" and isinstance(value, (list, tuple)):
            if not all(isinstance(part, str) for part in value):
                raise _invalid()
            config[key] = tuple(value)
        else:
            raise _invalid()
    for key, value in secrets.items():
        if key not in _SECRETS or value is not None and not isinstance(value, str):
            raise _invalid()
        if isinstance(value, str) and value.startswith("****"):
            raise _invalid()
        private[key] = value.strip(_JS_WHITESPACE) if isinstance(value, str) else ""
    return config, private


def _validate(
    configuration: Mapping[str, SmsConfigurationValue], secrets: Mapping[str, str]
) -> None:
    mock = configuration.get("mockDelivery") is True
    if configuration.get("enabled") and not mock:
        if any(not configuration.get(key) for key in _TEXT) or any(
            not secrets.get(key) for key in _SECRETS
        ):
            raise _invalid()
        params = configuration.get("templateParams")
        if not isinstance(params, tuple) or not any("{code}" in item for item in params):
            raise _invalid()
    for key, (low, high) in _LIMITS.items():
        value = configuration.get(key)
        if value is not None and (not isinstance(value, int) or not low <= value <= high):
            raise _invalid()


class CanonicalManagedSmsConfigurationService:
    """Save an already-managed record without changing the active version.

    Missing managed metadata refuses rather than silently adopting legacy/env
    secrets. Original legacy conflict resolution and first takeover need their
    own trusted provider. Unused staged secrets remain on refusal, just as in
    the original service: no delete/remint/retry is inferred from uncertainty.
    Refusal after a committed write withholds acknowledgement, not the write.
    """

    def __init__(
        self,
        database_path: Path,
        snapshots: CanonicalSnapshotService,
        *,
        writer: CanonicalWriterAdmission,
        vault: CanonicalSmsConfigurationVault,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A trusted canonical SQLite Path is required")
        uri = database_path.absolute().as_uri()
        if (
            not isinstance(snapshots, CanonicalSnapshotService)
            or snapshots._store._uri != uri + "?mode=ro"
        ):
            raise _unavailable()
        if not all(callable(getattr(vault, name, None)) for name in ("get_secret", "put_secret")):
            raise TypeError("The existing trusted platform vault adapter is required")
        self._write_uri = uri + "?mode=rw"
        self._reader = SqliteSavedSmsConfigurationReader(database_path)
        self._snapshots, self._writer, self._vault, self._clock = snapshots, writer, vault, clock

    async def _current(
        self,
        token: str,
        actor: CanonicalIdentitySnapshot,
        saved: SavedSmsConfiguration | None = None,
    ) -> None:
        current = await self._snapshots.snapshot(token)
        _require_platform_administrator(current)
        _assert_same_actor(actor, current)
        await assert_canonical_writer(self._writer)
        if saved is not None:
            await self._reader.assert_current(saved)

    async def inspect_saved(self, opaque_token: str) -> SavedSmsConfiguration | None:
        """Read saved metadata under current platform administration.

        This operation neither reads the vault nor needs canonical write
        takeover. Missing managed data stays absent; no legacy settings or
        active/runtime readiness are substituted. HTTP consumers must project
        only nonsecret configuration and reference-presence flags.
        """
        actor = await self._snapshots.snapshot(opaque_token)
        _require_platform_administrator(actor)
        saved = await self._reader.read_saved()
        middle = await self._snapshots.snapshot(opaque_token)
        _require_platform_administrator(middle)
        _assert_same_actor(actor, middle)
        if await self._reader.read_saved() != saved:
            raise _unavailable()
        current = await self._snapshots.snapshot(opaque_token)
        _require_platform_administrator(current)
        _assert_same_actor(actor, current)
        return saved

    def _publish(
        self,
        actor: CanonicalIdentitySnapshot,
        saved: SavedSmsConfiguration,
        version: str,
        configuration: dict[str, SmsConfigurationValue],
        refs: dict[str, str],
    ) -> None:
        try:
            with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                _require_platform_administrator(actor)
                assert_current_human(db, actor, self._clock)
                row = db.execute(
                    "SELECT value_json FROM platform_integration_settings WHERE setting_key='platform.v1.sms'"
                ).fetchone()
                if row is None or not isinstance(row[0], str):
                    raise _conflict()
                if (
                    hashlib.sha256(row[0].encode("utf-8")).hexdigest()
                    != saved.original_record_sha256
                ):
                    raise _conflict()
                now = self._clock()
                if (
                    type(now) not in (int, float)
                    or not math.isfinite(now)
                    or not 0 <= now <= (2**53 - 1) / 1000
                ):
                    raise _unavailable()
                timestamp = math.floor(now * 1000)
                value = json.dumps(
                    {
                        "version": version,
                        "config": configuration,
                        "secretRefs": refs,
                        "updatedBy": actor.member.owner_id,
                        "updatedAt": timestamp,
                    },
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                for key, raw in (
                    (f"platform.history.sms.{saved.version}", row[0]),
                    ("platform.v1.sms", value),
                ):
                    db.execute(
                        "INSERT INTO platform_integration_settings "
                        "(setting_key,value_json,updated_by,created_at,updated_at) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(setting_key) DO UPDATE SET value_json=excluded.value_json,"
                        "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                        (key, raw, actor.member.owner_id, timestamp, timestamp),
                    )
        except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
            raise _unavailable() from None

    async def save(
        self,
        opaque_token: str,
        *,
        expected_version: str | None,
        configuration: Mapping[str, object],
        secrets: Mapping[str, object],
    ) -> SmsConfigurationSaved:
        """Merge current managed values, stage/verify secrets, atomically publish.

        Current role, signed ceiling, original metadata and writer assertions
        surround vault I/O. The actual adapters must enforce authority at their
        effect boundary. This service does not supply that production adapter.
        """
        if expected_version is not None and not isinstance(expected_version, str):
            raise _invalid()
        config_changes, secret_changes = _changes(configuration, secrets)
        actor = await self._snapshots.snapshot(opaque_token)
        _require_platform_administrator(actor)
        await assert_canonical_writer(self._writer)
        saved = await self._reader.read_saved()
        if (saved.version if saved is not None else None) != expected_version:
            raise _conflict()
        if saved is None:
            raise _unavailable()
        config = dict(saved.configuration) | config_changes
        private: dict[str, str] = {}
        for key, ref in saved.secret_references.items():
            await self._current(opaque_token, actor, saved)
            try:
                value = await self._vault.get_secret(SMS_CONFIGURATION_VAULT_NAMESPACE, ref)
            except Exception:
                raise _unavailable() from None
            await self._current(opaque_token, actor, saved)
            if not isinstance(value, str) or not value:
                raise _unavailable()
            private[key] = value
        private.update(secret_changes)
        _validate(config, private)
        version = str(uuid4())
        refs: dict[str, str] = {}
        for key, value in private.items():
            if not value:
                continue
            ref = f"sms.{version}.{key}"
            await self._current(opaque_token, actor, saved)
            try:
                put: Callable[[str, str, str], Awaitable[object]] = self._vault.put_secret
                result = await put(SMS_CONFIGURATION_VAULT_NAMESPACE, ref, value)
            except Exception:
                raise _unavailable() from None
            await self._current(opaque_token, actor, saved)
            if result is not None:
                raise _unavailable()
            try:
                verified = await self._vault.get_secret(SMS_CONFIGURATION_VAULT_NAMESPACE, ref)
            except Exception:
                raise _unavailable() from None
            await self._current(opaque_token, actor, saved)
            if type(verified) is not str or verified != value:
                raise _unavailable()
            refs[key] = ref
        await self._current(opaque_token, actor, saved)
        await asyncio.get_running_loop().run_in_executor(
            None, self._publish, actor, saved, version, config, refs
        )
        await self._current(opaque_token, actor)
        return SmsConfigurationSaved(version)
