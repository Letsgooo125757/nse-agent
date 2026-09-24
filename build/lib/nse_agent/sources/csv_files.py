"""CSV importers: historical prices, fundamentals, dividends and macro series.

Historical data you buy from NSE, export from a broker, or collect by hand all
arrive as spreadsheets, so these importers accept common header spellings.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from ..models import MacroObservation, PriceBar
from .base import norm_header, parse_date, parse_decimal, parse_int

# canonical field -> accepted (normalised) header spellings
PRICE_COLUMNS = {
    "ticker":     {"ticker", "code", "symbol", "stock", "tradingcode"},
    "trade_date": {"date", "tradedate", "day"},
    "open":       {"open", "openingprice"},
    "high":       {"high", "dayhigh", "highprice"},
    "low":        {"low", "daylow", "lowprice"},
    "close":      {"close", "closingprice", "price", "last", "adjclose"},
    "prev_close": {"prevclose", "previous", "previousclose", "prev"},
    "volume":     {"volume", "vol", "sharestraded"},
    "turnover":   {"turnover", "value", "valuetraded"},
    "vwap":       {"vwap", "averageprice", "avgprice"},
}


class CsvFormatError(ValueError):
    pass


@dataclass
class ImportResult:
    rows: list[Any]
    skipped: list[str]  # human-readable reasons, capped for display by the caller


def _map_headers(fieldnames: Iterable[str], spec: dict[str, set[str]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in fieldnames:
        n = norm_header(raw)
        for canon, spellings in spec.items():
            if n in spellings and canon not in mapping:
                mapping[canon] = raw
    return mapping


def read_prices_csv(path: Path, *, ticker: str | None = None, source: str = "csv") -> ImportResult:
    """Rows need a date and a close; ticker comes from a column or the `ticker` arg."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = _map_headers(reader.fieldnames or [], PRICE_COLUMNS)
        missing = {"trade_date", "close"} - cols.keys()
        if missing:
            raise CsvFormatError(f"{path.name}: missing column(s) {sorted(missing)}; found {reader.fieldnames}")
        if "ticker" not in cols and not ticker:
            raise CsvFormatError(f"{path.name}: no ticker column — pass --ticker")

        bars, skipped = [], []
        for lineno, row in enumerate(reader, start=2):
            get = lambda k: row.get(cols[k]) if k in cols else None  # noqa: E731
            t = (get("ticker") or ticker or "").strip().upper()
            try:
                d = parse_date(get("trade_date") or "")
            except ValueError as exc:
                skipped.append(f"line {lineno}: {exc}")
                continue
            close = parse_decimal(get("close"))
            if not t or close is None:
                skipped.append(f"line {lineno}: missing ticker or close")
                continue
            bars.append(PriceBar(
                ticker=t, trade_date=d, close=close, source=source,
                open=parse_decimal(get("open")), high=parse_decimal(get("high")),
                low=parse_decimal(get("low")), prev_close=parse_decimal(get("prev_close")),
                volume=parse_int(get("volume")), turnover=parse_decimal(get("turnover")),
                vwap=parse_decimal(get("vwap")),
            ))
    return ImportResult(bars, skipped)


