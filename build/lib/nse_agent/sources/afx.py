"""Adapter for afx.kwayisi.org — a free public mirror of NSE prices.

Two pages are used:
  * the market page (/nse/)            -> one bar per listed security + NASI
  * a per-stock page (/nse/<tkr>.html) -> the last ~10 trading days (backfill)

This is a convenience source for a personal project. For anything you
distribute to other people, NSE requires a data licence (see README).

Parsers locate tables by their *header text*, not by CSS classes or position,
so cosmetic site changes don't silently shift columns. If the expected
headers disappear, a SourceError is raised instead of loading garbage.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..models import IndexValue, PriceBar, PriceBatch
from .base import SourceError, fetch, norm_header, parse_date, parse_decimal, parse_int

SOURCE = "afx"

_SUMMARY_DATE_RE = re.compile(
    r"TRADING SUMMARY FOR\s+(?:[A-Z]+DAY,\s*)?([A-Z]+\s+\d{1,2},\s*\d{4})", re.IGNORECASE
)
_NASI_CLOSE_RE = re.compile(r"\(NASI\).{0,160}?close at\s+([\d,]+(?:\.\d+)?)", re.IGNORECASE)


def _header_cells(table: Tag) -> list[str]:
    head_row = None
    thead = table.find("thead")
    if thead:
        head_row = thead.find("tr")
    if head_row is None:
        head_row = table.find("tr")
    if head_row is None:
        return []
    return [norm_header(c.get_text(" ", strip=True)) for c in head_row.find_all(["th", "td"])]


def _find_table(soup: BeautifulSoup, required: set[str]) -> tuple[Tag, list[str]]:
    for table in soup.find_all("table"):
        headers = _header_cells(table)
        if required.issubset(headers):
            return table, headers
    raise SourceError(f"No table with columns {sorted(required)} — page layout may have changed")


def _body_rows(table: Tag) -> list[list[str]]:
    tbody = table.find("tbody")
    rows = (tbody or table).find_all("tr")
    out = []
    for tr in rows:
        cells = tr.find_all("td")
        if not cells:  # header row
            continue
        out.append([c.get_text(" ", strip=True) for c in cells])
    return out


def parse_market_page(html: str | bytes, *, fallback_date: date) -> PriceBatch:
    """Parse the all-securities table. `fallback_date` is used only if the page
    carries no trading-session date (a warning is added when that happens)."""
    soup = BeautifulSoup(html, "html.parser")
    batch = PriceBatch(source=SOURCE)
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    m = _SUMMARY_DATE_RE.search(text)
    if m:
        batch.as_of = parse_date(m.group(1).title())
    else:
        batch.as_of = fallback_date
        batch.warnings.append("No trading-session date on page; used fetch date")

    table, headers = _find_table(soup, {"ticker", "price"})
    idx = {h: i for i, h in enumerate(headers)}

    for cells in _body_rows(table):
        if len(cells) < len(headers):
            cells = cells + [""] * (len(headers) - len(cells))
        ticker = cells[idx["ticker"]].strip().upper()
        close = parse_decimal(cells[idx["price"]])
        if not ticker or close is None:
            continue
        volume = parse_int(cells[idx["volume"]]) if "volume" in idx else None
        change = parse_decimal(cells[idx["change"]]) if "change" in idx else None
        # A blank volume means the security did not trade that session: NSE
        # carries the previous price forward, and so do we (volume 0).
        traded = volume is not None and volume > 0
        prev_close = (close - change) if (traded and change is not None) else (None if traded else close)
        batch.bars.append(PriceBar(
            ticker=ticker, trade_date=batch.as_of, close=close,
            prev_close=prev_close, volume=volume if traded else 0, source=SOURCE,
        ))

    if not batch.bars:
        raise SourceError("Market table found but no price rows parsed")

    nasi = _NASI_CLOSE_RE.search(text)
    if nasi:
        batch.indices.append(IndexValue("NASI", batch.as_of, parse_decimal(nasi.group(1)), SOURCE))
    return batch


def parse_stock_page(html: str | bytes, ticker: str) -> list[PriceBar]:
    """Parse the 'last N trading days' table on a single-stock page."""
    soup = BeautifulSoup(html, "html.parser")
    table, headers = _find_table(soup, {"date", "close"})
    idx = {h: i for i, h in enumerate(headers)}
    bars: list[PriceBar] = []
    for cells in _body_rows(table):
        try:
            d = parse_date(cells[idx["date"]])
        except (ValueError, IndexError):
            continue
        close = parse_decimal(cells[idx["close"]])
        if close is None:
            continue
        change = parse_decimal(cells[idx["change"]]) if "change" in idx else None
        bars.append(PriceBar(
            ticker=ticker.upper(), trade_date=d, close=close,
            prev_close=(close - change) if change is not None else None,
            volume=parse_int(cells[idx["volume"]]) if "volume" in idx else None,
            source=SOURCE,
        ))
    return bars


def fetch_market(base_url: str, *, fallback_date: date) -> PriceBatch:
    return parse_market_page(fetch(base_url), fallback_date=fallback_date)


def fetch_stock_history(base_url: str, ticker: str) -> list[PriceBar]:
    url = urljoin(base_url, f"{ticker.lower()}.html")
    return parse_stock_page(fetch(url), ticker)
