"""Mansa Markets price source: parsing, paging, errors, key safety and the CLI wiring."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from nse_agent import cli
from nse_agent.sources import afx, mansa

from .conftest import FIXTURES

KEY = "mansa_live_sk_TESTKEY0123456789abcdefgh"
# 12:20 UTC = 15:20 Nairobi: after the close. 09:45 UTC = 12:45 Nairobi: before it.
AFTER_CLOSE, BEFORE_CLOSE = "2026-11-13T12:20:10.122+00:00", "2026-11-13T09:45:10.122+00:00"


def row(ticker, price, change=0.0, volume=1000, stamp=AFTER_CLOSE):
    return {"ticker": ticker, "name": ticker, "price": price, "change": change,
            "change_pct": 0, "volume": volume, "scraped_at": stamp, "currency": "KES"}


def page(rows, *, stamp=AFTER_CLOSE, total=None, offset=0, more=False):
    return {"success": True, "data": rows,
            "meta": {"exchange": "NSE", "currency": "KES", "price_unit": "major",
                     "updated_at": stamp, "data_freshness": "30_minutes", "source": "mansa_api"},
            "pagination": {"total": total or len(rows), "limit": 100, "offset": offset, "has_more": more}}


def test_parse_page_prices_and_carry_forward():
    payload = page([row("SCOM", 34.95, -0.05, 93935), row("BKG", 62, 0, 0), row("ABSA", 31.95, 0.95, 37150)])
    bars, stamp, more = mansa.parse_page(payload)
    by = {b.ticker: b for b in bars}
    assert by["SCOM"].close == Decimal("34.95") and by["SCOM"].prev_close == Decimal("35.00")
    assert by["SCOM"].volume == 93935 and by["SCOM"].source == "mansa"
    assert by["ABSA"].prev_close == Decimal("31.00")
    # Did not trade: the price is carried forward with volume 0, as with afx.
    assert by["BKG"].volume == 0 and by["BKG"].prev_close == Decimal("62")
    assert stamp is not None and more is False


def test_parse_page_drops_bad_rows():
    payload = page([row("SCOM", 34.95), row("", 10), row("BAD", None), row("ZERO", 0), "junk", row("KCB", True)])
    bars, _, _ = mansa.parse_page(payload)
    assert [b.ticker for b in bars] == ["SCOM"]


@pytest.mark.parametrize("payload", [None, [], {"success": True}, {"success": True, "data": "x"}])
def test_parse_page_layout_change_raises(payload):
    with pytest.raises(mansa.SourceError):
        mansa.parse_page(payload)


def test_parse_page_error_body_surfaces_message():
    body = {"success": False, "error": {"code": "TIER_REQUIRED", "message": "This endpoint requires the Pro plan"}}
    with pytest.raises(mansa.SourceError, match="requires the Pro plan"):
        mansa.parse_page(body)


def test_fetch_market_follows_paging_and_dates_in_nairobi(monkeypatch):
    calls = []
    pages = [page([row("SCOM", 34.95), row("KCB", 50)], total=3, more=True),
             page([row("EQTY", 60)], total=3, offset=2)]

    def fake_get(base, key, path, params):
        calls.append((path, dict(params)))
        return pages[len(calls) - 1]

    monkeypatch.setattr(mansa, "_get", fake_get)
    batch = mansa.fetch_market("https://x/api/v1", KEY, fallback_date=date(2030, 1, 1))
    assert [c[1]["offset"] for c in calls] == [0, 2]
    assert all(c[0] == "markets/exchanges/NSE/stocks" for c in calls)
    assert sorted(b.ticker for b in batch.bars) == ["EQTY", "KCB", "SCOM"]
    assert batch.as_of == date(2026, 11, 13) and not batch.warnings
    assert {b.trade_date for b in batch.bars} == {date(2026, 11, 13)}


def test_late_utc_snapshot_is_dated_by_nairobi_day(monkeypatch):
    # 22:30 UTC on Thursday is 01:30 Friday in Nairobi.
    stamp = "2026-11-12T22:30:00+00:00"
    monkeypatch.setattr(mansa, "_get", lambda *a: page([row("SCOM", 35, stamp=stamp)], stamp=stamp))
    assert mansa.fetch_market("u", KEY, fallback_date=date(2030, 1, 1)).as_of == date(2026, 11, 13)


def test_snapshot_before_close_warns(monkeypatch):
    monkeypatch.setattr(mansa, "_get", lambda *a: page([row("SCOM", 35, stamp=BEFORE_CLOSE)], stamp=BEFORE_CLOSE))
    batch = mansa.fetch_market("u", KEY, fallback_date=date(2030, 1, 1))
    assert any("before the 15:00 close" in w for w in batch.warnings)


def test_paging_stops_if_the_api_keeps_saying_more(monkeypatch):
    n = []
    monkeypatch.setattr(mansa, "_get", lambda *a: n.append(1) or page([row("SCOM", 35)], more=True))
    mansa.fetch_market("u", KEY, fallback_date=date(2030, 1, 1))
    assert len(n) == mansa.MAX_PAGES


def test_missing_key_is_a_clear_error():
    with pytest.raises(mansa.SourceError, match="MANSA_API_KEY is not set"):
        mansa.fetch_market("u", "", fallback_date=date.today())


def _http(monkeypatch, status, body):
    seen = {}

    def fake_get(url, **kw):
        seen.update(url=url, **kw)
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    return seen


def test_key_goes_in_the_header_only_and_never_in_errors(monkeypatch):
    seen = _http(monkeypatch, 403, {"success": False, "error": {"code": "TIER_REQUIRED",
                                                                "message": "Requires the Pro plan"}})
    with pytest.raises(mansa.SourceError) as err:
        mansa.fetch_market("https://mansaapi.com/api/v1", KEY, fallback_date=date.today())
    assert KEY not in str(err.value) and "Pro plan" in str(err.value) and "403" in str(err.value)
    assert seen["headers"]["Authorization"] == f"Bearer {KEY}"
    assert KEY not in seen["url"] and KEY not in str(seen["params"])


def test_non_json_error_page_is_handled(monkeypatch):
    seen = {}
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
        404, text="<html>Page not found</html>", request=httpx.Request("GET", url)))
    with pytest.raises(mansa.SourceError, match="HTTP 404"):
        mansa.fetch_market("https://mansaapi.com/api/v1", KEY, fallback_date=date.today())


def test_settings_hide_the_key_in_repr(monkeypatch):
    monkeypatch.setenv("MANSA_API_KEY", f"  {KEY}  ")
    from nse_agent.config import get_settings
    s = get_settings()
    assert s.mansa_api_key == KEY and KEY not in repr(s)


# ------------------------------------------------------------------ with a database


def q(url, sql, *params):
    with psycopg.connect(url, row_factory=dict_row) as c:
        return c.execute(sql, params or None).fetchall()


@pytest.fixture()
def with_key(monkeypatch):
    monkeypatch.setenv("MANSA_API_KEY", KEY)
    # ABSA is in companies.csv; ZZZZ is not and must be skipped with a warning.
    monkeypatch.setattr(mansa, "_get", lambda *a: page(
        [row("SCOM", 34.95, -0.05, 93935), row("ABSA", 31.95, 0.95, 37150), row("ZZZZ", 5)]))
    # afx is unreachable unless a test says otherwise (as on GitHub); never hit the network.
    monkeypatch.setattr(afx, "fetch", lambda *a, **k: (_ for _ in ()).throw(afx.SourceError("offline")))


def test_ingest_prices_uses_mansa_when_a_key_is_set(db, with_key, capsys):
    assert cli.main(["ingest-prices"]) == 0
    assert "from mansa" in capsys.readouterr().out
    rows = {r["ticker"]: r for r in q(db, "SELECT * FROM daily_prices")}
    assert set(rows) == {"SCOM", "ABSA"} and rows["SCOM"]["source"] == "mansa"
    assert rows["SCOM"]["close"] == Decimal("34.9500") and rows["SCOM"]["trade_date"] == date(2026, 11, 13)
    run = q(db, "SELECT * FROM v_pipeline_health WHERE job='prices'")[0]
    assert run["source"] == "mansa" and "ZZZZ" in " ".join(run["warnings"])


def test_a_later_mansa_run_replaces_the_same_days_price(db, with_key, monkeypatch):
    cli.main(["ingest-prices"])
    monkeypatch.setattr(mansa, "_get", lambda *a: page([row("SCOM", 35.40, 0.40, 120000)]))
    cli.main(["ingest-prices"])
    scom = q(db, "SELECT close, volume FROM daily_prices WHERE ticker='SCOM'")
    assert len(scom) == 1 and scom[0]["close"] == Decimal("35.4000") and scom[0]["volume"] == 120000


def test_auto_falls_back_to_afx_when_mansa_fails(db, monkeypatch, capsys):
    monkeypatch.setenv("MANSA_API_KEY", KEY)

    def down(*a):
        raise mansa.SourceError("Mansa API HTTP 503: unavailable")

    monkeypatch.setattr(mansa, "_get", down)
    monkeypatch.setattr(afx, "fetch", lambda url, **k: (FIXTURES / "afx_market.html").read_bytes())
    assert cli.main(["ingest-prices"]) == 0
    out = capsys.readouterr()
    assert "from afx" in out.out and "trying afx" in out.err
    run = q(db, "SELECT * FROM v_pipeline_health WHERE job='prices'")[0]
    assert run["status"] == "partial" and any("Mansa failed" in w for w in run["warnings"])
    assert {r["source"] for r in q(db, "SELECT source FROM daily_prices")} == {"afx"}


def test_explicit_mansa_source_does_not_fall_back(db, monkeypatch):
    monkeypatch.setenv("MANSA_API_KEY", KEY)
    monkeypatch.setattr(mansa, "_get", lambda *a: (_ for _ in ()).throw(mansa.SourceError("Mansa API HTTP 401")))
    assert cli.main(["ingest-prices", "--source", "mansa"]) == 2
    run = q(db, "SELECT * FROM v_pipeline_health WHERE job='prices'")[0]
    assert run["status"] == "failed" and "401" in run["error"]


def test_explicit_afx_ignores_the_key(db, monkeypatch):
    monkeypatch.setenv("MANSA_API_KEY", KEY)
    monkeypatch.setattr(mansa, "_get", lambda *a: pytest.fail("Mansa must not be called"))
    monkeypatch.setattr(afx, "fetch", lambda url, **k: (FIXTURES / "afx_market.html").read_bytes())
    assert cli.main(["ingest-prices", "--source", "afx"]) == 0


def test_compare_prices_reports_differences_and_writes_nothing(db, with_key, monkeypatch, capsys):
    monkeypatch.setattr(afx, "fetch", lambda url, **k: (FIXTURES / "afx_market.html").read_bytes())
    assert cli.main(["compare-prices"]) == 0
    out = capsys.readouterr().out
    assert "afx as of" in out and "mansa as of 2026-11-13" in out and "Same close" in out
    assert q(db, "SELECT count(*) AS n FROM daily_prices")[0]["n"] == 0


def test_auto_tops_up_securities_mansa_lacks_from_afx(db, with_key, monkeypatch, capsys):
    monkeypatch.setattr(afx, "fetch", lambda url, **k: (FIXTURES / "afx_market.html").read_bytes())
    assert cli.main(["ingest-prices"]) == 0
    assert "taken from afx" in capsys.readouterr().out
    rows = {r["ticker"]: r for r in q(db, "SELECT ticker, source, close FROM daily_prices")}
    assert rows["SCOM"]["source"] == "mansa" and rows["SCOM"]["close"] == Decimal("34.9500")  # Mansa wins
    assert rows["ABSA"]["source"] == "mansa"
    afx_only = {t for t, r in rows.items() if r["source"] == "afx"}
    assert afx_only and "SCOM" not in afx_only and "ABSA" not in afx_only
    assert q(db, "SELECT count(*) AS n FROM index_values")[0]["n"] == 1  # NASI, from afx


def test_unreachable_afx_is_not_an_error_or_a_warning(db, with_key, capsys):
    assert cli.main(["ingest-prices"]) == 0          # with_key makes afx unreachable
    assert "afx not reachable" in capsys.readouterr().out
    run = q(db, "SELECT * FROM v_pipeline_health WHERE job='prices'")[0]
    assert not any("afx" in w.lower() for w in run["warnings"])   # a note on screen, not a warning


def test_explicit_mansa_source_never_calls_afx(db, with_key, monkeypatch):
    monkeypatch.setattr(afx, "fetch", lambda *a, **k: pytest.fail("afx must not be called"))
    assert cli.main(["ingest-prices", "--source", "mansa"]) == 0


def test_quickmart_is_a_known_company_so_its_prices_are_kept(db, monkeypatch):
    """QMRT is not on Mansa; its price comes from afx and must not be skipped as unknown."""
    monkeypatch.setenv("MANSA_API_KEY", KEY)
    monkeypatch.setattr(mansa, "_get", lambda *a: page([row("SCOM", 34.95)]))
    afx_html = (FIXTURES / "afx_market.html").read_text()
    row_html = "<tr><td>QMRT</td><td>Quick Mart Plc</td><td>20,000</td><td>7.50</td><td>+0.10</td></tr>"
    head, tbody, tail = afx_html.rpartition("</tbody>")  # the stock table is the last one
    monkeypatch.setattr(afx, "fetch", lambda url, **k: (head + row_html + tbody + tail).encode())
    assert cli.main(["ingest-prices"]) == 0
    assert q(db, "SELECT sector_code FROM companies WHERE ticker='QMRT'")[0]["sector_code"] == "COMMERCIAL"
    qm = q(db, "SELECT source, close FROM daily_prices WHERE ticker='QMRT'")
    assert qm == [{"source": "afx", "close": Decimal("7.5000")}]
