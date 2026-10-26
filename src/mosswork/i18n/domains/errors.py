"""``errors.*`` — API / MossworkError messages."""

from __future__ import annotations

from mosswork.i18n.loader import tr
from mosswork.infra.utils.locale import Locale

__all__ = ["error_message"]


def error_message(code: str, locale: str | Locale = "en", **kwargs: object) -> str:
    return tr(f"errors.{code}", locale, **kwargs)
