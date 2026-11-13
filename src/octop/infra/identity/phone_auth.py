"""Retained HMAC phone-code domain, using the existing canonical SQLite tables.

No schema creation, SMS fallback, logging, account registration or JWT issuing
lives here. Writer admission and the real asynchronous SMS provider must be
injected by trusted composition. Database operations run off the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from octop.infra.identity.write_authority import CanonicalWriterAdmission, assert_canonical_writer

_JS_WHITESPACE = " \t\n\r\v\f\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_PHONE_SEPARATORS = re.compile(f"[{_JS_WHITESPACE}-]")
_HOUR_MS = 60 * 60 * 1000

type PhoneAuthReason = Literal[
    "invalid_phone",
    "resend_cooldown",
    "hourly_limit",
    "verify_attempts_exhausted",
    "sms_delivery_failed",
    "phone_code_store_unavailable",
]


class PhoneAuthError(Exception):
    """Stable domain refusal; transport maps the reason to localized copy."""

    def __init__(
        self, status: int, reason: PhoneAuthReason, retry_after_sec: int | None = None
    ) -> None:
        self.status = status
        self.reason = reason
        self.retry_after_sec = retry_after_sec
        super().__init__(reason)


@dataclass(frozen=True)
class PhoneCodeConfig:
    code_ttl_sec: int
    resend_cooldown_sec: int
    max_sends_per_hour: int
    max_verify_attempts: int

    def __post_init__(self) -> None:
        for value in (self.code_ttl_sec, self.max_sends_per_hour, self.max_verify_attempts):
            if type(value) is not int or value <= 0:
                raise ValueError("Phone code limits must be positive integers")
        if type(self.resend_cooldown_sec) is not int or self.resend_cooldown_sec < 0:
            raise ValueError("Phone code cooldown must be a nonnegative integer")


@dataclass(frozen=True)
class PhoneCodeSent:
    next_send_in: int
    delivery: Literal["tencent"] = "tencent"


class SmsSender(Protocol):
    async def __call__(self, phone: str, code: str) -> None: ...


def normalize_phone(raw: object) -> str | None:
    """Match retained JavaScript whitespace, prefix and ASCII digit semantics."""
    if not isinstance(raw, str):
        return None
    bare = _PHONE_SEPARATORS.sub("", raw)
    if bare.startswith("+86"):
        bare = bare[3:]
    elif bare.startswith("86") and len(bare) == 13:
        bare = bare[2:]
    return bare if re.fullmatch(r"1[0-9]{10}", bare) else None


def _hash_code(code: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), f"phone-code:{code}".encode(), hashlib.sha256
    ).hexdigest()


@dataclass(frozen=True)
class _Reservation:
    phone: str
    code_hash: str
    created_at: int
    expires_at: int
    send_id: int


class SqlitePhoneCodeStore:
    """Use only an existing canonical schema; mode=rw never creates a DB."""

    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A trusted local SQLite Path is required")
        self._uri = database_path.absolute().as_uri() + "?mode=rw"

    def _reserve(
        self, phone: str, code_hash: str, now: int, config: PhoneCodeConfig
    ) -> _Reservation:
        try:
            with (
                closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as connection,
                connection,
            ):
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM phone_login_codes WHERE expires_at <= ?", (now,))
                connection.execute(
                    "DELETE FROM phone_login_sends WHERE sent_at < ?", (now - 24 * _HOUR_MS,)
                )
                existing = connection.execute(
                    "SELECT created_at FROM phone_login_codes WHERE phone = ?", (phone,)
                ).fetchone()
                if existing is not None:
                    remaining = config.resend_cooldown_sec - (now - existing[0]) // 1000
                    if remaining > 0:
                        raise PhoneAuthError(429, "resend_cooldown", remaining)
                sends = connection.execute(
                    "SELECT COUNT(*) FROM phone_login_sends WHERE phone = ? AND sent_at >= ?",
                    (phone, now - _HOUR_MS),
                ).fetchone()
                if sends[0] >= config.max_sends_per_hour:
                    raise PhoneAuthError(429, "hourly_limit")
                expires = now + config.code_ttl_sec * 1000
                connection.execute(
                    """INSERT INTO phone_login_codes
                    (phone, code_hash, created_at, expires_at, attempts) VALUES (?, ?, ?, ?, 0)
                    ON CONFLICT(phone) DO UPDATE SET code_hash = excluded.code_hash,
                    created_at = excluded.created_at, expires_at = excluded.expires_at,
                    attempts = 0""",
                    (phone, code_hash, now, expires),
                )
                cursor = connection.execute(
                    "INSERT INTO phone_login_sends (phone, sent_at) VALUES (?, ?)", (phone, now)
                )
                assert cursor.lastrowid is not None
                return _Reservation(phone, code_hash, now, expires, cursor.lastrowid)
        except sqlite3.Error:
            raise PhoneAuthError(503, "phone_code_store_unavailable") from None

    def _discard_failed_send(self, reservation: _Reservation) -> None:
        try:
            with (
                closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as connection,
                connection,
            ):
                # The send-log id identifies our reservation even if a later
                # code happens to have the same HMAC and millisecond clock.
                connection.execute(
                    """DELETE FROM phone_login_codes
                    WHERE phone = ? AND code_hash = ? AND created_at = ? AND expires_at = ?
                    AND (SELECT MAX(id) FROM phone_login_sends WHERE phone = ?) = ?""",
                    (
                        reservation.phone,
                        reservation.code_hash,
                        reservation.created_at,
                        reservation.expires_at,
                        reservation.phone,
                        reservation.send_id,
                    ),
                )
        except sqlite3.Error:
            raise PhoneAuthError(503, "phone_code_store_unavailable") from None

    def _verify(self, phone: str, code_hash: str, now: int, max_attempts: int) -> bool:
        try:
            with (
                closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as connection,
                connection,
            ):
                connection.execute("BEGIN IMMEDIATE")
                record = connection.execute(
                    "SELECT code_hash, expires_at, attempts FROM phone_login_codes WHERE phone = ?",
                    (phone,),
                ).fetchone()
                if record is None:
                    return False
                if record[1] <= now:
                    connection.execute("DELETE FROM phone_login_codes WHERE phone = ?", (phone,))
                    return False
                if record[2] >= max_attempts:
                    connection.execute("DELETE FROM phone_login_codes WHERE phone = ?", (phone,))
                    # Commit the burn before surfacing the refusal.
                    connection.commit()
                    raise PhoneAuthError(429, "verify_attempts_exhausted")
                if not hmac.compare_digest(code_hash, record[0]):
                    connection.execute(
                        "UPDATE phone_login_codes SET attempts = attempts + 1 WHERE phone = ?",
                        (phone,),
                    )
                    return False
                consumed = connection.execute(
                    "DELETE FROM phone_login_codes WHERE phone = ? AND code_hash = ?",
                    (phone, record[0]),
                )
                return consumed.rowcount > 0
        except sqlite3.Error:
            raise PhoneAuthError(503, "phone_code_store_unavailable") from None


class CanonicalPhoneAuthService:
    """Send and consume retained phone codes after explicit writer admission.

    Reservation and verification use BEGIN IMMEDIATE, tightening the original
    read-then-send race without changing its tables or HMAC. A failed provider
    clears only its own pending code; the hourly send log remains, as before.
    Cancellation leaves an uncertain delivery reserved rather than retrying.
    """

    def __init__(
        self,
        store: SqlitePhoneCodeStore,
        config: PhoneCodeConfig,
        *,
        secret: str,
        sms_sender: SmsSender,
        writer: CanonicalWriterAdmission,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(secret, str) or not secret:
            raise ValueError("The retained phone-code secret is required")
        self._store = store
        self._config = config
        self._secret = secret
        self._sms_sender = sms_sender
        self._writer = writer
        self._clock = clock

    def _now(self) -> int:
        now = self._clock()
        if not math.isfinite(now) or now < 0:
            raise ValueError("Invalid phone-code clock")
        return int(now * 1000)

    async def send_code(self, raw_phone: object) -> PhoneCodeSent:
        phone = normalize_phone(raw_phone)
        if phone is None:
            raise PhoneAuthError(400, "invalid_phone")
        code = f"{secrets.randbelow(1_000_000):06d}"
        await assert_canonical_writer(self._writer)
        reservation = await asyncio.get_running_loop().run_in_executor(
            None,
            self._store._reserve,
            phone,
            _hash_code(code, self._secret),
            self._now(),
            self._config,
        )
        await assert_canonical_writer(self._writer)
        try:
            sender: Callable[[str, str], Awaitable[object]] = self._sms_sender
            result = await sender(phone, code)
            if result is not None:
                raise TypeError("SMS sender did not follow its success contract")
        except Exception:
            await assert_canonical_writer(self._writer)
            await asyncio.get_running_loop().run_in_executor(
                None, self._store._discard_failed_send, reservation
            )
            raise PhoneAuthError(502, "sms_delivery_failed") from None
        return PhoneCodeSent(self._config.resend_cooldown_sec)

    async def verify_code(self, raw_phone: object, code: object) -> bool:
        phone = normalize_phone(raw_phone)
        if phone is None or not isinstance(code, str):
            return False
        trimmed = code.strip(_JS_WHITESPACE)
        if not re.fullmatch(r"[0-9]{4,8}", trimmed):
            return False
        await assert_canonical_writer(self._writer)
        return await asyncio.get_running_loop().run_in_executor(
            None,
            self._store._verify,
            phone,
            _hash_code(trimmed, self._secret),
            self._now(),
            self._config.max_verify_attempts,
        )