def read_macro_csv(path: Path) -> ImportResult:
    """Columns: series_code, date, value (e.g. CBK_CBR,2026-08-12,9.25)."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = _map_headers(reader.fieldnames or [], {
            "series_code": {"seriescode", "series", "code"},
            "obs_date": {"date", "obsdate"},
            "value": {"value", "rate", "yield"},
        })
        if len(cols) < 3:
            raise CsvFormatError(f"{path.name}: need series_code,date,value columns")
        obs, skipped = [], []
        for lineno, row in enumerate(reader, start=2):
            try:
                d = parse_date(row[cols["obs_date"]])
            except ValueError as exc:
                skipped.append(f"line {lineno}: {exc}")
                continue
            v = parse_decimal(row[cols["value"]])
            if v is None:
                skipped.append(f"line {lineno}: no value")
                continue
            obs.append(MacroObservation(row[cols["series_code"]].strip().upper(), d, v))
    return ImportResult(obs, skipped)


FINANCIAL_FIELDS = ("revenue", "operating_profit", "profit_before_tax", "net_income",
                    "total_assets", "total_liabilities", "total_equity",
                    "operating_cash_flow", "eps", "dps")


def read_financials_csv(path: Path) -> ImportResult:
    """Columns: ticker, period_end, period_type (FY/H1/Q1/Q3), then any of
    FINANCIAL_FIELDS, shares_outstanding, currency, source_url. Amounts are in
    full units (KES, not KES '000) — convert before importing."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = {norm_header(h): h for h in (reader.fieldnames or [])}
        for req in ("ticker", "periodend", "periodtype"):
            if req not in header:
                raise CsvFormatError(f"{path.name}: missing column {req}")
        rows, skipped = [], []
        for lineno, row in enumerate(reader, start=2):
            try:
                rec: dict[str, Any] = {
                    "ticker": row[header["ticker"]].strip().upper(),
                    "period_end": parse_date(row[header["periodend"]]),
                    "period_type": row[header["periodtype"]].strip().upper(),
                    "currency": (row.get(header.get("currency", ""), "") or "KES").strip() or "KES",
                    "source_url": (row.get(header.get("sourceurl", ""), "") or "").strip() or None,
                }
            except ValueError as exc:
                skipped.append(f"line {lineno}: {exc}")
                continue
            if rec["period_type"] not in {"FY", "H1", "Q1", "Q3"}:
                skipped.append(f"line {lineno}: bad period_type {rec['period_type']!r}")
                continue
            for f in FINANCIAL_FIELDS:
                rec[f] = parse_decimal(row.get(header.get(norm_header(f), ""), None))
            rec["shares_outstanding"] = parse_int(row.get(header.get("sharesoutstanding", ""), None))
            rows.append(rec)
    return ImportResult(rows, skipped)


def read_dividends_csv(path: Path) -> ImportResult:
    """Columns: ticker, action_type, amount_per_share, book_closure_date,
    payment_date, announced_date, financial_year, source_url (only the first
    three are required)."""
    valid_types = {"interim_dividend", "final_dividend", "special_dividend"}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = {norm_header(h): h for h in (reader.fieldnames or [])}
        for req in ("ticker", "actiontype", "amountpershare"):
            if req not in header:
                raise CsvFormatError(f"{path.name}: missing column {req}")

        def opt_date(row: dict, key: str) -> date | None:
            raw = (row.get(header.get(key, ""), "") or "").strip()
            return parse_date(raw) if raw else None

        rows, skipped = [], []
        for lineno, row in enumerate(reader, start=2):
            action = row[header["actiontype"]].strip().lower()
            if action not in valid_types:
                skipped.append(f"line {lineno}: action_type must be one of {sorted(valid_types)}")
                continue
            try:
                rec = {
                    "ticker": row[header["ticker"]].strip().upper(),
                    "action_type": action,
                    "amount_per_share": parse_decimal(row[header["amountpershare"]]),
                    "book_closure_date": opt_date(row, "bookclosuredate"),
                    "payment_date": opt_date(row, "paymentdate"),
                    "announced_date": opt_date(row, "announceddate"),
                    "financial_year": parse_int(row.get(header.get("financialyear", ""), None)),
                    "source_url": (row.get(header.get("sourceurl", ""), "") or "").strip() or None,
                }
            except ValueError as exc:
                skipped.append(f"line {lineno}: {exc}")
                continue
            if rec["amount_per_share"] is None or rec["amount_per_share"] < Decimal(0):
                skipped.append(f"line {lineno}: bad amount_per_share")
                continue
            rows.append(rec)
    return ImportResult(rows, skipped)
