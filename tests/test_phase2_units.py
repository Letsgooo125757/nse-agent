"""Pure-logic tests for Phase 2 (no database)."""
from datetime import date
from decimal import Decimal as D

import pytest

from nse_agent import alerts, planner
from nse_agent.fees import FeeSchedule, dividend_tax, trade_fees
from nse_agent.investor import QUESTIONS, Profile, profile_from_dict, score_answers, tolerance_for
from nse_agent.portfolio import PortfolioError, Txn, build_positions, shares_held_on, summarise

# ---------------------------------------------------------------- fees


def test_fees_match_published_example():
    # nsecalc.co.ke: KES 10,000 trade at 1.5% brokerage ~ KES 205
    fb = trade_fees(D("10000"), FeeSchedule())
    assert fb.brokerage == D("150.00") and fb.vat == D("24.00")
    assert fb.nse_levy + fb.cma_levy + fb.cdsc_levy + fb.icf_levy == D("29.00")
    assert fb.stamp_duty == 2
    assert fb.total == D("205.00") and fb.total_pct == D("2.05")


def test_stamp_duty_brackets_and_minimum():
    assert trade_fees(D("10000.01"), FeeSchedule()).stamp_duty == 4
    fb = trade_fees(D("1000"), FeeSchedule(brokerage_min=D("100")))
    assert fb.brokerage == D("100")
    assert trade_fees(D("0"), FeeSchedule()).total == 0


def test_dividend_tax():
    assert dividend_tax(D("1000"), FeeSchedule()) == D("50.00")


# ---------------------------------------------------------------- questionnaire

ALL_MIN = {q.key: 0 for q in QUESTIONS}
ALL_MAX = {q.key: len(q.options) - 1 for q in QUESTIONS}


def test_score_bounds_and_tolerance():
    assert score_answers(ALL_MIN) == 0
    assert score_answers(ALL_MAX) == 100
    assert [tolerance_for(s) for s in (0, 34, 35, 64, 65, 100)] == \
        ["conservative", "conservative", "moderate", "moderate", "aggressive", "aggressive"]


def test_score_validation():
    with pytest.raises(ValueError):
        score_answers({"horizon": 0})
    with pytest.raises(ValueError):
        score_answers({**ALL_MIN, "horizon": 9})


def test_profile_from_json_is_one_based():
    p, savings = profile_from_dict({"name": "J", "monthly_investable": 5000, "emergency_savings": 100,
                                    "answers": {"_help": "x", **{k: 1 for k in ALL_MIN}}})
    assert p.answers == ALL_MIN and p.risk_score == 0 and p.horizon_years == 1
    assert savings == D(100)


# ---------------------------------------------------------------- planner


def prof(**kw) -> Profile:
    base = dict(display_name="T", monthly_income=D(80000), monthly_expenses=D(40000),
                monthly_investable=D(10000), answers=dict(ALL_MAX))
    base.update(kw)
    return Profile(**base)


def test_debt_comes_first():
    plan = planner.build_plan(prof(high_interest_debt=D(25000)), {})
    assert plan.monthly_split == {"high_interest_debt": D(10000)}
    assert plan.steps[0].title.startswith("Clear high-interest debt")
    assert "3 month" in plan.steps[0].detail


def test_emergency_fund_before_investing():
    plan = planner.build_plan(prof(), {"emergency_cash": D(50000)})
    assert plan.emergency_target == D(240000)
    assert plan.monthly_split == {"emergency_cash": D(10000)}


def test_emergency_nearly_full_spills_into_investing():
    plan = planner.build_plan(prof(), {"emergency_cash": D(236000)})
    assert plan.monthly_split["emergency_cash"] == D(4000)
    assert sum(v for k, v in plan.monthly_split.items() if k in planner.INVEST_CLASSES) == D(6000)


def test_short_horizon_means_no_shares():
    answers = {**ALL_MAX, "horizon": 1}  # 1-3 years
    plan = planner.build_plan(prof(answers=answers), {"emergency_cash": D(240000)})
    assert plan.targets["nse_equities"] == 0
    assert "No shares for now" in plan.equity_rules[0]


def test_aggressive_long_horizon_small_portfolio():
    # under KES 100k of long-term money within a year -> no separate T-bill bucket
    plan = planner.build_plan(prof(monthly_investable=D(5000)), {"emergency_cash": D(240000)})
    assert plan.targets == {"money_market": D(20), "government_securities": D(0), "nse_equities": D(80)}
    assert sum(plan.targets.values()) == 100


def test_larger_portfolio_gets_government_securities_and_routes_to_gaps():
    current = {"emergency_cash": D(240000), "nse_equities": D(400000), "money_market": D(0),
               "government_securities": D(0)}
    answers = {**ALL_MAX, "outcome_range": 0, "drop_reaction": 1}  # score ~76, 15y horizon
    plan = planner.build_plan(prof(answers=answers), current)
    t = plan.targets
    assert t["government_securities"] > 0 and sum(t.values()) == 100
    # shares are overweight (100% vs target), so new money avoids them
    assert plan.monthly_split.get("nse_equities", 0) == 0
    assert plan.drift_pp["nse_equities"] > 5


def test_willingness_curve():
    assert [planner.willingness_equity(s) for s in (0, 50, 100)] == [20, 50, 80]
    assert [planner.horizon_equity_cap(y) for y in (1, 2, 4, 7, 15)] == [0, 0, 30, 60, 80]


# ---------------------------------------------------------------- portfolio

