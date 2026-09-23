"""Shared helpers for source adapters: HTTP fetching and forgiving parsers."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from ..config import get_settings

_SUFFIX = {"K": Decimal("1e3"), "M": Decimal("1e6"), "B": Decimal("1e9"), "T": Decimal("1e12"),
           "TR": Decimal("1e12"), "BN": Decimal("1e9"), "MN": Decimal("1e6")}
_EMPTY = {"", "-", "--", "—", "n/a", "na", "null", "none"}


class SourceError(RuntimeError):
    """Raised when a source can't be fetched or its format has changed."""


def fetch(url: str, *, timeout: float | None = None) -> bytes:
    s = get_settings()
    try:
        resp = httpx.get(
            url,
            timeout=timeout or s.http_timeout,
            headers={"User-Agent": s.user_agent},
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:  # network, TLS, 4xx/5xx
        raise SourceError(f"GET {url} failed: {exc}") from exc
    return resp.content


def parse_decimal(text: str | None) -> Decimal | None:
    """'1,234.50' -> 1234.50, '+0.10' -> 0.10, '40.1B' -> 40100000000, '' -> None."""
    if text is None:
        return None
    t = text.strip().replace(",", "").replace("KES", "").replace("Ksh", "").replace("%", "").strip()
    if t.lower() in _EMPTY:
        return None
    m = re.fullmatch(r"([+-]?\d*\.?\d+)\s*([A-Za-z]{0,2})", t)
    if not m:
        return None
    num, suffix = m.groups()
    try:
        value = Decimal(num)
    except InvalidOperation:
        return None
    if suffix:
        mult = _SUFFIX.get(suffix.upper())
        if mult is None:
            return None
        value *= mult
    return value


def parse_int(text: str | None) -> int | None:
    d = parse_decimal(text)
    return int(d) if d is not None else None


_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%d %b %Y",
                 "%d-%b-%y", "%b %d, %Y", "%B %d, %Y", "%A, %B %d, %Y", "%Y/%m/%d")


def parse_date(text: str) -> date:
    t = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {text!r}")


def norm_header(text: str) -> str:
    """'Change %' -> 'change%', ' Trade Date ' -> 'tradedate'."""
    return re.sub(r"[\s_]+", "", text.strip().lower())
