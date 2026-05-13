"""TCG set exclusions shared by catalog, drops, and pack series sync."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement


# McDonald's promo sets mostly duplicate existing cards/art and do not make interesting packs.
EXCLUDED_SET_CODE_PREFIXES: tuple[str, ...] = ("mcd",)


def is_excluded_set_code(set_code: str | None) -> bool:
    code = (set_code or "").strip().lower()
    return any(code.startswith(prefix) for prefix in EXCLUDED_SET_CODE_PREFIXES)


def filter_excluded_set_codes(set_codes: Iterable[str]) -> list[str]:
    return [code for code in set_codes if code and not is_excluded_set_code(code)]


def excluded_set_clause(set_code_column: ColumnElement[str]) -> ColumnElement[bool]:
    """SQLAlchemy filter that removes excluded TCG sets from card queries."""
    lowered = func.lower(set_code_column)
    clause: ColumnElement[bool] | None = None
    for prefix in EXCLUDED_SET_CODE_PREFIXES:
        next_clause = lowered.like(f"{prefix}%")
        clause = next_clause if clause is None else clause | next_clause
    if clause is None:
        return lowered != ""
    return ~clause
