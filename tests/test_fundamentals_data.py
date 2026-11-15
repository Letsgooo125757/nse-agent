"""The shipped fundamentals CSVs: every row must load, belong to a listed company and be internally sane."""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from nse_agent import cli
from nse_agent.sources import csv_files

ROOT = Path(__file__).resolve().parents[1]
FUND = sorted((ROOT / "data" / "fundamentals").glob("financials_*.csv"))
TICKERS = {r["ticker"] for r in csv.DictReader((ROOT / "nse_agent/data/companies.csv").open())}


def q(url, sql, *p):
    with psycopg.connect(url, row_factory=dict_row) as c:
        return c.execute(sql, p or None).fetchall()


@pytest.mark.parametrize("path", FUND, ids=lambda p: p.name)
def test_every_row_loads_and_is_sane(path):
    res = csv_files.read_financials_csv(path)
    assert not res.skipped, res.skipped
    seen = set()
    for r in res.rows:
        t, key = r["ticker"], (r["ticker"], r["period_end"], r["period_type"])
        assert t in TICKERS, f"{t} is not in companies.csv"
        assert key not in seen, f"duplicate row {key}"
        seen.add(key)
        assert r["source_url"] and r["source_url"].startswith("http"), f"{key} has no source_url"
        assert r["period_end"] <= date.today()
        ta, tl, te = r["total_assets"], r["total_liabilities"], r["total_equity"]
        if ta is not None and tl is not None and te is not None:
            # equity attributable to owners can sit below assets - liabilities (non-controlling interests)
            assert te <= ta - tl + ta * Decimal("0.005"), f"{key}: equity exceeds assets - liabilities"
        if ta is not None and te is not None and te > 0:
            assert te < ta, f"{key}: equity is not below assets"
        if r["eps"] is not None and r["net_income"] is not None and r["eps"] != 0:
            assert (r["eps"] > 0) == (r["net_income"] > 0), f"{key}: EPS and net income disagree in sign"
        if r["profit_before_tax"] is not None and r["net_income"] is not None and r["profit_before_tax"] > 0:
            assert r["net_income"] < r["profit_before_tax"] * Decimal("1.05"), f"{key}: profit after tax above pre-tax profit"
        assert r["dps"] is None or r["dps"] >= 0


def test_all_shipped_financials_import_cleanly(db):
    for path in FUND:
        assert cli.main(["import-financials", str(path)]) == 0
    assert q(db, "SELECT count(*) AS n FROM financial_statements")[0]["n"] >= 40


def test_valuation_view_hides_meaningless_ratios_for_loss_makers(db):
    cli.main(["import-financials", str(ROOT / "data/fundamentals/financials_2026-11-batch1.csv")])
    with psycopg.connect(db) as c:
        c.execute("""INSERT INTO daily_prices(ticker, trade_date, close, volume, source)
                     VALUES ('KQ','2026-11-13',4.5,100,'t'), ('BAT','2026-11-13',400,100,'t')""")
    v = {r["ticker"]: r for r in q(db, "SELECT * FROM v_valuation WHERE ticker IN ('KQ','BAT')")}
    assert v["KQ"]["pe_ratio"] is None and v["KQ"]["roe_pct"] is None and v["KQ"]["payout_ratio"] is None
    assert v["BAT"]["pe_ratio"] == Decimal("7.62") and v["BAT"]["roe_pct"] is not None   # 400 / 52.46 = 7.62
