"""Rules-based financial plan: what to do with this month's money.

Deterministic on purpose. An LLM can *explain* this plan later, but the
numbers come from rules you can read here and test:

  1. Clear high-interest debt first.
  2. Build an emergency fund (N months of expenses) in cash or an MMF.
  3. Split long-term money between money market, government securities and
     NSE equities, where the equity share comes from your risk score and is
     capped by your time horizon.
  4. Route each month's contribution to the most underweight class, so you
     rebalance by buying rather than by selling (which costs fees).
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from .fees import trade_fees
from .investor import Profile

INVEST_CLASSES = ("money_market", "government_securities", "nse_equities")
LABELS = {
    "high_interest_debt": "Pay down high-interest debt",
    "emergency_cash": "Emergency fund (cash / MMF)",
    "money_market": "Money market fund",
    "government_securities": "T-bills / government bonds",
    "nse_equities": "NSE shares",
}

# Below this much long-term money, T-bill/bond minimums make a separate
# government-securities bucket impractical, so it's folded into the MMF.
# Check the current minimum on CBK's DhowCSD and adjust via env if needed.
GOVT_SECURITIES_MIN = Decimal(os.getenv("GOVT_SECURITIES_MIN", "100000"))
DRIFT_TOLERANCE_PP = Decimal(os.getenv("DRIFT_TOLERANCE_PP", "5"))


def horizon_equity_cap(years: int) -> int:
    if years < 3:
        return 0
    if years < 5:
        return 30
    if years < 10:
        return 60
    return 80


def willingness_equity(score: int) -> int:
    """Risk score 0 -> 20%, 50 -> 50%, 100 -> 80%, rounded to 5."""
    raw = 20 + 0.6 * score
    return int(5 * round(min(max(raw, 20), 80) / 5))


@dataclass
class PlanStep:
    title: str
    detail: str
    monthly_amount: Decimal | None = None


@dataclass
class Plan:
    targets: dict[str, Decimal]                  # % of long-term money, sums to 100
    monthly_split: dict[str, Decimal]            # where this month's money goes
    steps: list[PlanStep] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    equity_rules: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    current_pct: dict[str, Decimal] = field(default_factory=dict)
    drift_pp: dict[str, Decimal] = field(default_factory=dict)
    emergency_target: Decimal | None = None
    emergency_current: Decimal = Decimal(0)


def _q(x: Decimal) -> Decimal:
    return Decimal(x).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def build_plan(p: Profile, current: dict[str, Decimal]) -> Plan:
    """`current` maps asset class -> KES value today (from portfolio.snapshot)."""
    M = Decimal(p.monthly_investable or 0)
    warnings: list[str] = []
    reasoning: list[str] = []

    # ---- target mix for long-term money
    score, years = p.risk_score, p.horizon_years
    will, cap = willingness_equity(score), horizon_equity_cap(years)
    equity = min(will, cap)
    reasoning.append(f"Risk score {score}/100 ({p.risk_tolerance}) supports about {will}% in shares.")
    if cap < will:
        reasoning.append(f"Your horizon (~{years} years) caps shares at {cap}%. Money needed "
                         "within 3 years shouldn't sit in shares, which can fall 30%+ and take years to recover.")
    rest = Decimal(100 - equity)
    long_term_total = sum((current.get(c, Decimal(0)) for c in INVEST_CLASSES), Decimal(0))
    if long_term_total + 12 * M < GOVT_SECURITIES_MIN:
        govt, mmf = Decimal(0), rest
        reasoning.append(f"Until your long-term money nears KES {GOVT_SECURITIES_MIN:,.0f}, "
                         "the non-share part stays in a money market fund. T-bill/bond minimums "
                         "make a separate bucket impractical before then.")
    else:
        govt = _q(rest * Decimal("0.6"))
        mmf = rest - govt
        reasoning.append("The non-share part is split 60/40 between government securities "
                         "(locks in yields) and a money market fund (easy access).")
        if current.get("government_securities", Decimal(0)) < GOVT_SECURITIES_MIN:
            reasoning.append("Park the T-bill/bond portion in your MMF until it's enough for the "
                             "minimum purchase on CBK's DhowCSD, then buy in one go.")
    targets = {"money_market": mmf, "government_securities": govt, "nse_equities": Decimal(equity)}

    # ---- this month's waterfall
    split = {k: Decimal(0) for k in ("high_interest_debt", "emergency_cash", *INVEST_CLASSES)}
    steps: list[PlanStep] = []
    remaining = M
    if M <= 0:
        warnings.append("Monthly investable amount is 0, so there's nothing to allocate this month.")

    debt = Decimal(p.high_interest_debt or 0)
    if debt > 0:
        pay = min(remaining, debt)
        split["high_interest_debt"] = pay
        remaining -= pay
        months = math.ceil(debt / M) if M else None
        steps.append(PlanStep(
            "Clear high-interest debt first",
            f"KES {debt:,.0f} outstanding. Mobile loans and cards often cost 20-100%+ a year, a "
            "guaranteed loss no NSE share reliably beats."
            + (f" At KES {M:,.0f}/month that's about {months} month(s)." if months else ""),
            pay))

    emergency_now = current.get("emergency_cash", Decimal(0))
    target_ef = (p.monthly_expenses * p.emergency_months_target) if p.monthly_expenses else None
    if target_ef is None:
        warnings.append("Monthly expenses unknown, so the emergency fund target can't be checked. "
                        "Update your profile.")
    elif emergency_now < target_ef:
        gap = target_ef - emergency_now
        put = min(remaining, gap)
        split["emergency_cash"] = put
        remaining -= put
        months = math.ceil(gap / M) if M else None
        steps.append(PlanStep(
            "Build your emergency fund",
            f"Target {p.emergency_months_target} months of expenses = KES {target_ef:,.0f}; "
            f"you have KES {emergency_now:,.0f}. Keep it in a money market fund. Without it, an "
            "emergency could force you to sell shares at a bad time."
            + (f" About {months} month(s) at your current rate." if months else ""),
            put))

    if remaining > 0:
        future_total = long_term_total + remaining
        shortfall = {c: max(Decimal(0), targets[c] / 100 * future_total - current.get(c, Decimal(0)))
                     for c in INVEST_CLASSES}
        total_short = sum(shortfall.values())
        weights = shortfall if total_short > 0 else {c: targets[c] for c in INVEST_CLASSES}
        wsum = sum(weights.values())
        allocated = Decimal(0)
        for c in INVEST_CLASSES[:-1]:
            amt = _q(remaining * weights[c] / wsum)
            split[c] = amt
            allocated += amt
        split[INVEST_CLASSES[-1]] = remaining - allocated
        steps.append(PlanStep(
            "Invest for the long term",
            "New money goes to whichever parts are below target, so the mix moves "
            "toward target without selling anything.",
            remaining))

    # ---- current mix vs target
    current_pct, drift = {}, {}
    if long_term_total > 0:
        for c in INVEST_CLASSES:
            pct = (100 * current.get(c, Decimal(0)) / long_term_total).quantize(Decimal("0.1"))
            current_pct[c] = pct
            drift[c] = pct - targets[c]

    # ---- share-picking guard rails
    small = trade_fees(Decimal(1000))
    ten_k = trade_fees(Decimal(10000))
    rules = [
        f"No single company above {p.max_stock_pct:.0f}% of your shares, no sector above "
        f"{p.max_sector_pct:.0f}%. On the NSE, banks and Safaricom dominate, so this matters.",
        "Aim for 5+ companies across 3+ sectors as the share portion grows; one stock is a bet, not a portfolio.",
        f"Fees: a KES 1,000 purchase costs about {small.total_pct}% and KES 10,000 about "
        f"{ten_k.total_pct}%, each way. Batch small monthly amounts into fewer, larger trades.",
        "Shares with very low daily volume can be hard to sell at a fair price. Check volume before buying.",
    ]
    if equity == 0:
        rules = ["No shares for now: your horizon is too short. Revisit when it's 3+ years."]

    return Plan(targets=targets, monthly_split={k: v for k, v in split.items() if v},
                steps=steps, reasoning=reasoning, equity_rules=rules, warnings=warnings,
                current_pct=current_pct, drift_pp=drift, emergency_target=target_ef,
                emergency_current=emergency_now)
