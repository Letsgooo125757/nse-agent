"""Plain data records passed from sources to loaders.

Sources only *produce* these; they never touch the database. That keeps each
adapter easy to test with a saved HTML/CSV/XML fixture.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class PriceBar:
    ticker: str
    trade_date: date
    close: Decimal
    source: str
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    prev_close: Decimal | None = None
    volume: int | None = None
    turnover: Decimal | None = None
    vwap: Decimal | None = None


@dataclass(frozen=True)
class IndexValue:
    index_code: str
    trade_date: date
    value: Decimal
    source: str


@dataclass
class PriceBatch:
    """Everything one price fetch produced, plus anything worth flagging."""
    source: str
    bars: list[PriceBar] = field(default_factory=list)
    indices: list[IndexValue] = field(default_factory=list)
    as_of: date | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NewsItem:
    source_code: str
    url: str
    title: str
    summary: str | None = None
    content: str | None = None
    author: str | None = None
    categories: tuple[str, ...] = ()
    published_at: datetime | None = None


@dataclass(frozen=True)
class MacroObservation:
    series_code: str
    obs_date: date
    value: Decimal
