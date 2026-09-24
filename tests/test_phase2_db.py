"""Phase 2 end-to-end through the CLI against a real Postgres."""
from __future__ import annotations

import json
import subprocess
from decimal import Decimal

import psycopg
import pytest
from psycopg.rows import dict_row

from nse_agent import cli

from .conftest import FIXTURES

TEMPLATES = FIXTURES.parents[1] / "templates"


def q(url, sql, *params):
    with psycopg.connect(url, row_factory=dict_row) as c:
        return c.execute(sql, params or None).fetchall()


@pytest.fixture()
def setup(db, tmp_path):
    """Seeded DB + prices (SCOM 14.80, KCB 39.15 on 2024-11-19) + a profile."""
    assert cli.main(["import-prices", str(FIXTURES / "prices_history.csv")]) == 0
    divs = tmp_path / "divs.csv"
    divs.write_text("ticker,action_type,amount_per_share,book_closure_date,payment_date,financial_year\n"
                    "KCB,final_dividend,2.00,2024-11-28,2024-12-15,2023\n")
    assert cli.main(["import-dividends", str(divs)]) == 0
    assert cli.main(["profile", "create", "--from-file", str(TEMPLATES / "profile.json")]) == 0
    return db


def test_phase1_database_upgrades_in_place(database_url):
    """A DB created by the Phase 1 schema (with data) upgrades cleanly."""
    root = FIXTURES.parents[1]
    first = subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"], capture_output=True,
                           text=True, cwd=root).stdout.split()
    old = subprocess.run(["git", "show", f"{first[0] if first else 'HEAD'}:db/schema.sql"],
                         capture_output=True, text=True, cwd=root)
    if old.returncode or "other_holdings" in old.stdout:
        pytest.skip("Phase 1 schema not available from git history")
    with psycopg.connect(database_url, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.execute(old.stdout)
        c.execute("INSERT INTO investor_profiles(display_name) VALUES ('old')")
    from nse_agent.db import apply_schema, connect
    with connect(database_url) as conn:
        apply_schema(conn)
        apply_schema(conn)
    row = q(database_url, "SELECT * FROM investor_profiles")[0]
    assert row["display_name"] == "old" and row["max_stock_pct"] == 20


def test_profile_and_plan(setup, capsys):
    capsys.readouterr()
    assert cli.main(["profile", "show"]) == 0
    out = capsys.readouterr().out
    assert "Risk score" in out and "James" in out
    p = q(setup, "SELECT * FROM investor_profiles")[0]
    assert p["risk_tolerance"] in {"moderate", "aggressive"} and p["horizon_years"] == 15
    assert json.loads(json.dumps(p["questionnaire"]))["horizon"] == 4

    assert cli.main(["plan"]) == 0
    out = capsys.readouterr().out
    assert "Build your emergency fund" in out           # 50k of 240k target
    assert "not licensed investment advice" in out
    targets = {r["asset_class"]: r["target_pct"] for r in q(setup, "SELECT * FROM allocation_targets")}
    assert sum(targets.values()) == 100


def test_trades_dividends_bonus_and_portfolio(setup, capsys):
    assert cli.main(["buy", "SCOM", "1000", "15.50", "--date", "2024-11-18"]) == 0
    out = capsys.readouterr().out
    assert "Estimated fees KES 318.65" in out           # 15,500: 232.50+37.20+44.95+4
    assert cli.main(["buy", "KCB", "100", "38", "--fees", "80", "--date", "2024-11-18"]) == 0
    assert cli.main(["sell", "SCOM", "5000", "15", "--date", "2024-11-19"]) == 2   # oversell
    assert "only 1,000 held" in capsys.readouterr().err
    assert cli.main(["sell", "SCOM", "200", "15", "--date", "2024-11-19"]) == 0
    assert cli.main(["bonus", "KCB", "1", "10", "--date", "2024-11-19"]) == 0
    assert cli.main(["dividend", "KCB", "2", "--date", "2024-12-15"]) == 0
    assert "110 x KES 2 = KES 220.00 gross, tax 11.00" in capsys.readouterr().out
    assert cli.main(["holding", "set", "money_market", "CIC MMF", "30000", "--yield-pct", "13"]) == 0

    capsys.readouterr()
    assert cli.main(["portfolio"]) == 0
    out = capsys.readouterr().out
    assert "SCOM" in out and "KCB" in out and "Telecom" in out and "Banking" in out
    assert "Total tracked wealth" in out
    assert "dividends received (net) 209.00" in out

    from nse_agent.portfolio import snapshot
    from nse_agent.db import connect
    with connect(setup) as conn:
        s = snapshot(conn, 1)
    kcb = next(h for h in s.holdings if h.ticker == "KCB")
    assert kcb.shares == 110 and kcb.market_value == Decimal("110") * Decimal("39.15")
    assert s.other["emergency_cash"] == 50000 and s.other["money_market"] == 30000

    assert cli.main(["transactions"]) == 0
    assert cli.main(["delete-transaction", "3"]) == 0   # the SCOM sell


def test_import_transactions(setup, tmp_path, capsys):
    f = tmp_path / "tx.csv"
    f.write_text("date,ticker,side,quantity,price,fees,tax,notes\n"
                 "2024-11-18,SCOM,buy,500,15,,,first\n"
                 "2024-11-19,SCOM,sell,100,15.2,20,,\n"
                 "2024-11-19,SCOM,dividend,400,0.55,,,interim\n")
    assert cli.main(["import-transactions", str(f)]) == 0
    rows = q(setup, "SELECT side, fees, tax FROM transactions ORDER BY id")
    assert rows[0]["fees"] > 0 and rows[1]["fees"] == 20 and rows[2]["tax"] == Decimal("11.00")

    bad = tmp_path / "bad.csv"
    bad.write_text("date,ticker,side,quantity,price\n2024-11-19,KCB,sell,1,10\n")
    assert cli.main(["import-transactions", str(bad)]) == 2
    assert "line 2" in capsys.readouterr().err


def test_alerts_end_to_end(setup, capsys):
    cli.main(["buy", "SCOM", "1000", "15.50", "--date", "2024-11-18"])
    cli.main(["buy", "KCB", "100", "38", "--date", "2024-11-18"])
    cli.main(["watch", "EQTY"])
    cli.main(["alert-rule", "add", "SCOM", "below", "15"])
    assert cli.main(["alert-rule", "add", "SCOM"]) == 2           # incomplete
    capsys.readouterr()

    assert cli.main(["alerts", "run", "--date", "2024-11-20"]) == 0
    out = capsys.readouterr().out
    types = {r["alert_type"] for r in q(setup, "SELECT alert_type FROM alerts")}
    assert "price_rule" in types                 # SCOM 14.80 <= 15
    assert "big_move" in types                   # SCOM -6.3% on 11-19
    assert "book_closure" in types               # KCB closes 11-28, we hold 100
    assert "diversification" in types            # only 2 companies, SCOM ~80%
    assert "emergency_fund" in types
    assert "KES 200.00 gross" in out and "190.00 after" in out

    assert cli.main(["alerts", "run", "--date", "2024-11-20"]) == 0
    assert "No new alerts." in capsys.readouterr().out   # deduped

    assert cli.main(["alerts", "ack", "all"]) == 0
    assert cli.main(["alerts", "list"]) == 0
    assert "No open alerts." in capsys.readouterr().out


def test_no_profile_message(db, capsys):
    assert cli.main(["plan"]) == 2
    assert "profile create" in capsys.readouterr().err


def test_fees_and_companies_commands(db, capsys):
    assert cli.main(["fees", "10000"]) == 0
    assert "2.05%" in capsys.readouterr().out
    assert cli.main(["companies", "--sector", "banking"]) == 0
    assert "EQTY" in capsys.readouterr().out
