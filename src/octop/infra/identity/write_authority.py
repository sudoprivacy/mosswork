"""Admission contract for the retained canonical identity writer.

The trusted composition must establish original-writer drain and exclusive
takeover before injecting an implementation. This check does not create a
lease, fence another process, or authorize a request-selected database. The
implementation must preserve exclusivity across the admitted operation and
any awaited side effect; an arbitrary local flag is not takeover evidence.
"""

from collections.abc import Awaitable, Callable
from typing import Protocol

from octop.infra.errors import ErrorCode, MossworkError


class CanonicalWriterAdmission(Protocol):
    async def assert_current_writer(self) -> None:
        """Refuse unless this composition still owns the canonical writer."""
        ...


async def assert_canonical_writer(admission: CanonicalWriterAdmission) -> None:
    """Accept only the trusted assertion's strict None success contract."""
    try:
        assertion: Callable[[], Awaitable[object]] = admission.assert_current_writer
        result = await assertion()
        if result is None:
            return
    except Exception:
        pass
    raise MossworkError.localized(
        ErrorCode.INTERNAL_ERROR,
        status=503,
        details={"dependency": "canonical_identity_writer"},
    ) from None
