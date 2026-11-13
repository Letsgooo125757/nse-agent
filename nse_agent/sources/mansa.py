"""Adapter for the Mansa Markets API (https://mansamarkets.com), NSE market list.

One request (or two, while the market has more than one page of stocks)
returns every NSE security with its latest price, change and volume. That
fits the free plan (100 requests a day) many times over.

What the free plan does NOT include: per-stock price history (Pro plan). The
database already holds history, and the daily job adds one close per day.

Quotes refresh about every 30 minutes (the response says so in
`meta.data_freshness`), so a run after the 15:00 EAT close gets the final
price. The snapshot's own timestamp is used to warn if it predates the close.

The API key is sent in the Authorization header only. It is never put in a
URL, a log line or an error message.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ..config import NAIROBI_TZ, get_settings
from ..models import PriceBar, PriceBatch
from .base import SourceError

SOURCE = "mansa"
EXCHANGE = "NSE"
PAGE_SIZE = 100
MAX_PAGES = 10  # safety stop: the NSE has fewer than 100 listings


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _when(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_page(payload: Any) -> tuple[list[PriceBar], datetime | None, bool]:
    """One page of the stock list -> (bars, snapshot time, has_more).

    The trade date is the Nairobi calendar date of the snapshot, so a
    snapshot taken after 15:00 EAT counts as that day's close.
    """
    if not isinstance(payload, dict) or not payload.get("success"):
        err = payload.get("error") if isinstance(payload, dict) else None
        msg = (err or {}).get("message") if isinstance(err, dict) else None
        raise SourceError(f"Mansa API refused the request: {msg or 'unexpected response'}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise SourceError("Mansa API response changed: no 'data' list")

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    stamp = _when(meta.get("updated_at"))
    bars: list[PriceBar] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        close = _dec(row.get("price"))
        if not ticker or close is None or close <= 0:
            continue
        row_stamp = _when(row.get("scraped_at")) or stamp
        stamp = max(filter(None, (stamp, row_stamp)), default=None)
        change = _dec(row.get("change"))
        volume = row.get("volume")
        volume = int(volume) if isinstance(volume, (int, float)) and not isinstance(volume, bool) else None
        traded = volume is not None and volume > 0
        # Same rule as the afx adapter: no trades means the price is carried forward.
        prev_close = (close - change) if (traded and change is not None) else (None if traded else close)
        bars.append(PriceBar(ticker=ticker, trade_date=date.min, close=close, prev_close=prev_close,
                             volume=volume if traded else 0, source=SOURCE))
    pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
    return bars, stamp, bool(pagination.get("has_more"))


def _get(base_url: str, api_key: str, path: str, params: dict[str, Any]) -> Any:
    try:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/{path.lstrip('/')}", params=params,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json",
                     "User-Agent": get_settings().user_agent},
            timeout=get_settings().http_timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise SourceError(f"Mansa API request failed: {type(exc).__name__}") from exc
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code >= 400 or not isinstance(body, dict):
        err = body.get("error") if isinstance(body, dict) else None
        msg = err.get("message") if isinstance(err, dict) else None
        hint = {401: "check MANSA_API_KEY", 403: "your plan may not include this"}.get(resp.status_code, "")
        raise SourceError(f"Mansa API HTTP {resp.status_code}: {msg or hint or 'request failed'}")
    return body


def fetch_market(base_url: str, api_key: str | None, *, fallback_date: date) -> PriceBatch:
    """Every NSE security's latest price, following the API's offset paging."""
    if not api_key:
        raise SourceError("MANSA_API_KEY is not set")
    bars: list[PriceBar] = []
    stamp: datetime | None = None
    offset = 0
    for _ in range(MAX_PAGES):
        page = _get(base_url, api_key, f"markets/exchanges/{EXCHANGE}/stocks",
                    {"limit": PAGE_SIZE, "offset": offset})
        page_bars, page_stamp, more = parse_page(page)
        bars += page_bars
        stamp = max(filter(None, (stamp, page_stamp)), default=None)
        offset += len(page.get("data") or [])
        if not more or not page.get("data"):
            break
    batch = PriceBatch(source=SOURCE)
    if stamp:
        local = stamp.astimezone(NAIROBI_TZ)
        batch.as_of = local.date()
        if local.weekday() < 5 and local.time() < dtime(15, 0):
            batch.warnings.append(
                f"Mansa snapshot is from {local:%H:%M} EAT, before the 15:00 close; "
                "prices may not be final (a later run replaces them)")
    else:
        batch.as_of = fallback_date
        batch.warnings.append("No snapshot time in the Mansa response; used the fetch date")
    batch.bars = [PriceBar(**{**b.__dict__, "trade_date": batch.as_of}) for b in bars]
    if not batch.bars:
        raise SourceError("Mansa API returned no usable prices")
    return batch
