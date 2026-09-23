"""End-to-end tests against a real Postgres (see conftest for how one is found).

Network calls are replaced with the saved fixtures, so these exercise
parse -> load -> views exactly as a real run would.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import psycopg
import pytest
from psycopg.rows import dict_row

from nse_agent import cli
from nse_agent.sources import afx, rss

from .conftest import FIXTURES


@pytest.fixture()
def offline(monkeypatch):
    """Serve fixtures instead of hitting the network."""
    def fake_fetch(url, **_):
        if url.endswith("scom.html"):
            return (FIXTURES / "afx_scom.html").read_bytes()
        if "afx" in url:
            return (FIXTURES / "afx_market.html").read_bytes()
        if "capitalfm" in url:
            return (FIXTURES / "capitalfm.xml").read_bytes()
        raise rss.SourceError(f"offline: {url}")
    monkeypatch.setattr(afx, "fetch", fake_fetch)
    monkeypatch.setattr(rss, "fetch", fake_fetch)


def q(url, sql, *params):
    with psycopg.connect(url, row_factory=dict_row) as c:
        return c.execute(sql, params or None).fetchall()


def test_init_is_idempotent(db):
    assert cli.main(["init-db"]) == 0
    assert cli.main(["init-db"]) == 0
    n_csv = len((FIXTURES.parents[1] / "nse_agent/data/companies.csv").read_text().strip().splitlines()) - 1
    assert q(db, "SELECT count(*) AS n FROM companies")[0]["n"] == n_csv == 65


def test_ingest_prices_and_views(db, offline, capsys):
    assert cli.main(["ingest-prices", "--history", "--tickers", "SCOM", "--delay", "0"]) == 0
    out = capsys.readouterr().out
    assert "Skipped unknown tickers: ZZZZ" in out
    assert "Stale data" in out  # fixture is from 2024

    rows = q(db, "SELECT count(*) AS n FROM daily_prices")[0]["n"]
    assert rows == 9 + 5  # 9 known tickers from snapshot + 5 SCOM history days

    lp = {r["ticker"]: r for r in q(db, "SELECT * FROM v_latest_prices")}
    assert lp["KCB"]["close"] == Decimal("37.55")
    assert lp["KCB"]["change_pct"] == Decimal("-4.09")
    # SCOM: latest is the snapshot (2024-11-20), 52w high from history
    assert lp["SCOM"]["trade_date"] == date(2024, 11, 20)
    assert lp["SCOM"]["high_52w"] == Decimal("16.75")

    assert q(db, "SELECT value FROM index_values WHERE index_code='NASI'")[0]["value"] == Decimal("112.71")

    run = q(db, "SELECT * FROM v_pipeline_health WHERE job='prices'")[0]
    assert run["status"] == "partial" and run["rows_written"] == 15  # 14 bars + 1 index

    # Re-running must not duplicate anything
    assert cli.main(["ingest-prices"]) == 0
    assert q(db, "SELECT count(*) AS n FROM daily_prices")[0]["n"] == rows


def test_csv_import_keeps_richer_data(db, capsys):
    assert cli.main(["import-prices", str(FIXTURES / "prices_history.csv")]) == 0
    assert "3 rows written, 1 lines skipped" in capsys.readouterr().out
    row = q(db, "SELECT * FROM daily_prices WHERE ticker='SCOM' AND trade_date='2024-11-19'")[0]
    assert row["high"] == Decimal("15.85")


def test_snapshot_does_not_erase_ohlc(db, offline, tmp_path):
    # Load OHLC for 2024-11-20, then a close-only snapshot for the same day
    csv = tmp_path / "ohlc.csv"
    csv.write_text("date,ticker,open,high,low,close,volume\n2024-11-20,KCB,39,39.2,37.4,37.50,1\n")
    cli.main(["import-prices", str(csv)])
    cli.main(["ingest-prices"])
    r = q(db, "SELECT * FROM daily_prices WHERE ticker='KCB' AND trade_date='2024-11-20'")[0]
    assert r["close"] == Decimal("37.55") and r["high"] == Decimal("39.2")


def test_fundamentals_dividends_macro(db):
    cli.main(["import-prices", str(FIXTURES / "prices_history.csv")])
    assert cli.main(["import-financials", str(FIXTURES / "financials.csv")]) == 0
    assert cli.main(["import-dividends", str(FIXTURES / "dividends.csv")]) == 0
    assert cli.main(["import-macro", str(FIXTURES / "macro.csv")]) == 0

    v = {r["ticker"]: r for r in q(db, "SELECT * FROM v_valuation")}
    assert v["SCOM"]["pe_ratio"] == Decimal("9.43")          # 14.80 / 1.57
    assert v["SCOM"]["dividend_yield_pct"] == Decimal("8.11")  # 1.20 / 14.80
    assert v["SCOM"]["roe_pct"] == Decimal("10.00")

    up = q(db, "SELECT * FROM v_upcoming_dividends")
    assert [(r["ticker"], r["amount_per_share"]) for r in up] == [("SCOM", Decimal("0.6500"))]

    assert q(db, "SELECT count(*) AS n FROM macro_observations")[0]["n"] == 2
    health = {r["job"]: r for r in q(db, "SELECT * FROM v_pipeline_health")}
    assert health["import-macro"]["status"] == "partial"  # FOO series warned

    # importing twice is idempotent
    cli.main(["import-dividends", str(FIXTURES / "dividends.csv")])
    assert q(db, "SELECT count(*) AS n FROM corporate_actions")[0]["n"] == 2


def test_news_ingest_tagging_and_dedupe(db, offline, capsys):
    assert cli.main(["ingest-news"]) == 0  # google feeds fail offline, capitalfm works
    out = capsys.readouterr()
    assert "capitalfm_business: 3 items, 3 new" in out.out
    assert "FAILED" in out.err

    links = q(db, """SELECT a.title, l.ticker, l.relevance, l.in_title FROM news_ticker_links l
                     JOIN news_articles a ON a.id = l.article_id""")
    by_title = {}
    for r in links:
        by_title.setdefault(r["title"][:20], {})[r["ticker"]] = r
    stanchart = by_title["StanChart falls to t"]
    assert stanchart["SCBK"]["in_title"] and stanchart["SCBK"]["relevance"] >= Decimal("0.8")
    assert {"KCB", "EQTY", "COOP", "NCBA", "ABSA", "SBIC", "IMH", "DTK"} <= stanchart.keys()
    assert stanchart["KCB"]["relevance"] < stanchart["SCBK"]["relevance"]
    assert "SCOM" in by_title["Number of ATMs falls"]

    topics = q(db, "SELECT topics FROM news_articles WHERE title LIKE 'Number of ATMs%'")[0]["topics"]
    assert {"currency", "inflation", "banking_regulation"} <= set(topics)

    # second run: nothing new (URL with utm param is normalised)
    cli.main(["ingest-news", "--source", "capitalfm_business"])
    assert "3 items, 0 new" in capsys.readouterr().out
    assert q(db, "SELECT count(*) AS n FROM news_articles")[0]["n"] == 3

    failed = q(db, "SELECT last_error FROM news_sources WHERE source_code='gnews_nse'")[0]
    assert "offline" in failed["last_error"]

    n_links = len(links)
    assert cli.main(["retag-news"]) == 0
    assert q(db, "SELECT count(*) AS n FROM news_ticker_links")[0]["n"] == n_links


def test_failed_run_is_logged(db, monkeypatch):
    def boom(*a, **k):
        raise afx.SourceError("site down")
    monkeypatch.setattr(afx, "fetch", boom)
    assert cli.main(["ingest-prices"]) == 2
    run = q(db, "SELECT * FROM v_pipeline_health WHERE job='prices'")[0]
    assert run["status"] == "failed" and "site down" in run["error"]


def test_status_command(db, capsys):
    assert cli.main(["status"]) == 0
    assert "companies" in capsys.readouterr().out