TX = [
    Txn("KCB", "buy", date(2024, 1, 2), D(100), D(10), D(5)),
    Txn("KCB", "buy", date(2024, 2, 1), D(100), D(20), D(5)),
    Txn("KCB", "sell", date(2024, 3, 1), D(50), D(30), D(3)),
    Txn("KCB", "bonus", date(2024, 4, 1), D(15), D(0)),
    Txn("KCB", "dividend", date(2024, 5, 1), D(165), D(1), tax=D("8.25")),
    Txn("SCOM", "buy", date(2024, 1, 5), D(1000), D(15), D(30)),
]


def test_average_cost_realised_bonus_dividend():
    pos = build_positions(TX)["KCB"]
    assert pos.shares == 165
    assert pos.cost == D("2257.5")                 # 3010 - 50 * 15.05
    assert pos.realised_pnl == D("744.5")          # 1500 - 3 - 752.5
    assert pos.dividends_net == D("156.75")
    assert pos.avg_cost.quantize(D("0.01")) == D("13.68")


def test_cannot_oversell():
    with pytest.raises(PortfolioError):
        build_positions(TX + [Txn("SCOM", "sell", date(2024, 6, 1), D(2000), D(15))])


def test_shares_held_on():
    assert shares_held_on(TX, "KCB", date(2024, 2, 15)) == 200
    assert shares_held_on(TX, "KCB", date(2024, 12, 31)) == 165


def test_summary_weights_and_sectors():
    prices = {"KCB": {"close": D(40), "trade_date": date(2024, 6, 1), "name": "KCB", "sector_code": "BANKING"},
              "SCOM": {"close": D(15), "trade_date": date(2024, 6, 1), "name": "Safaricom", "sector_code": "TELECOM"}}
    s = summarise(build_positions(TX), prices, {"money_market": D(5000)})
    assert s.equity_value == D(165 * 40 + 1000 * 15)
    assert [h.ticker for h in s.holdings] == ["SCOM", "KCB"]
    assert sum(h.weight_pct for h in s.holdings) == D("100.0")
    assert set(s.sector_pct) == {"BANKING", "TELECOM"}
    assert s.class_values() == {"money_market": D(5000), "nse_equities": s.equity_value}
    scom = s.holdings[0]
    assert scom.unrealised_pnl == D(-30)            # fees make it negative at cost price


def test_unpriced_holding_valued_at_cost():
    s = summarise(build_positions(TX[:1]), {})
    assert s.holdings[0].market_value == D(1005) and s.warnings


# ---------------------------------------------------------------- alert checks

PX = {"SCOM": {"close": D(20), "trade_date": date(2026, 9, 23), "change_pct": D("-6.2"), "volume": 1000,
               "high_52w": D(25), "low_52w": D(20), "trading_days_52w": 200}}


def test_price_rules():
    rules = [{"id": 1, "ticker": "SCOM", "condition": "below", "threshold": D(21)},
             {"id": 2, "ticker": "SCOM", "condition": "above", "threshold": D(21)},
             {"id": 3, "ticker": "SCOM", "condition": "move_pct", "threshold": D(5)}]
    hits = alerts.check_price_rules(rules, PX)
    assert [h.details["rule_id"] for h in hits] == [1, 3]


def test_big_move_and_52w_low():
    drafts = alerts.check_moves({"SCOM"}, PX, held={"SCOM"})
    assert {d.alert_type for d in drafts} == {"big_move", "52w_low"}
    assert "you hold" in drafts[0].message


def test_book_closure_window_and_amounts():
    acts = [{"id": 7, "ticker": "SCOM", "action_type": "final_dividend", "amount_per_share": D("0.65"),
             "book_closure_date": date(2026, 10, 1), "payment_date": date(2026, 10, 30)},
            {"id": 8, "ticker": "SCOM", "action_type": "final_dividend", "amount_per_share": D("1"),
             "book_closure_date": date(2026, 12, 1), "payment_date": None}]
    drafts = alerts.check_book_closures(acts, {"SCOM": D(1000)}, date(2026, 9, 24))
    assert len(drafts) == 1 and drafts[0].severity == "action"
    assert "650.00 gross" in drafts[0].message and "617.50" in drafts[0].message


def test_stale_check():
    assert alerts.check_stale(date(2026, 9, 1), date(2026, 9, 24), 5)
    assert not alerts.check_stale(date(2026, 9, 23), date(2026, 9, 24), 5)


def _summary(weights: dict[str, tuple[int, str]]):
    txns = [Txn(t, "buy", date(2024, 1, 1), D(v), D(1)) for t, (v, _) in weights.items()]
    prices = {t: {"close": D(1), "trade_date": date(2024, 1, 1), "name": t, "sector_code": sec}
              for t, (_, sec) in weights.items()}
    return summarise(build_positions(txns), prices)


def test_few_holdings_get_one_diversification_nudge():
    s = _summary({"SCOM": (800, "TELECOM"), "KCB": (200, "BANKING")})
    drafts = alerts.check_concentration(s, D(20), D(40), date(2024, 1, 1))
    assert [d.alert_type for d in drafts] == ["diversification"]
    assert "at least 5 companies" in drafts[0].message


def test_concentration_limits_with_enough_holdings():
    s = _summary({"SCOM": (300, "TELECOM"), "KCB": (200, "BANKING"), "EQTY": (200, "BANKING"),
                  "COOP": (150, "BANKING"), "EABL": (150, "MANUFACTURING")})
    drafts = alerts.check_concentration(s, D(20), D(40), date(2024, 1, 1))
    assert {(d.alert_type, d.ticker) for d in drafts} == {("concentration", "SCOM"),
                                                          ("sector_concentration", None)}
