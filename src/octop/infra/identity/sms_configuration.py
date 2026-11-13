"""Read the original saved SMS configuration without activating or copying it.

Moss103 stores managed SMS under platform.v1.sms, with immutable references in
the original moss:platform-config vault. Legacy sudowork.sms is a separate
fallback, not the active managed record. This reader provides metadata only;
current secret access, runtime activation/readiness and writer ownership stay
with the actual trusted composition. No credential value is read or created.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from octop.infra.errors import ErrorCode, MossworkError

type SmsConfigurationValue = bool | int | str | tuple[str, ...]

SMS_CONFIGURATION_VAULT_NAMESPACE = "moss:platform-config"

_BOOLEAN = frozenset({"enabled", "mockDelivery"})
_TEXT = frozenset({"sdkAppId", "signName", "templateId", "region"})
_NUMBER = frozenset({"codeTtlSec", "resendCooldownSec", "maxSendsPerHour", "maxVerifyAttempts"})
_SECRETS = frozenset({"secretId", "secretKey"})


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR,
        status=503,
        details={"dependency": "canonical_saved_sms_configuration"},
    )


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-JSON numeric constant")


@dataclass(frozen=True, repr=False)
class SavedSmsConfiguration:
    """Original immutable metadata, not an active runtime or credential grant.

    The opaque references must be resolved only through the existing vault and
    current authority. Configuration and references cannot be mutated through
    this object. Revalidation checks the same retained row and reader, never
    silently substitutes a newer version or a legacy fallback.
    """

    version: str
    configuration: Mapping[str, SmsConfigurationValue]
    secret_references: Mapping[str, str] = field(repr=False)
    updated_by: str
    updated_at: int
    original_record_sha256: str
    _reader: object = field(repr=False, compare=False)


class SqliteSavedSmsConfigurationReader:
    """Read existing canonical storage using mode=ro/query_only only.

    Missing managed record means None, not ready/disabled/permission to import
    a legacy setting. Missing schema, malformed metadata or changed original
    references refuse. Save, secret staging, version/history publication and
    active-version/restart handling are not implemented by this reader.
    """

    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A trusted original canonical SQLite Path is required")
        self._uri = database_path.absolute().as_uri() + "?mode=ro"
        self._seal = object()

    def _read(self) -> SavedSmsConfiguration | None:
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as db:
                db.execute("PRAGMA query_only = ON")
                db.execute("BEGIN")
                row = db.execute(
                    "SELECT value_json FROM platform_integration_settings "
                    "WHERE setting_key='platform.v1.sms'"
                ).fetchone()
                if row is None:
                    return None
                if not isinstance(row[0], str):
                    raise _unavailable()
                raw = row[0]
                value = json.loads(raw, parse_constant=_invalid_constant)
                if not isinstance(value, dict):
                    raise _unavailable()
                version, config, refs = (
                    value.get("version"),
                    value.get("config"),
                    value.get("secretRefs"),
                )
                if (
                    not isinstance(version, str)
                    or not isinstance(config, dict)
                    or not isinstance(refs, dict)
                ):
                    raise _unavailable()
                # Exact original SMS field kinds; unknown fields cannot become
                # a new secret/configuration channel through this projection.
                configuration: dict[str, SmsConfigurationValue] = {}
                for key, item in config.items():
                    if (
                        key in _BOOLEAN
                        and type(item) is bool
                        or key in _TEXT
                        and isinstance(item, str)
                    ):
                        configuration[key] = item
                    elif key in _NUMBER and (
                        type(item) in (int, float)
                        and math.isfinite(item)
                        and int(item) == item
                        and abs(item) <= 2**53 - 1
                    ):
                        configuration[key] = int(item)
                    elif (
                        key == "templateParams"
                        and isinstance(item, list)
                        and all(isinstance(part, str) for part in item)
                    ):
                        configuration[key] = tuple(item)
                    else:
                        raise _unavailable()
                for key, ref in refs.items():
                    if (
                        key not in _SECRETS
                        or not isinstance(ref, str)
                        or not ref.startswith(f"sms.{version}.")
                    ):
                        raise _unavailable()
                updated_by, updated_at = value.get("updatedBy"), value.get("updatedAt")
                if not isinstance(updated_by, str) or not (
                    isinstance(updated_at, (int, float))
                    and not isinstance(updated_at, bool)
                    and math.isfinite(updated_at)
                    and int(updated_at) == updated_at
                    and 0 <= updated_at <= 2**53 - 1
                ):
                    raise _unavailable()
                return SavedSmsConfiguration(
                    version,
                    MappingProxyType(configuration),
                    MappingProxyType(dict(refs)),
                    updated_by,
                    int(updated_at),
                    hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                    self._seal,
                )
        except (sqlite3.Error, OSError, ValueError, RecursionError, OverflowError):
            raise _unavailable() from None

    async def read_saved(self) -> SavedSmsConfiguration | None:
        """Return original saved metadata; this never reports runtime readiness."""
        return await asyncio.get_running_loop().run_in_executor(None, self._read)

    async def assert_current(self, expected: SavedSmsConfiguration) -> None:
        """Refuse a withdrawn/changed original record before using its references.

        A subsequent effect still needs current authority at the actual vault or
        runtime service I/O. This separate read is not an atomic effect fence.
        """
        if type(expected) is not SavedSmsConfiguration or expected._reader is not self._seal:
            raise _unavailable()
        current = await self.read_saved()
        if current is None or current != expected:
            raise _unavailable()
